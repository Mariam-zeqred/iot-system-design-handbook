"""Chapter 3 - data modelling.

Four small tools in one file, standard-library only:

    python telemetry.py validate samples/*.json        # check records against the stream schemas
    python telemetry.py decode 01b40a... [--dev plot1]  # 11-byte LoRaWAN status frame -> status record
    python telemetry.py settings samples/desired.json   # desired settings -> clamped reported settings
    python telemetry.py growth streams.json [--markdown] # rows and bytes per device per year, per stream

``validate`` migrates a record with an older ``schema`` number to the current
one, then checks required fields, types, physical ranges and nullability.
Unknown fields are tolerated and listed, never rejected: a reader must survive
a newer writer.

``decode`` is the gateway's half of path C from chapter 2: the plot node has
no clock and sends no timestamp, so the gateway stamps the record with its own
receive time and keeps the frame's sequence counter so missed frames show.

``settings`` is the device's half of the desired/reported pattern: clamp every
value to the hardware limits, echo the version, say what was clamped.

``growth`` measures the example record of each stream and multiplies it out, so
the difference between "overwritten" and "appended" is a number, not a slogan.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# 1. Field specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One field of one stream.

    ``kind`` is the JSON type family: "int", "num" (int or float), "bool01"
    (0 or 1, the shape chapter 2's payloads used), "str", "list", "dict".
    ``lo``/``hi`` are the physical range for numbers; a value outside it is an
    error even if the type is right, which is how a unit change in place
    (27.4 degC sent as 274 tenths) gets caught.
    """

    kind: str
    required: bool = True
    nullable: bool = False
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] | None = None
    unit: str = ""


COMMON = {
    "schema": FieldSpec("int", lo=1, hi=SCHEMA_VERSION),
    "dev": FieldSpec("str"),
    "ts": FieldSpec("int", lo=1_600_000_000, hi=4_000_000_000, unit="s, UTC"),
}

# The current (v2) shape of every stream.  Field names are the contract shared
# by firmware, gateway, cloud and app; they are added to, never renamed.
SCHEMAS: dict[str, dict[str, FieldSpec]] = {
    "status": {
        **COMMON,
        "t": FieldSpec("num", nullable=True, lo=-40, hi=85, unit="degC"),
        "h": FieldSpec("num", nullable=True, lo=0, hi=100, unit="%RH"),
        "soil": FieldSpec("int", required=False, nullable=True, lo=0, hi=100, unit="%"),
        "tank": FieldSpec("int", required=False, nullable=True, lo=0, hi=100, unit="%"),
        "fan": FieldSpec("bool01"),
        "valve": FieldSpec("bool01"),
        "rssi": FieldSpec("int", required=False, lo=-130, hi=0, unit="dBm"),
        "vbat_mv": FieldSpec("int", required=False, lo=0, hi=5000, unit="mV"),
        "seq": FieldSpec("int", required=False, lo=0, hi=255),
        "fw": FieldSpec("str", required=False),
    },
    "history": {
        **COMMON,
        "period_s": FieldSpec("int", lo=1, hi=86_400, unit="s"),
        "n": FieldSpec("int", lo=0, hi=100_000),
        "t": FieldSpec("dict", nullable=True),
        "h": FieldSpec("dict", nullable=True),
        "fan_duty": FieldSpec("num", lo=0, hi=1),
        "soil": FieldSpec("int", required=False, nullable=True, lo=0, hi=100, unit="%"),
        "tank": FieldSpec("int", required=False, nullable=True, lo=0, hi=100, unit="%"),
    },
    "event": {
        **COMMON,
        "id": FieldSpec("str"),
        "type": FieldSpec(
            "str",
            choices=(
                "boot",
                "fan_on",
                "fan_off",
                "valve_open",
                "valve_close",
                "limit_hit",
                "sensor_fault",
                "setting_clamped",
            ),
        ),
        "severity": FieldSpec("str", choices=("info", "warning", "critical")),
        "reason": FieldSpec("str"),
        "ctx": FieldSpec("list", required=False),
        "ack_ts": FieldSpec(
            "int", required=False, nullable=True, lo=1_600_000_000, hi=4_000_000_000
        ),
        "resolved_ts": FieldSpec(
            "int", required=False, nullable=True, lo=1_600_000_000, hi=4_000_000_000
        ),
    },
    "settings_desired": {
        "schema": COMMON["schema"],
        "version": FieldSpec("int", lo=0, hi=2**31),
        "ts": COMMON["ts"],
        "t_on": FieldSpec("num", required=False, lo=0, hi=60, unit="degC"),
        "t_off": FieldSpec("num", required=False, lo=0, hi=60, unit="degC"),
        "h_max": FieldSpec("num", required=False, lo=0, hi=100, unit="%RH"),
        "valve_max_s": FieldSpec("int", required=False, lo=0, hi=86_400, unit="s"),
    },
    "settings_reported": {
        "schema": COMMON["schema"],
        "version": FieldSpec("int", lo=0, hi=2**31),
        "ts": COMMON["ts"],
        "src": FieldSpec("str", choices=("app", "local", "default")),
        "t_on": FieldSpec("num", lo=0, hi=60, unit="degC"),
        "t_off": FieldSpec("num", lo=0, hi=60, unit="degC"),
        "h_max": FieldSpec("num", lo=0, hi=100, unit="%RH"),
        "valve_max_s": FieldSpec("int", lo=0, hi=86_400, unit="s"),
        "clamped": FieldSpec("list"),
    },
}

