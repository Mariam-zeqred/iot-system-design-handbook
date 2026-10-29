"""Chapter 9 - observability and OTA.

Standard-library only:

    python fleet.py signals signals.json              # what each cloud shape shows you of a fleet
    python fleet.py synth --seed 7 --devices 60       # a synthetic status snapshot, labelled as such
    python fleet.py health status_snapshot.json       # the five lines a fleet dashboard needs
    python fleet.py presence                          # four detectors of one silence, a day of Wi-Fi hiccups
    python fleet.py checks                            # the device's refusals before it flashes an image
    python fleet.py keygen out/                       # an Ed25519 signing key pair (never committed)
    python fleet.py sign manifest.json image.bin --key out/ota_signing.key
    python fleet.py verify manifest.signed.json image.bin --pub out/ota_signing.pub --running 1.4.0 --hw gh-rev-b
    python fleet.py rollout --defect 0.15             # a staged rollout with a halt rule
    python fleet.py rollout --defect 0.15 --trials 500

``signals`` is a table read from JSON: for each thing you want to know about
a fleet you cannot touch, how each of chapter 8's three shapes shows it.
``synth`` writes a status snapshot for a fleet that does not exist, with the
faults of section 3 planted at known devices, and ``health`` turns any
snapshot of chapter 3's status records (plus the four fields this chapter
adds: ``up_s``, ``rst``, ``boots``, ``heap_min``) into presence, version,
reboot, delivery and memory lines.  ``presence`` simulates a day of short
Wi-Fi outages and a few long ones for a thousand devices and counts what each
detector would have told the farmer.  ``checks`` builds an image and a signed
manifest in a temporary directory and runs ten candidates through the eight
checks a device makes before and after it flashes; ``keygen``, ``sign`` and
``verify`` do the same on real files.  ``rollout`` plays a staged rollout
against a fleet in which a share of one hardware revision fails the health
check, and shows what the halt rule buys.  Nothing here talks to a device or a
cloud; the numbers are arithmetic on the inputs and on seeded random draws.

The Ed25519 implementation is the RFC 8032 reference algorithm written out in
Python integers so that the example stays standard-library.  It is for
reading, not for production: a device uses its SDK's verifier.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import random
import secrets
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).parent
DAY = 86400
NOW_DEFAULT = int(datetime(2026, 10, 29, 12, 0, tzinfo=UTC).timestamp())


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def human(seconds: float) -> str:
    s = round(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 2 * DAY:
        return f"{s / 3600:.1f} h"
    return f"{s / DAY:.1f} d"


# ---------------------------------------------------------------------------
# 1. signals: what each shape shows you of a fleet
# ---------------------------------------------------------------------------

SHAPES = ("firebase", "aws", "self")
SHAPE_NAMES = {
    "firebase": "Firebase RTDB",
    "aws": "AWS IoT Core + stores",
    "self": "self-hosted MQTT + Timescale",
}
LEVELS = ("native", "add-on", "you build")


def signal_rows(spec: dict) -> list[dict]:
    rows = []
    for sig in spec["signals"]:
        row = {"signal": sig["signal"], "question": sig["question"]}
        for p in SHAPES:
            level, how = sig[p]
            if level not in LEVELS:
                raise ValueError(f"unknown level {level!r} for {sig['signal']!r}")
            row[p] = level
            row[p + "_how"] = how
        rows.append(row)
    return rows


def signal_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    return {
        p: {lv: sum(1 for r in rows if r[p] == lv) for lv in LEVELS} for p in SHAPES
    }


def print_signals(spec: dict) -> None:
    rows = signal_rows(spec)
    w = max(len(r["signal"]) for r in rows)
    print(
        f"{'signal':<{w}}  {'Firebase RTDB':<14}  {'AWS IoT Core':<14}  {'self-hosted':<14}"
    )
    for r in rows:
        print(
            f"{r['signal']:<{w}}  {r['firebase']:<14}  {r['aws']:<14}  {r['self']:<14}"
        )
    counts = signal_counts(rows)
    print()
    for p in SHAPES:
        c = counts[p]
        print(
            f"{SHAPE_NAMES[p]:<30} native {c['native']:>2}   add-on {c['add-on']:>2}"
            f"   you build {c['you build']:>2}"
        )
    print()
    for r in rows:
        print(f"- {r['signal']}: {r['question']}")
        for p in SHAPES:
            print(f"    {SHAPE_NAMES[p]:<30} {r[p]:<9} {r[p + '_how']}")


# ---------------------------------------------------------------------------
# 2. the status record's four new fields, and the fleet table they allow
# ---------------------------------------------------------------------------

RESET_REASONS = ("power", "sw", "wdt", "panic", "brownout", "unknown")
LOOP_REASONS = ("wdt", "panic", "brownout")
CLASS_PERIOD = {"gh": 60, "pl": 600, "gw": 60}  # chapter 6's node mix
LOOP_UP_S = 600
LOW_HEAP_BYTES = 20_000
WEAK_DELIVERY = 0.90


def parse_version(text: str) -> tuple[int, int, int]:
    """Strict MAJOR.MINOR.PATCH, the incubator's rule: anything else is refused."""
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"version {text!r} is not MAJOR.MINOR.PATCH")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def device_class(dev: str) -> str:
    prefix = dev[:2]
    if prefix not in CLASS_PERIOD:
        raise ValueError(f"unknown device class for {dev!r}")
    return prefix


