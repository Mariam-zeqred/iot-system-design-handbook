"""Chapter 2 - choosing the data path.

Two small tools in one file, both standard-library only:

    python data_path.py place   computations.json [--markdown]
    python data_path.py traffic paths.json        [--markdown]

``place`` applies the chapter's five placement rules, in order, to every
computation in the JSON file and prints where each one should run and why.

``traffic`` measures the example payloads of each candidate data path and
prints messages per day, bytes per day and MB per month, per device and for
each fleet size, plus the offline-detection delay that the status period
implies.  The cost column is illustrative: it uses the price in the JSON
file, not a live price page.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Where a computation can run.  "device" is the MCU, "gateway" a computer on
# the farm's side of the internet link, "cloud" the back end, "app" the
# farmer's phone.
PLACES = ("device", "gateway", "cloud", "app")

# Inputs that the device cannot have on its own.
REMOTE_INPUTS = {"absence", "multi-device", "long-history", "external"}


@dataclass(frozen=True)
class Computation:
    name: str
    critical: bool
    deadline_s: float | None
    inputs: str  # local | absence | multi-device | long-history | external | user
    changes: str  # rarely | sometimes | often
    note: str = ""

    def place(self, accepted_outage_s: float) -> tuple[str, str]:
        """Return (place, rule) for this computation.

        The rules are applied in order; the first that matches wins.
        """
        if self.critical:
            return "device", "R1 critical: may not depend on the link (ch. 1 Rule A)"
        if self.inputs == "user":
            return "app", "R2 presentation and human input run where the person is"
        if self.inputs in REMOTE_INPUTS:
            tight = self.deadline_s is not None and self.deadline_s < 60
            if tight and self.inputs == "multi-device":
                return (
                    "gateway",
                    "R3 needs other devices' data within 60 s: a local hop",
                )
            return "cloud", f"R3 needs inputs the device cannot have ({self.inputs})"
        if self.deadline_s is not None and self.deadline_s < accepted_outage_s:
            return "device", (
                f"R4 deadline {self.deadline_s:g} s is shorter than the accepted outage "
                f"({accepted_outage_s:g} s)"
            )
        if self.changes == "often":
            return (
                "cloud",
                "R5 changes often and has no tight deadline: keep it where it is cheap to change",
            )
        return (
            "device",
            "R5 local inputs, no tight deadline, stable: run it at the source and send the summary",
        )


def load_computations(path: Path) -> tuple[float, list[Computation]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    comps = [Computation(**c) for c in data["computations"]]
    return float(data["accepted_outage_s"]), comps


def place_report(path: Path, markdown: bool) -> str:
    outage, comps = load_computations(path)
    rows = [(c.name, *c.place(outage)) for c in comps]
    if markdown:
        lines = ["| Computation | Runs on | Rule |", "|---|---|---|"]
        lines += [f"| {n} | **{p}** | {r} |" for n, p, r in rows]
        return "\n".join(lines)
    width = max(len(n) for n, _, _ in rows)
    lines = [f"{n:<{width}}  {p:<7}  {r}" for n, p, r in rows]
    counts = {p: sum(1 for _, q, _ in rows if q == p) for p in PLACES}
    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    return "\n".join([*lines, f"{len(rows)} computations: {summary}"])


# --------------------------------------------------------------------------
# Traffic


def payload_bytes(stream: dict) -> int:
    """Size of one message body: measured from the example, or given."""
    if "bytes" in stream:
        return int(stream["bytes"])
    body = json.dumps(stream["example"], separators=(",", ":"))
    return len(body.encode("utf-8"))


def message_bytes(stream: dict, link_overhead: int) -> int:
    """Body plus what the link adds per message.

    For MQTT: 2-byte fixed header, 2-byte topic length, the topic itself.
    ``link_overhead`` covers the transport (e.g. 29 bytes per TLS 1.2 record,
    13 bytes of LoRaWAN header).
    """
    mqtt = 4 + len(stream.get("topic", "")) if stream.get("topic") else 0
    return payload_bytes(stream) + mqtt + link_overhead


def messages_per_day(stream: dict) -> float:
    if "period_s" in stream:
        return 86400.0 / float(stream["period_s"])
    return float(stream["per_day"])


@dataclass(frozen=True)
class PathTotals:
    name: str
    link: str
    msgs_per_day: float
    bytes_per_day: float
    offline_delay_s: float

    @property
    def mb_per_month(self) -> float:
        return self.bytes_per_day * 30 / 1e6


def total_for(path: dict, offline_after_misses: int) -> PathTotals:
    overhead = int(path.get("link_overhead_bytes", 0))
    msgs = 0.0
    nbytes = 0.0
    for s in path["streams"]:
        n = messages_per_day(s)
        msgs += n
        nbytes += n * message_bytes(s, overhead)
    status = next(s for s in path["streams"] if s["name"] == path["status_stream"])
    delay = float(status["period_s"]) * offline_after_misses
    return PathTotals(path["name"], path["link"], msgs, nbytes, delay)


def traffic_report(path: Path, markdown: bool) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    misses = int(data["offline_after_misses"])
    price = float(data["illustrative_price_per_million_messages_usd"])
    fleets = [int(n) for n in data["fleet_sizes"]]
    totals = [total_for(p, misses) for p in data["paths"]]

    out: list[str] = []
    out.append("Per message, measured from the example payloads:")
    for p in data["paths"]:
        overhead = int(p.get("link_overhead_bytes", 0))
        for s in p["streams"]:
            out.append(
                f"  {p['name'][:1]} {s['name']:<26} body {payload_bytes(s):>4} B, "
                f"on the wire {message_bytes(s, overhead):>4} B, "
                f"{messages_per_day(s):>7.1f} msg/day"
            )
    out.append("")
    head = [
        "Path",
        "msg/day/device",
        "kB/day/device",
        "MB/month/device",
        f"offline seen after ({misses} misses)",
    ]
    head += [f"msg/month x{n}" for n in fleets]
    head += [f"USD/month x{n} (illustrative)" for n in fleets]
    rows: list[list[str]] = []
    for t in totals:
        row = [
            t.name,
            f"{t.msgs_per_day:,.0f}",
            f"{t.bytes_per_day / 1e3:,.1f}",
            f"{t.mb_per_month:,.2f}",
            f"{t.offline_delay_s / 60:,.0f} min",
        ]
        row += [f"{t.msgs_per_day * 30 * n:,.0f}" for n in fleets]
        row += [f"{t.msgs_per_day * 30 * n / 1e6 * price:,.2f}" for n in fleets]
        rows.append(row)
    if markdown:
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
        out += ["| " + " | ".join(r) + " |" for r in rows]
    else:
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(head)]
        out.append("  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)))
        out += [
            "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows
        ]
    out.append("")
    out.append(
        f"Cost is illustrative: {price:g} USD per million messages from paths.json, "
        "not a live price page. Chapter 8 prices real providers."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("place", "traffic"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        p.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "place":
        print(place_report(args.file, args.markdown))
    else:
        print(traffic_report(args.file, args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