# Three-level structure of an aggregate field.
AGGREGATE_KEYS = ("min", "mean", "max")


# ---------------------------------------------------------------------------
# 2. Migration: older records are upgraded on read, never rewritten in place
# ---------------------------------------------------------------------------


def migrate(
    kind: str, record: dict[str, Any], dev: str | None = None
) -> dict[str, Any]:
    """Return a copy of ``record`` upgraded to SCHEMA_VERSION.

    Version 1 is chapter 2's payload: no ``schema``, no ``dev`` (the device
    id lived only in the MQTT topic), no ``period_s`` on history.  Migration
    only adds fields; it never changes the meaning, type or unit of one that
    exists, so a v1 reader given a v2 record still works.
    """
    out = dict(record)
    version = int(out.get("schema", 1))
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"schema {version} is newer than this reader ({SCHEMA_VERSION})"
        )
    if version == 1:
        out["schema"] = 2
        if "dev" not in out and kind not in ("settings_desired", "settings_reported"):
            if dev is None:
                raise ValueError(
                    "v1 record has no 'dev'; pass the id from the topic or path"
                )
            out["dev"] = dev
        if kind == "history":
            out.setdefault("period_s", 600)  # every v1 aggregate was a 10-minute one
            out.setdefault("n", 0)
        if kind == "event":
            out.setdefault("severity", "info")
            out.setdefault(
                "id", f"{out['dev']}-{out.get('ts', 0)}-{out.get('type', '?')}"
            )
    return out


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------


@dataclass
class Report:
    kind: str
    errors: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    migrated_from: int | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _type_ok(spec: FieldSpec, value: Any) -> bool:
    if spec.kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec.kind == "num":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.kind == "bool01":
        return value in (0, 1) and not isinstance(value, bool)
    if spec.kind == "str":
        return isinstance(value, str)
    if spec.kind == "list":
        return isinstance(value, list)
    if spec.kind == "dict":
        return isinstance(value, dict)
    raise ValueError(f"unknown field kind {spec.kind}")