def synth_snapshot(seed: int = 7, devices: int = 60, now: int = NOW_DEFAULT) -> dict:
    """A status snapshot for a fleet that does not exist.

    Seventy percent greenhouse nodes, twenty percent plot nodes behind a
    gateway, ten percent gateways.  The faults of chapter 9 section 3 are
    planted at fixed device names when the fleet is large enough to hold them.
    """
    rng = random.Random(seed)
    n_gh = round(devices * 0.7)
    n_pl = round(devices * 0.2)
    n_gw = devices - n_gh - n_pl
    names = (
        [f"gh{i:02d}" for i in range(1, n_gh + 1)]
        + [f"pl{i:02d}" for i in range(1, n_pl + 1)]
        + [f"gw{i:02d}" for i in range(1, n_gw + 1)]
    )
    records = []
    for dev in names:
        cls = device_class(dev)
        period = CLASS_PERIOD[cls]
        up_s = rng.randint(DAY, 30 * DAY)
        rst = "power" if rng.random() < 0.7 else "sw"
        missed = rng.uniform(0.0, 0.02)
        rssi = {
            "gh": rng.randint(-75, -55),
            "pl": rng.randint(-115, -95),
            "gw": rng.randint(-70, -50),
        }[cls]
        rec = {
            "schema": 2,
            "dev": dev,
            "ts": now - rng.randint(0, period - 1),
            "t": round(rng.uniform(24.0, 31.0), 1),
            "h": round(rng.uniform(50.0, 75.0), 1),
            "rssi": rssi,
            "fw": "1.5.0",
            "seq": 0,
            "up_s": up_s,
            "rst": rst,
            "boots": rng.randint(5, 40),
            "heap_min": rng.randint(60_000, 110_000),
            "_missed": missed,
        }
        records.append(rec)
    by = {r["dev"]: r for r in records}

    def plant(dev: str, **fields) -> None:
        if dev in by:
            by[dev].update(fields)

    # presence: one unit silent for two days, two inside and outside their windows
    plant("gh07", ts=now - (2 * DAY + 3 * 3600 + 14 * 60), fw="1.4.0")
    plant("gh12", ts=now - 35 * 60)
    plant("pl03", ts=now - (2 * 3600 + 10 * 60))
    plant("pl05", ts=now - 70 * 60)
    # versions: five units the rollout has not reached
    for dev in ("gh01", "gh02", "gh03", "pl01", "pl02"):
        plant(dev, fw="1.4.0")
    # reboot loops, a brownout, a unit freshly restarted by an OTA
    plant("gh20", up_s=140, rst="wdt", boots=212)
    plant("gh21", up_s=230, rst="panic", boots=180)
    plant("gh30", up_s=3 * 3600 + 12 * 60, rst="brownout", boots=19)
    plant("gw02", up_s=50 * 60, rst="sw", boots=23)
    # weak links and a leak
    for dev, rssi, missed in (
        ("gh33", -84, 0.12),
        ("gh34", -86, 0.15),
        ("gh35", -87, 0.13),
        ("gh36", -88, 0.18),
    ):
        plant(dev, rssi=rssi, _missed=missed)
    plant("gh40", heap_min=9_800)

    for rec in records:
        period = CLASS_PERIOD[device_class(rec["dev"])]
        expected = rec["up_s"] // period
        rec["seq"] = round(expected * (1.0 - rec.pop("_missed")))
    return {
        "_meta": {
            "synthetic": True,
            "generator": f"python fleet.py synth --seed {seed} --devices {devices}",
            "now": now,
            "note": (
                "A fleet that does not exist. Faults are planted at fixed device names "
                "so the chapter can point at them; nothing here was measured."
            ),
        },
        "status": records,
    }