def validate(kind: str, record: dict[str, Any], dev: str | None = None) -> Report:
    """Migrate then check one record of stream ``kind``."""
    if kind not in SCHEMAS:
        raise ValueError(f"unknown stream kind {kind!r}; one of {sorted(SCHEMAS)}")
    rep = Report(kind)
    original = int(record.get("schema", 1))
    try:
        rec = migrate(kind, record, dev)
    except ValueError as exc:
        rep.errors.append(str(exc))
        return rep
    if original != rec["schema"]:
        rep.migrated_from = original

    specs = SCHEMAS[kind]
    for name, spec in specs.items():
        if name not in rec:
            if spec.required:
                rep.errors.append(f"{name}: missing")
            continue
        value = rec[name]
        if value is None:
            if not spec.nullable:
                rep.errors.append(f"{name}: null is not allowed")
            continue
        if not _type_ok(spec, value):
            rep.errors.append(
                f"{name}: expected {spec.kind}, got {type(value).__name__} {value!r}"
            )
            continue
        bounded = (
            spec.kind in ("int", "num") and spec.lo is not None and spec.hi is not None
        )
        if bounded and not (spec.lo <= value <= spec.hi):
            unit = f" {spec.unit}" if spec.unit else ""
            rep.errors.append(
                f"{name}: {value!r} outside [{spec.lo:g}, {spec.hi:g}]{unit}"
            )
        if spec.choices is not None and value not in spec.choices:
            rep.errors.append(f"{name}: {value!r} not one of {list(spec.choices)}")
        if spec.kind == "dict" and kind == "history":
            missing = [k for k in AGGREGATE_KEYS if k not in value]
            if missing:
                rep.errors.append(f"{name}: aggregate lacks {missing}")
            elif not (value["min"] <= value["mean"] <= value["max"]):
                rep.errors.append(f"{name}: min <= mean <= max violated: {value}")
    if kind == "event" and isinstance(rec.get("ctx"), list) and len(rec["ctx"]) > 60:
        rep.errors.append(
            f"ctx: {len(rec['ctx'])} samples, more than the 60 s of context agreed"
        )
    if kind == "settings_reported" and rec.get("t_on", 0) <= rec.get("t_off", 0):
        rep.errors.append("t_on must be above t_off (hysteresis band)")
    rep.unknown = sorted(set(rec) - set(specs))
    return rep


# ---------------------------------------------------------------------------
# 4. The 11-byte status frame of path C (plot node over LoRaWAN)
# ---------------------------------------------------------------------------

FRAME_VERSION = 1
# ver u8 | t i16 0.01 degC | h u16 0.01 %RH | soil u8 % | tank u8 % | flags u8 | vbat u16 mV | seq u8
FRAME_FMT = "<BhHBBBHB"
FRAME_LEN = struct.calcsize(FRAME_FMT)  # 11
FLAG_FAN, FLAG_VALVE, FLAG_T_OK, FLAG_H_OK, FLAG_SOIL_OK, FLAG_TANK_OK = (
    1 << i for i in range(6)
)


def encode_status_frame(rec: dict[str, Any], seq: int) -> bytes:
    """Device side: pack a status record into the 11-byte frame.

    A field the device could not read is sent as 0 with its valid bit clear;
    the receiver turns it back into null.  The frame carries no timestamp.
    """
    flags = (FLAG_FAN if rec["fan"] else 0) | (FLAG_VALVE if rec["valve"] else 0)
    t = rec.get("t")
    h = rec.get("h")
    soil = rec.get("soil")
    tank = rec.get("tank")
    if t is not None:
        flags |= FLAG_T_OK
    if h is not None:
        flags |= FLAG_H_OK
    if soil is not None:
        flags |= FLAG_SOIL_OK
    if tank is not None:
        flags |= FLAG_TANK_OK
    return struct.pack(
        FRAME_FMT,
        FRAME_VERSION,
        round((t or 0) * 100),
        round((h or 0) * 100),
        soil or 0,
        tank or 0,
        flags,
        int(rec.get("vbat_mv", 0)),
        seq & 0xFF,
    )


def decode_status_frame(frame: bytes, dev: str, received_ts: int) -> dict[str, Any]:
    """Gateway side: unpack a frame into a v2 status record.

    ``received_ts`` is the gateway's clock: the plot node has none.  The
    version byte is checked first so a future frame layout fails loudly
    instead of decoding into plausible nonsense.
    """
    if len(frame) != FRAME_LEN:
        raise ValueError(f"frame is {len(frame)} bytes, expected {FRAME_LEN}")
    ver, t_raw, h_raw, soil, tank, flags, vbat, seq = struct.unpack(FRAME_FMT, frame)
    if ver != FRAME_VERSION:
        raise ValueError(
            f"frame version {ver} not supported (decoder knows {FRAME_VERSION})"
        )
    return {
        "schema": SCHEMA_VERSION,
        "dev": dev,
        "ts": received_ts,
        "t": t_raw / 100 if flags & FLAG_T_OK else None,
        "h": h_raw / 100 if flags & FLAG_H_OK else None,
        "soil": soil if flags & FLAG_SOIL_OK else None,
        "tank": tank if flags & FLAG_TANK_OK else None,
        "fan": 1 if flags & FLAG_FAN else 0,
        "valve": 1 if flags & FLAG_VALVE else 0,
        "vbat_mv": vbat,
        "seq": seq,
    }


def missed_frames(prev_seq: int, seq: int) -> int:
    """How many frames were lost between two consecutive received ones (mod 256)."""
    return (seq - prev_seq - 1) % 256


# ---------------------------------------------------------------------------
# 5. Settings: desired (written by the app) -> reported (written by the device)
# ---------------------------------------------------------------------------

# Hardware limits live on the device (chapter 1, N5): the app may ask, the
# device decides.  The app shows the same limits so the two never disagree.
LIMITS: dict[str, tuple[float, float]] = {
    "t_on": (20.0, 40.0),
    "t_off": (18.0, 38.0),
    "h_max": (40.0, 95.0),
    "valve_max_s": (0, 1800),
}
DEFAULTS: dict[str, float] = {
    "t_on": 28.0,
    "t_off": 26.0,
    "h_max": 85.0,
    "valve_max_s": 600,
}
MIN_BAND = 1.0  # degC between t_off and t_on


def apply_settings(
    desired: dict[str, Any], current: dict[str, Any] | None, now_ts: int
) -> dict[str, Any]:
    """Device side: produce the reported settings record from a desired one.

    Fields the app did not send keep their current value (or the default);
    every value is clamped to LIMITS; the hysteresis band is enforced; the
    desired ``version`` is echoed so the app can tell *which* request this
    report answers.  A reported record is written even when nothing changed,
    because the app's only proof of delivery is seeing the version come back.
    """
    base = dict(DEFAULTS)
    if current:
        base.update({k: current[k] for k in DEFAULTS if k in current})
    clamped: list[str] = []
    out: dict[str, Any] = {}
    for name, default in base.items():
        value = desired.get(name, default)
        lo, hi = LIMITS[name]
        fixed = min(max(value, lo), hi)
        if isinstance(default, int):
            fixed = int(fixed)
        if fixed != value:
            clamped.append(name)
        out[name] = fixed
    if out["t_on"] - out["t_off"] < MIN_BAND:
        out["t_off"] = max(LIMITS["t_off"][0], out["t_on"] - MIN_BAND)
        clamped.append("t_off")
    return {
        "schema": SCHEMA_VERSION,
        "version": int(desired.get("version", 0)),
        "ts": now_ts,
        "src": "app",
        **out,
        "clamped": sorted(set(clamped)),
    }


# ---------------------------------------------------------------------------
# 6. Growth: what each stream costs in rows and bytes
# ---------------------------------------------------------------------------