def health(
    snapshot: dict, now: int | None = None, current: str = "1.5.0", misses: int = 10
) -> dict:
    """The fleet table: presence, versions, reboots, delivery, memory."""
    records = snapshot["status"]
    now = int(
        now if now is not None else snapshot.get("_meta", {}).get("now", NOW_DEFAULT)
    )
    cur = parse_version(current)
    rows = []
    for r in records:
        cls = device_class(r["dev"])
        period = CLASS_PERIOD[cls]
        window = misses * period
        age = now - r["ts"]
        expected = max(1, r["up_s"] // period)
        delivery = min(1.0, r["seq"] / expected)
        rows.append(
            {
                "dev": r["dev"],
                "cls": cls,
                "period": period,
                "window": window,
                "age": age,
                "online": age <= window,
                "fw": r["fw"],
                "behind": parse_version(r["fw"]) < cur,
                "up_s": r["up_s"],
                "rst": r["rst"],
                "boots": r.get("boots"),
                "rebooted_24h": r["up_s"] < DAY,
                "loop_suspect": r["up_s"] < LOOP_UP_S and r["rst"] in LOOP_REASONS,
                "delivery": delivery,
                "weak": delivery < WEAK_DELIVERY,
                "rssi": r["rssi"],
                "heap_min": r["heap_min"],
                "low_heap": r["heap_min"] < LOW_HEAP_BYTES,
            }
        )
    offline = [x for x in rows if not x["online"]]
    by_fw: dict[str, int] = {}
    for x in rows:
        by_fw[x["fw"]] = by_fw.get(x["fw"], 0) + 1
    reasons: dict[str, int] = {}
    for x in rows:
        if x["rebooted_24h"]:
            reasons[x["rst"]] = reasons.get(x["rst"], 0) + 1
    fleet_delivery = sum(x["delivery"] for x in rows) / len(rows)
    return {
        "now": now,
        "devices": len(rows),
        "misses": misses,
        "current": current,
        "synthetic": bool(snapshot.get("_meta", {}).get("synthetic")),
        "generator": snapshot.get("_meta", {}).get("generator"),
        "online": len(rows) - len(offline),
        "offline": sorted(offline, key=lambda x: -x["age"]),
        "by_fw": dict(
            sorted(by_fw.items(), key=lambda kv: parse_version(kv[0]), reverse=True)
        ),
        "behind": [x for x in rows if x["behind"]],
        "rebooted_24h": sum(1 for x in rows if x["rebooted_24h"]),
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "loop_suspects": [x for x in rows if x["loop_suspect"]],
        "fleet_delivery": fleet_delivery,
        "weak": sorted([x for x in rows if x["weak"]], key=lambda x: x["delivery"]),
        "low_heap": [x for x in rows if x["low_heap"]],
        "rows": rows,
    }


def print_health(rep: dict) -> None:
    label = "synthetic snapshot" if rep["synthetic"] else "snapshot"
    print(f"{label}: {rep['devices']} devices, now = {iso(rep['now'])}")
    if rep["synthetic"]:
        print(f"  (generated by `{rep['generator']}`; a fleet that does not exist)")
    windows = ", ".join(f"{c} {rep['misses'] * p} s" for c, p in CLASS_PERIOD.items())
    print()
    print(
        f"presence   online {rep['online']}   offline {len(rep['offline'])}   (window = {rep['misses']} misses: {windows})"
    )
    for x in rep["offline"]:
        print(
            f"           {x['dev']}  silent {human(x['age']):>8}  last status {iso(rep['now'] - x['age'])}  fw {x['fw']}"
        )
    fw_line = "   ".join(f"{v}: {n}" for v, n in rep["by_fw"].items())
    print(f"firmware   {fw_line}")
    if rep["behind"]:
        names = " ".join(
            x["dev"] + ("*" if not x["online"] else "") for x in rep["behind"]
        )
        print(
            f"           {len(rep['behind'])} behind current {rep['current']}: {names}  (* = offline, the rollout cannot reach it)"
        )
    reasons = "   ".join(f"{k} {v}" for k, v in rep["reasons"].items()) or "none"
    print(
        f"reboots    rebooted in the last 24 h: {rep['rebooted_24h']}   by reason: {reasons}"
    )
    for x in rep["loop_suspects"]:
        print(
            f"           loop suspect {x['dev']}: up {x['up_s']} s after {x['rst']}, boots {x['boots']}"
        )
    print(
        f"delivery   fleet {100 * rep['fleet_delivery']:.1f} % of expected status writes since boot"
    )
    for x in rep["weak"]:
        print(
            f"           weak link {x['dev']}: {100 * x['delivery']:.0f} % delivered, rssi {x['rssi']} dBm"
        )
    if rep["low_heap"]:
        for x in rep["low_heap"]:
            print(
                f"memory     low heap_min {x['dev']}: {x['heap_min'] / 1000:.1f} kB (floor {LOW_HEAP_BYTES / 1000:.0f} kB)"
            )
    else:
        print(
            f"memory     no unit below {LOW_HEAP_BYTES / 1000:.0f} kB of minimum free heap"
        )


# ---------------------------------------------------------------------------
# 3. presence: four detectors of one silence
# ---------------------------------------------------------------------------

DETECTORS = (
    "status age > N3 window",
    "last will / lifecycle event, raw",
    "last will, 60 s hold-down",
    "last will, hold-down to N3",
    "device's own online flag",
)


def detect(
    d: float, phase: float, period_s: float, window_s: float, keepalive_s: float
) -> dict[str, float | None]:
    """When each detector would notify for one outage of ``d`` seconds.

    ``phase`` is how long before the outage the device last sent anything
    (uniform on a status period).  Returns the delay from the outage's start,
    or None when the detector stays quiet.  The will is published by the
    broker when 1.5 keep-alive intervals pass in silence, or at once when the
    device reconnects with the same client id first (session take-over,
    MQTT 3.1.1 section 3.1.4): either way it is published exactly once.
    """
    status_age = window_s - phase if d > window_s - phase else None
    timeout = 1.5 * keepalive_s - phase
    will = min(timeout, d)
    hold60 = will + 60.0 if d > will + 60.0 else None
    hold_n3 = max(0.0, window_s - 1.5 * keepalive_s)
    holdn3 = will + hold_n3 if d > will + hold_n3 else None
    return {
        DETECTORS[0]: status_age,
        DETECTORS[1]: will,
        DETECTORS[2]: hold60,
        DETECTORS[3]: holdn3,
        DETECTORS[4]: None,
    }


@dataclass
class PresenceParams:
    devices: int = 1000
    period_s: float = 60.0
    misses: int = 10
    keepalive_s: float = 300.0
    hiccups_per_day: float = 3.0
    hiccup_median_s: float = 40.0
    hiccup_sigma: float = 1.0
    long_per_day: float = 0.02
    long_min_s: float = 1800.0
    long_max_s: float = 7200.0
    seed: int = 1

    @property
    def window_s(self) -> float:
        return self.misses * self.period_s


def simulate_presence(p: PresenceParams) -> dict:
    rng = random.Random(p.seed)
    counts = {
        name: {"notified": 0, "wanted": 0, "noise": 0, "delay_sum": 0.0}
        for name in DETECTORS
    }
    outages = 0
    wanted = 0
    for _ in range(p.devices):
        k = _poisson(rng, p.hiccups_per_day)
        durations = [
            rng.lognormvariate(math.log(p.hiccup_median_s), p.hiccup_sigma)
            for _ in range(k)
        ]
        if rng.random() < p.long_per_day:
            durations.append(rng.uniform(p.long_min_s, p.long_max_s))
        for d in durations:
            outages += 1
            is_wanted = d >= p.window_s
            wanted += is_wanted
            phase = rng.uniform(0.0, p.period_s)
            for name, delay in detect(
                d, phase, p.period_s, p.window_s, p.keepalive_s
            ).items():
                if delay is None:
                    continue
                c = counts[name]
                c["notified"] += 1
                c["wanted" if is_wanted else "noise"] += 1
                c["delay_sum"] += delay
    for c in counts.values():
        c["missed"] = wanted - c["wanted"]
        c["mean_delay"] = c["delay_sum"] / c["notified"] if c["notified"] else None
    return {"params": p, "outages": outages, "wanted": wanted, "detectors": counts}


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, prod = math.exp(-lam), 0, rng.random()
    while prod > limit:
        k += 1
        prod *= rng.random()
    return k


def print_presence(res: dict) -> None:
    p: PresenceParams = res["params"]
    print(
        f"{p.devices} devices, one day; status every {p.period_s:.0f} s, N3 window {p.window_s:.0f} s "
        f"({p.misses} misses), MQTT keep-alive {p.keepalive_s:.0f} s (will after {1.5 * p.keepalive_s:.0f} s of silence)"
    )
    print(
        f"outages: {res['outages']} ({p.hiccups_per_day:g} short ones per device-day, median {p.hiccup_median_s:.0f} s; "
        f"{p.long_per_day:g} long ones per device-day, {p.long_min_s / 60:.0f}-{p.long_max_s / 60:.0f} min); "
        f"{res['wanted']} of them last at least the window and the farmer should hear about them"
    )
    print()
    w = max(len(n) for n in DETECTORS)
    print(
        f"{'detector':<{w}}  {'notified':>8}  {'wanted':>6}  {'noise':>6}  {'missed':>6}  mean delay"
    )
    for name in DETECTORS:
        c = res["detectors"][name]
        delay = human(c["mean_delay"]) if c["mean_delay"] is not None else "never"
        print(
            f"{name:<{w}}  {c['notified']:>8}  {c['wanted']:>6}  {c['noise']:>6}  {c['missed']:>6}  {delay}"
        )
    print()
    print(
        "wanted = outages at least one N3 window long; noise = notifications for shorter ones;"
    )
    print(
        "missed = wanted outages the detector never reported. Model of a schedule, not a fleet."
    )


# ---------------------------------------------------------------------------
# 4. Ed25519, RFC 8032 reference algorithm in Python integers
# ---------------------------------------------------------------------------

_q = 2**255 - 19
_l = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _q - 2, _q)