def record_bytes(record: dict[str, Any]) -> int:
    return len(json.dumps(record, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class Growth:
    name: str
    mode: str  # overwrite | append
    rows_per_day: float
    bytes_per_row: int
    retention_days: float | None

    @property
    def rows_per_year(self) -> float:
        return 0.0 if self.mode == "overwrite" else self.rows_per_day * 365

    @property
    def mb_per_year(self) -> float:
        return self.rows_per_year * self.bytes_per_row / 1e6

    @property
    def steady_rows(self) -> float:
        if self.mode == "overwrite":
            return 1.0
        if self.retention_days is None:
            return float("inf")
        return self.rows_per_day * self.retention_days

    @property
    def steady_mb(self) -> float:
        return self.steady_rows * self.bytes_per_row / 1e6


def growth_rows(streams: dict[str, Any]) -> list[Growth]:
    rows = []
    for s in streams["streams"]:
        per_day = s["per_day"] if "per_day" in s else 86_400 / s["period_s"]
        rows.append(
            Growth(
                s["name"],
                s["mode"],
                float(per_day),
                record_bytes(s["example"]),
                s.get("retention_days"),
            )
        )
    return rows


def growth_report(path: Path, markdown: bool) -> str:
    streams = json.loads(path.read_text(encoding="utf-8"))
    fleet = int(streams.get("fleet_size", 1000))
    rows = growth_rows(streams)
    head = (
        "stream",
        "mode",
        "rows/day",
        "B/row",
        "MB/yr/device",
        "retention",
        "steady MB/device",
        f"steady GB/{fleet} devices",
    )
    table = []
    for g in rows:
        ret = (
            "-"
            if g.mode == "overwrite"
            else ("none" if g.retention_days is None else f"{g.retention_days:g} d")
        )
        if g.mode == "overwrite":
            steady = steady_fleet = "1 row"
        elif g.steady_mb == float("inf"):
            steady = steady_fleet = "unbounded"
        else:
            steady = f"{g.steady_mb:.2f}"
            steady_fleet = f"{g.steady_mb * fleet / 1000:.2f}"
        table.append(
            (
                g.name,
                g.mode,
                f"{g.rows_per_day:g}",
                str(g.bytes_per_row),
                f"{g.mb_per_year:.2f}",
                ret,
                steady,
                steady_fleet,
            )
        )
    if markdown:
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(r) + " |" for r in table]
        return "\n".join(lines)
    widths = [max(len(x) for x in col) for col in zip(head, *table)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*head)] + [fmt.format(*r) for r in table])


# ---------------------------------------------------------------------------
# 7. Command line
# ---------------------------------------------------------------------------


def _kind_from_name(path: Path) -> str:
    """samples/status_v1.json -> status; samples/settings_desired.json -> settings_desired."""
    stem = path.stem
    for kind in sorted(SCHEMAS, key=len, reverse=True):
        if stem.startswith(kind):
            return kind
    raise ValueError(f"cannot tell the stream kind from the file name {path.name}")


def validate_files(paths: list[Path], dev: str | None) -> tuple[str, int]:
    lines = []
    bad = 0
    for p in paths:
        kind = _kind_from_name(p)
        rep = validate(kind, json.loads(p.read_text(encoding="utf-8")), dev)
        status = "ok " if rep.ok else "BAD"
        notes = []
        if rep.migrated_from is not None:
            notes.append(f"migrated v{rep.migrated_from}->v{SCHEMA_VERSION}")
        if rep.unknown:
            notes.append(f"unknown fields tolerated: {rep.unknown}")
        lines.append(f"{status} {p.name:<26} {kind:<18} {'; '.join(notes)}".rstrip())
        for e in rep.errors:
            lines.append(f"      - {e}")
        bad += not rep.ok
    return "\n".join(lines), bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser(
        "validate", help="validate record files; stream kind from the file name"
    )
    v.add_argument("files", nargs="+", type=Path)
    v.add_argument(
        "--dev", default="gh1", help="device id for v1 records that lack one"
    )
    d = sub.add_parser("decode", help="decode an 11-byte status frame given as hex")
    d.add_argument("hex")
    d.add_argument("--dev", default="plot1")
    d.add_argument("--received-ts", type=int, default=1_760_400_000)
    s = sub.add_parser("settings", help="apply a desired settings file on the device")
    s.add_argument("desired", type=Path)
    s.add_argument("--current", type=Path, default=None)
    g = sub.add_parser("growth", help="rows and bytes per stream per device")
    g.add_argument("streams", type=Path)
    g.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "validate":
        text, bad = validate_files(args.files, args.dev)
        print(text)
        return 1 if bad else 0
    if args.cmd == "decode":
        rec = decode_status_frame(bytes.fromhex(args.hex), args.dev, args.received_ts)
        print(json.dumps(rec, indent=2))
        return 0
    if args.cmd == "settings":
        desired = json.loads(args.desired.read_text(encoding="utf-8"))
        current = (
            json.loads(args.current.read_text(encoding="utf-8"))
            if args.current
            else None
        )
        print(json.dumps(apply_settings(desired, current, 1_760_400_060), indent=2))
        return 0
    if args.cmd == "growth":
        print(growth_report(args.streams, args.markdown))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