_d = (-121665 * _inv(121666)) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = (4 * _inv(5)) % _q
_B = (_xrecover(_By), _By)


def _edwards(P: tuple[int, int], Q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return (x3 % _q, y3 % _q)


def _scalarmult(P: tuple[int, int], e: int) -> tuple[int, int]:
    Q = (0, 1)
    for bit in bin(e)[2:] if e else "":
        Q = _edwards(Q, Q)
        if bit == "1":
            Q = _edwards(Q, P)
    return Q


def _encodepoint(P: tuple[int, int]) -> bytes:
    x, y = P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decodepoint(s: bytes) -> tuple[int, int]:
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (s[31] >> 7):
        x = _q - x
    if (-x * x + y * y - 1 - _d * x * x * y * y) % _q != 0:
        raise ValueError("point is not on the curve")
    return (x, y)


def _clamp(h: bytes) -> int:
    return (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)


def _hint(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def ed25519_publickey(sk: bytes) -> bytes:
    if len(sk) != 32:
        raise ValueError("a secret key is 32 bytes")
    a = _clamp(hashlib.sha512(sk).digest())
    return _encodepoint(_scalarmult(_B, a))


def ed25519_sign(m: bytes, sk: bytes, pk: bytes) -> bytes:
    h = hashlib.sha512(sk).digest()
    a = _clamp(h)
    r = _hint(h[32:] + m) % _l
    R = _encodepoint(_scalarmult(_B, r))
    S = (r + _hint(R + pk + m) * a) % _l
    return R + S.to_bytes(32, "little")


def ed25519_verify(sig: bytes, m: bytes, pk: bytes) -> bool:
    if len(sig) != 64 or len(pk) != 32:
        return False
    try:
        R = _decodepoint(sig[:32])
        A = _decodepoint(pk)
    except ValueError:
        return False
    S = int.from_bytes(sig[32:], "little")
    if S >= _l:
        return False
    h = _hint(sig[:32] + pk + m) % _l
    return _scalarmult(_B, S) == _edwards(R, _scalarmult(A, h))


# ---------------------------------------------------------------------------
# 5. the manifest, the signature over it, and the device's eight checks
# ---------------------------------------------------------------------------

MANIFEST_FIELDS = (
    "schema",
    "product",
    "hw",
    "version",
    "min_fw",
    "size",
    "sha256",
    "url",
)
IMAGE_MARKER = b"FARM_FW_VERSION="


def canonical(manifest: dict) -> bytes:
    """The bytes that are signed: sorted keys, no whitespace, the listed fields only."""
    missing = [k for k in MANIFEST_FIELDS if k not in manifest]
    if missing:
        raise ValueError(f"manifest lacks {missing}")
    body = {k: manifest[k] for k in MANIFEST_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign_manifest(manifest: dict, sk: bytes) -> dict:
    pk = ed25519_publickey(sk)
    sig = ed25519_sign(canonical(manifest), sk, pk)
    return {
        "manifest": {k: manifest[k] for k in MANIFEST_FIELDS},
        "sig": base64.b64encode(sig).decode(),
        "key_id": hashlib.sha256(pk).hexdigest()[:16],
    }


def make_manifest(
    image: bytes,
    version: str,
    hw: str = "gh-rev-b",
    min_fw: str = "1.0.0",
    product: str = "farm-monitor",
) -> dict:
    return {
        "schema": 1,
        "product": product,
        "hw": hw,
        "version": version,
        "min_fw": min_fw,
        "size": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
        "url": f"https://updates.example.net/{product}/{version}/{hw}.bin",
    }


def make_image(version: str, size: int = 300_000, seed: int = 1) -> bytes:
    marker = IMAGE_MARKER + version.encode() + b"\0"
    return marker + random.Random(seed).randbytes(size - len(marker))


CHECKS = (
    (
        "C1 signature",
        "the signature over the manifest verifies with the public key compiled into the running image",
    ),
    (
        "C2 newer",
        "the manifest's version parses as MAJOR.MINOR.PATCH and is strictly newer than the running one",
    ),
    ("C3 hardware", "the manifest names this unit's hardware id"),
    ("C4 stepping stone", "the running version is at least the manifest's min_fw"),
    ("C5 size", "the image fits the OTA partition, before a byte is downloaded"),
    (
        "C6 attempts",
        "this version has failed fewer than max_attempts times (counter in NVS)",
    ),
    (
        "C7 digest",
        "SHA-256 of the streamed bytes equals the manifest's, before the boot partition is switched",
    ),
    (
        "C8 health",
        "after the reboot: sensor answering and safety task ticking inside the health window, else the bootloader rolls back",
    ),
)


@dataclass
class Unit:
    running: str = "1.4.0"
    hw: str = "gh-rev-b"
    partition: int = 1_572_864  # 1.5 MiB OTA slot
    attempts: int = 0
    max_attempts: int = 3


def device_checks(
    signed: dict,
    pub: bytes,
    unit: Unit,
    image: bytes | None,
    healthy: bool | None = True,
) -> list[tuple[str, str, str]]:
    """Run the eight checks in order; stop at the first refusal.

    ``image`` is None when the download has not happened (checks C1-C6 only).
    ``healthy`` is what the health window would observe after the reboot.
    """
    out: list[tuple[str, str, str]] = []
    m = signed["manifest"]

    def refuse(name: str, note: str) -> list[tuple[str, str, str]]:
        out.append((name, "REFUSE", note))
        reached = {n for n, _, _ in out}
        for n, _ in CHECKS:
            if n not in reached:
                out.append((n, "not reached", ""))
        return out

    try:
        ok = ed25519_verify(base64.b64decode(signed["sig"]), canonical(m), pub)
    except (ValueError, KeyError) as exc:
        return refuse(CHECKS[0][0], f"manifest unusable: {exc}")
    if not ok:
        return refuse(
            CHECKS[0][0],
            "signature does not verify: not from our signer, or edited after signing",
        )
    out.append((CHECKS[0][0], "pass", f"key {signed.get('key_id', '?')}"))

    try:
        offered, running = parse_version(m["version"]), parse_version(unit.running)
    except ValueError as exc:
        return refuse(CHECKS[1][0], str(exc))
    if offered <= running:
        return refuse(
            CHECKS[1][0],
            f"{m['version']} is not newer than running {unit.running}: never sideways, never a downgrade",
        )
    out.append((CHECKS[1][0], "pass", f"{unit.running} -> {m['version']}"))

    if m["hw"] != unit.hw:
        return refuse(CHECKS[2][0], f"image is for {m['hw']}, this unit is {unit.hw}")
    out.append((CHECKS[2][0], "pass", unit.hw))

    if parse_version(m["min_fw"]) > running:
        return refuse(
            CHECKS[3][0], f"needs at least {m['min_fw']} running; install that first"
        )
    out.append((CHECKS[3][0], "pass", f"min_fw {m['min_fw']}"))

    if m["size"] > unit.partition:
        return refuse(
            CHECKS[4][0], f"{m['size']} bytes into a {unit.partition}-byte partition"
        )
    out.append((CHECKS[4][0], "pass", f"{m['size']} of {unit.partition} bytes"))

    if unit.attempts >= unit.max_attempts:
        return refuse(
            CHECKS[5][0],
            f"{unit.attempts} failed attempts at {m['version']} already; a person must look",
        )
    out.append(
        (CHECKS[5][0], "pass", f"attempt {unit.attempts + 1} of {unit.max_attempts}")
    )

    if image is None:
        out.append((CHECKS[6][0], "not reached", "download not performed"))
        out.append((CHECKS[7][0], "not reached", ""))
        return out
    digest = hashlib.sha256(image).hexdigest()
    if digest != m["sha256"] or len(image) != m["size"]:
        return refuse(
            CHECKS[6][0],
            "streamed bytes do not match the manifest: partition not switched",
        )
    out.append((CHECKS[6][0], "pass", digest[:16] + "..."))

    if healthy is None:
        out.append((CHECKS[7][0], "not reached", "not booted"))
    elif healthy:
        out.append(
            (CHECKS[7][0], "pass", "marked valid; the old image is now the fallback")
        )
    else:
        out.append(
            (
                CHECKS[7][0],
                "ROLLBACK",
                "health window missed: bootloader returns to the previous image, attempts += 1",
            )
        )
    return out


def verdict(results: list[tuple[str, str, str]]) -> tuple[str, str]:
    """(outcome, the check that decided it)."""
    for name, state, _ in results:
        if state in ("REFUSE", "ROLLBACK"):
            return state, name
    return "FLASHED", "all eight"


def checks_demo(seed: int = 1) -> list[dict]:
    """Ten candidates against one unit; everything is built in memory."""
    rng = random.Random(seed)
    sk = rng.randbytes(32)
    other_sk = rng.randbytes(32)
    pk = ed25519_publickey(sk)
    unit = Unit()
    image = make_image("1.5.0", seed=seed)
    good = sign_manifest(make_manifest(image, "1.5.0"), sk)

    flipped = bytearray(image)
    flipped[len(flipped) // 2] ^= 0x01
    edited = json.loads(json.dumps(good))
    edited["manifest"]["version"] = "1.6.0"
    old_image = make_image("1.3.0", seed=seed + 1)
    big_image = make_image("1.5.0", size=1_800_000, seed=seed + 2)

    cases = [
        ("1.5.0, signed, intact, healthy after boot", good, pk, unit, image, True),
        (
            "same manifest; one byte of the download flipped",
            good,
            pk,
            unit,
            bytes(flipped),
            True,
        ),
        ("manifest edited to 1.6.0 after signing", edited, pk, unit, image, True),
        (
            "1.5.0 signed with a different key",
            sign_manifest(make_manifest(image, "1.5.0"), other_sk),
            pk,
            unit,
            image,
            True,
        ),
        (
            "1.3.0 signed with the right key (an old image replayed)",
            sign_manifest(make_manifest(old_image, "1.3.0"), sk),
            pk,
            unit,
            old_image,
            True,
        ),
        (
            "1.4.0-ota, signed (a build tag in the version)",
            sign_manifest(make_manifest(image, "1.4.0-ota"), sk),
            pk,
            unit,
            image,
            True,
        ),
        (
            "1.5.0 built for hw pl-rev-a",
            sign_manifest(make_manifest(image, "1.5.0", hw="pl-rev-a"), sk),
            pk,
            unit,
            image,
            True,
        ),
        (
            "1.5.0 with min_fw 1.4.5; the unit runs 1.4.0",
            sign_manifest(make_manifest(image, "1.5.0", min_fw="1.4.5"), sk),
            pk,
            unit,
            image,
            True,
        ),
        (
            "1.5.0 of 1 800 000 bytes; the slot is 1 572 864",
            sign_manifest(make_manifest(big_image, "1.5.0"), sk),
            pk,
            unit,
            big_image,
            True,
        ),
        ("1.5.0 after three failed attempts", good, pk, Unit(attempts=3), image, True),
        (
            "1.5.0, intact, but the sensor never answers after boot",
            good,
            pk,
            unit,
            image,
            False,
        ),
    ]
    rows = []
    for title, signed, key, u, img, healthy in cases:
        results = device_checks(signed, key, u, img, healthy)
        outcome, by = verdict(results)
        note = next((n for _, s, n in results if s in ("REFUSE", "ROLLBACK")), "")
        rows.append(
            {
                "candidate": title,
                "outcome": outcome,
                "by": by,
                "note": note,
                "results": results,
            }
        )
    return rows


def print_checks(rows: list[dict]) -> None:
    print(
        "the unit: running 1.4.0, hardware gh-rev-b, OTA slot 1 572 864 bytes, max 3 attempts per version"
    )
    print()
    for name, meaning in CHECKS:
        print(f"{name:<18} {meaning}")
    print()
    w = max(len(r["candidate"]) for r in rows)
    print(f"{'candidate':<{w}}  {'outcome':<9}  decided by")
    for r in rows:
        print(f"{r['candidate']:<{w}}  {r['outcome']:<9}  {r['by']}")
    print()
    for r in rows:
        if r["note"]:
            print(f"- {r['candidate']}: {r['note']}")


# ---------------------------------------------------------------------------
# 6. the staged rollout and its halt rule
# ---------------------------------------------------------------------------


@dataclass
class RolloutParams:
    fleet: int = 1000
    stages: tuple[int, ...] = (1, 10, 100)  # cumulative percent of the fleet
    rev_b_share: float = 0.30  # share of units on the hardware revision the defect hits
    defect: float = (
        0.15  # probability a rev-B unit fails the health check on this image
    )
    flaky: float = (
        0.003  # probability a flash is interrupted (power cut): old image kept, retried
    )
    poll_s: float = 300.0  # the device polls its desired record every poll_s
    download_s: float = 30.0
    health_s: float = 180.0  # the health window after the reboot
    soak_s: float = 3600.0  # wait after the stage's last health report before deciding
    halt_rate: float = 0.01  # halt when failed / offered in a stage reaches this
    max_attempts: int = 3
    withdraw: bool = (
        True  # a halt also rewrites every unit's desired record to the old version
    )
    seed: int = 1


@dataclass
class Stage:
    index: int
    offered: int
    flashed: int = 0
    healthy: int = 0
    rolled_back: int = 0
    retried: int = 0
    duration_s: float = 0.0
    decision: str = ""


@dataclass
class RolloutResult:
    params: RolloutParams
    stages: list[Stage] = field(default_factory=list)
    halted: bool = False
    halted_at: int | None = None
    affected: int = 0  # units that booted the bad image and rolled back
    reached: int = 0  # units that run the new image at the end
    extra_flashes: int = (
        0  # retries by affected units if the halt did not withdraw the record
    )
    total_s: float = 0.0


def stage_sizes(fleet: int, stages: tuple[int, ...]) -> list[int]:
    sizes, so_far = [], 0
    for pct in stages:
        target = round(fleet * pct / 100)
        sizes.append(max(0, target - so_far))
        so_far = max(so_far, target)
    if so_far < fleet:
        sizes[-1] += fleet - so_far
    return sizes


def simulate_rollout(p: RolloutParams) -> RolloutResult:
    rng = random.Random(p.seed)
    units = [rng.random() < p.rev_b_share for _ in range(p.fleet)]
    rng.shuffle(units)
    res = RolloutResult(params=p)
    cursor = 0
    for i, n in enumerate(stage_sizes(p.fleet, p.stages), start=1):
        st = Stage(index=i, offered=n)
        last_report = 0.0
        for rev_b in units[cursor : cursor + n]:
            t = rng.uniform(0.0, p.poll_s)
            if rng.random() < p.flaky:
                st.retried += 1
                t += (
                    p.poll_s
                )  # the bootloader kept the old image; the next poll tries again
            t += p.download_s
            st.flashed += 1
            t += p.health_s
            if rev_b and rng.random() < p.defect:
                st.rolled_back += 1
            else:
                st.healthy += 1
            last_report = max(last_report, t)
        cursor += n
        st.duration_s = last_report + p.soak_s
        res.total_s += st.duration_s
        res.affected += st.rolled_back
        res.reached += st.healthy
        rate = st.rolled_back / st.offered if st.offered else 0.0
        if st.offered and st.rolled_back and rate >= p.halt_rate:
            st.decision = f"HALT: {st.rolled_back} of {st.offered} rolled back ({100 * rate:.1f} % >= {100 * p.halt_rate:g} %)"
            res.stages.append(st)
            res.halted, res.halted_at = True, i
            break
        st.decision = f"proceed: {st.rolled_back} of {st.offered} rolled back ({100 * rate:.1f} %)"
        res.stages.append(st)
    if res.halted and not p.withdraw:
        res.extra_flashes = res.affected * (p.max_attempts - 1)
    return res


def print_rollout(res: RolloutResult) -> None:
    p = res.params
    print(
        f"fleet {p.fleet}; stages {', '.join(str(s) + ' %' for s in p.stages)}; rev-B share {p.rev_b_share:g}, "
        f"defect {p.defect:g} on rev B ({100 * p.rev_b_share * p.defect:.1f} % of the fleet); "
        f"poll {p.poll_s:.0f} s, health {p.health_s:.0f} s, soak {p.soak_s:.0f} s; halt at {100 * p.halt_rate:g} %; seed {p.seed}"
    )
    print()
    print(
        f"{'stage':>5}  {'offered':>7}  {'flashed':>7}  {'healthy':>7}  {'rolled back':>11}  {'retried':>7}  {'took':>8}  decision"
    )
    for st in res.stages:
        print(
            f"{st.index:>5}  {st.offered:>7}  {st.flashed:>7}  {st.healthy:>7}  {st.rolled_back:>11}  "
            f"{st.retried:>7}  {human(st.duration_s):>8}  {st.decision}"
        )
    print()
    all_at_once = p.fleet * p.rev_b_share * p.defect
    if res.halted:
        print(
            f"halted at stage {res.halted_at}: {res.affected} units booted the bad image and rolled back, "
            f"{res.reached} run it; all at once would have hit about {all_at_once:.1f}"
        )
        if p.withdraw:
            print(
                "the halt rewrote every desired record to the old version, so the affected units stop retrying"
            )
        else:
            print(
                f"the halt only stopped new offers: the {res.affected} affected units retry at every poll until "
                f"max_attempts, {res.extra_flashes} more flash-reboot-rollback cycles"
            )
    else:
        print(
            f"completed: {res.reached} of {p.fleet} run the new image after {human(res.total_s)}, {res.affected} rolled back"
        )


def rollout_trials(p: RolloutParams, trials: int) -> dict:
    halted_at: dict[int, int] = {}
    affected: list[int] = []
    completed = 0
    for k in range(trials):
        r = simulate_rollout(RolloutParams(**{**p.__dict__, "seed": p.seed + k}))
        affected.append(r.affected)
        if r.halted:
            halted_at[r.halted_at] = halted_at.get(r.halted_at, 0) + 1
        else:
            completed += 1
    affected.sort()
    return {
        "trials": trials,
        "halted_at": dict(sorted(halted_at.items())),
        "completed": completed,
        "affected_mean": sum(affected) / trials,
        "affected_median": affected[trials // 2],
        "affected_max": affected[-1],
        "all_at_once": p.fleet * p.rev_b_share * p.defect,
    }


def print_trials(t: dict, p: RolloutParams) -> None:
    print(
        f"{t['trials']} rollouts of the same image, seeds {p.seed}..{p.seed + t['trials'] - 1}:"
    )
    for stage, n in t["halted_at"].items():
        print(f"  halted at stage {stage}: {n} ({100 * n / t['trials']:.0f} %)")
    print(
        f"  completed:         {t['completed']} ({100 * t['completed'] / t['trials']:.0f} %)"
    )
    print(
        f"  units that booted the bad image: mean {t['affected_mean']:.1f}, median {t['affected_median']}, "
        f"worst {t['affected_max']}; all at once: about {t['all_at_once']:.1f}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_key(path: str | Path) -> bytes:
    return bytes.fromhex(Path(path).read_text(encoding="utf-8").strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signals", help="what each cloud shape shows you of a fleet")
    s.add_argument("spec", nargs="?", default=str(HERE / "signals.json"))

    s = sub.add_parser("synth", help="write a synthetic status snapshot to stdout")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--devices", type=int, default=60)

    s = sub.add_parser("health", help="the fleet table from a status snapshot")
    s.add_argument("snapshot", nargs="?", default=str(HERE / "status_snapshot.json"))
    s.add_argument(
        "--now",
        type=int,
        default=None,
        help="unix time; default: the snapshot's _meta.now",
    )
    s.add_argument("--current", default="1.5.0", help="the version the rollout is at")
    s.add_argument(
        "--misses",
        type=int,
        default=10,
        help="N3: offline after this many missed status periods",
    )

    s = sub.add_parser(
        "presence", help="four detectors of one silence over a day of hiccups"
    )
    for name, default in (
        ("devices", 1000),
        ("period-s", 60.0),
        ("misses", 10),
        ("keepalive-s", 300.0),
        ("hiccups-per-day", 3.0),
        ("hiccup-median-s", 40.0),
        ("hiccup-sigma", 1.0),
        ("long-per-day", 0.02),
        ("long-min-s", 1800.0),
        ("long-max-s", 7200.0),
        ("seed", 1),
    ):
        s.add_argument(f"--{name}", type=type(default), default=default)

    s = sub.add_parser(
        "checks", help="ten candidates through the device's eight checks"
    )
    s.add_argument("--seed", type=int, default=1)

    s = sub.add_parser(
        "keygen", help="write ota_signing.key and ota_signing.pub into a directory"
    )
    s.add_argument("out_dir")

    s = sub.add_parser(
        "sign", help="sign a manifest over an image; writes <manifest>.signed.json"
    )
    s.add_argument("manifest")
    s.add_argument("image")
    s.add_argument("--key", required=True, help="path of the 32-byte hex secret key")
    s.add_argument("--out", default=None)

    s = sub.add_parser(
        "verify", help="run the device's checks on a signed manifest and an image"
    )
    s.add_argument("signed")
    s.add_argument(
        "image",
        nargs="?",
        default=None,
        help="omit to run only the checks made before the download",
    )
    s.add_argument("--pub", required=True)
    s.add_argument("--running", default="1.4.0")
    s.add_argument("--hw", default="gh-rev-b")
    s.add_argument("--partition", type=int, default=1_572_864)
    s.add_argument("--attempts", type=int, default=0)
    s.add_argument("--max-attempts", type=int, default=3)

    s = sub.add_parser("rollout", help="a staged rollout with a halt rule")
    s.add_argument("--fleet", type=int, default=1000)
    s.add_argument("--stages", default="1,10,100", help="cumulative percent per stage")
    s.add_argument("--rev-b-share", type=float, default=0.30)
    s.add_argument("--defect", type=float, default=0.15)
    s.add_argument("--flaky", type=float, default=0.003)
    s.add_argument("--poll-s", type=float, default=300.0)
    s.add_argument("--health-s", type=float, default=180.0)
    s.add_argument("--soak-s", type=float, default=3600.0)
    s.add_argument("--halt-rate", type=float, default=0.01)
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument(
        "--no-withdraw",
        action="store_true",
        help="the halt stops new offers but leaves the desired records",
    )
    s.add_argument("--seed", type=int, default=1)
    s.add_argument(
        "--trials",
        type=int,
        default=0,
        help="repeat with consecutive seeds and summarise",
    )

    a = ap.parse_args(argv)

    if a.cmd == "signals":
        print_signals(load(a.spec))
    elif a.cmd == "synth":
        json.dump(synth_snapshot(a.seed, a.devices), sys.stdout, indent=1)
        print()
    elif a.cmd == "health":
        print_health(health(load(a.snapshot), a.now, a.current, a.misses))
    elif a.cmd == "presence":
        p = PresenceParams(
            devices=a.devices,
            period_s=a.period_s,
            misses=a.misses,
            keepalive_s=a.keepalive_s,
            hiccups_per_day=a.hiccups_per_day,
            hiccup_median_s=a.hiccup_median_s,
            hiccup_sigma=a.hiccup_sigma,
            long_per_day=a.long_per_day,
            long_min_s=a.long_min_s,
            long_max_s=a.long_max_s,
            seed=a.seed,
        )
        print_presence(simulate_presence(p))
    elif a.cmd == "checks":
        print_checks(checks_demo(a.seed))
    elif a.cmd == "keygen":
        out = Path(a.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        sk = secrets.token_bytes(32)
        (out / "ota_signing.key").write_text(sk.hex() + "\n", encoding="utf-8")
        (out / "ota_signing.pub").write_text(
            ed25519_publickey(sk).hex() + "\n", encoding="utf-8"
        )
        print(
            f"wrote {out / 'ota_signing.key'} (keep it off every device and out of git) and {out / 'ota_signing.pub'}"
        )
    elif a.cmd == "sign":
        manifest = load(a.manifest)
        image = Path(a.image).read_bytes()
        manifest["size"] = len(image)
        manifest["sha256"] = hashlib.sha256(image).hexdigest()
        signed = sign_manifest(manifest, _read_key(a.key))
        out = Path(a.out) if a.out else Path(a.manifest).with_suffix(".signed.json")
        out.write_text(json.dumps(signed, indent=1) + "\n", encoding="utf-8")
        print(
            f"wrote {out}: {manifest['version']} for {manifest['hw']}, {manifest['size']} bytes, key {signed['key_id']}"
        )
    elif a.cmd == "verify":
        unit = Unit(a.running, a.hw, a.partition, a.attempts, a.max_attempts)
        image = Path(a.image).read_bytes() if a.image else None
        results = device_checks(
            load(a.signed), _read_key(a.pub), unit, image, healthy=None
        )
        for name, state, note in results:
            print(f"{name:<18} {state:<12} {note}")
        outcome, by = verdict(results)
        print(
            f"-> {outcome} ({by})"
            if outcome != "FLASHED"
            else "-> would flash (health check follows the reboot)"
        )
        return 0 if outcome == "FLASHED" else 1
    elif a.cmd == "rollout":
        p = RolloutParams(
            fleet=a.fleet,
            stages=tuple(int(x) for x in a.stages.split(",")),
            rev_b_share=a.rev_b_share,
            defect=a.defect,
            flaky=a.flaky,
            poll_s=a.poll_s,
            health_s=a.health_s,
            soak_s=a.soak_s,
            halt_rate=a.halt_rate,
            max_attempts=a.max_attempts,
            withdraw=not a.no_withdraw,
            seed=a.seed,
        )
        if a.trials:
            print_trials(rollout_trials(p, a.trials), p)
        else:
            print_rollout(simulate_rollout(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
