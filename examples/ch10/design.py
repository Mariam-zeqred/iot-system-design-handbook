"""Chapter 10 - the farm monitor end to end.

Four tools over the artefacts of chapters 1-9:

    python design.py bom bom.json            # bill of materials per node class against the brief's budget
    python design.py trace trace.json        # every decision traced to a chapter-1 row; gaps exit 1
    python design.py cost bom.json           # three-year cost at the brief's farm and at 10 / 1 000 devices
    python design.py dossier design.json     # the whole design record as Markdown, every earlier tool re-run

Nothing here measures anything: the prices in bom.json are labelled assumptions,
the cloud bill is chapter 8's arithmetic on chapter 3's records, and the
traceability check is bookkeeping over JSON files. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
REPO = EXAMPLES.parent

DEVICE_SIDE_CHAPTERS = {4, 5}
SHAPE_NAMES = {
    "firebase": "A Firebase RTDB",
    "aws": "B AWS IoT Core + stores",
    "self": "C self-hosted MQTT + Timescale",
}


# ---------------------------------------------------------------------------
# 0. plumbing: the earlier chapters' tools, imported from their folders
# ---------------------------------------------------------------------------


def chapter_module(chapter: int, filename: str) -> ModuleType:
    """Import `examples/chNN/<filename>` as a module without touching sys.path."""
    path = EXAMPLES / f"ch{chapter:02d}" / filename
    spec = importlib.util.spec_from_file_location(f"ch{chapter:02d}_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing chapter
        raise FileNotFoundError(path)
    if spec.name in sys.modules:
        return sys.modules[spec.name]
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses in the module look themselves up here
    spec.loader.exec_module(mod)
    return mod


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _table(head: list[str], rows: list[list[str]], markdown: bool) -> str:
    if markdown:
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        out += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(out)
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows, strict=True)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join(fmt.format(*r) for r in [head, *rows])


def _usd(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------------------
# 1. bom: parts, prices, the budget line
# ---------------------------------------------------------------------------


@dataclass
class ClassBill:
    id: str
    name: str
    budget_usd: float | None
    budget_from: str
    lines: list[dict[str, Any]]

    @property
    def total(self) -> float:
        return sum(ln["qty"] * ln["unit_usd"] for ln in self.lines)

    @property
    def within_budget(self) -> bool | None:
        if self.budget_usd is None:
            return None
        return self.total <= self.budget_usd

    @property
    def unsourced(self) -> list[str]:
        return [ln["part"] for ln in self.lines if not ln.get("source")]


def class_bills(spec: dict[str, Any]) -> list[ClassBill]:
    return [
        ClassBill(
            c["id"],
            c["name"],
            c.get("budget_usd"),
            c.get("budget_from", ""),
            c["parts"],
        )
        for c in spec["classes"]
    ]


def class_cost(spec: dict[str, Any], class_id: str) -> float:
    for cb in class_bills(spec):
        if cb.id == class_id:
            return cb.total
    raise KeyError(class_id)


def farm_hardware_usd(spec: dict[str, Any]) -> float:
    """The brief's farm: counts from bom.json's `farm` block."""
    return sum(n * class_cost(spec, cid) for cid, n in spec["farm"]["counts"].items())


def bom_report(spec: dict[str, Any], markdown: bool) -> tuple[str, int]:
    out: list[str] = []
    status = 0
    for cb in class_bills(spec):
        rows = [
            [
                ln["part"],
                str(ln["qty"]),
                _usd(ln["unit_usd"]),
                _usd(ln["qty"] * ln["unit_usd"]),
                ln["why"],
            ]
            for ln in cb.lines
        ]
        out.append(f"{'## ' if markdown else ''}{cb.name}")
        out.append(
            _table(["part", "qty", "unit", "line", "why (chapter)"], rows, markdown)
        )
        if cb.budget_usd is None:
            verdict = f"total {_usd(cb.total)}; {cb.budget_from}"
        elif cb.within_budget:
            verdict = (
                f"total {_usd(cb.total)} against {_usd(cb.budget_usd)}: within budget, "
                f"{_usd(cb.budget_usd - cb.total)} left for the second sensor and the spares ({cb.budget_from})"
            )
        else:
            status = 1
            verdict = f"total {_usd(cb.total)} against {_usd(cb.budget_usd)}: OVER BUDGET by {_usd(cb.total - cb.budget_usd)}"
        out.append(verdict)
        if cb.unsourced:
            status = 1
            out.append(f"! unsourced prices: {', '.join(cb.unsourced)}")
        out.append("")
    counts = spec["farm"]["counts"]
    parts = ", ".join(
        f"{n} x {cid} {_usd(class_cost(spec, cid))}" for cid, n in counts.items()
    )
    out.append(
        f"the brief's farm: {parts} = {_usd(farm_hardware_usd(spec))} of hardware, once"
    )
    out.append("every unit price is an assumption (bom.json `source`), not a quotation")
    return "\n".join(out), status


# ---------------------------------------------------------------------------
# 2. trace: decisions against chapter 1's rows
# ---------------------------------------------------------------------------


def load_hazards() -> list[Any]:
    ch01 = chapter_module(1, "never_list.py")
    return ch01.load(EXAMPLES / "ch01" / "never_list.json")


def trace_gaps(
    spec: dict[str, Any], hazards: list[Any], repo: Path = REPO
) -> list[str]:
    """Every gap as one line; an empty list is a traced design."""
    gaps: list[str] = []
    ids = {h.id for h in hazards} | set(spec["brief"])
    critical = {h.id for h in hazards if h.severity == "critical"}
    answered: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for d in spec["decisions"]:
        if not d["traces_to"]:
            gaps.append(f"ch{d['chapter']}: '{d['decision'][:50]}…' traces to nothing")
        for t in d["traces_to"]:
            if t not in ids:
                gaps.append(
                    f"ch{d['chapter']}: cites {t}, which is neither a hazard nor a brief line"
                )
            else:
                answered[t].append(d)
        if not (repo / d["artefact"]).exists():
            gaps.append(
                f"ch{d['chapter']}: artefact {d['artefact']} is not in the repository"
            )
    for h in hazards:
        ds = answered[h.id]
        if not ds:
            gaps.append(f"{h.id}: no decision answers it")
            continue
        if h.id in critical and not any(d.get("device_side") for d in ds):
            gaps.append(
                f"{h.id}: critical, but no decision of chapter 4 or 5 answers it on the device (Rule A)"
            )
        if h.id not in spec.get("hazard_tests", {}):
            gaps.append(f"{h.id}: no bench test (chapter 1 field 8)")
    for b in spec["brief"]:
        if not answered[b]:
            gaps.append(f"{b}: no decision answers this brief line")
    for d in spec["decisions"]:
        if (
            d.get("device_side")
            and d["chapter"] not in DEVICE_SIDE_CHAPTERS
            and d["chapter"] != 9
        ):
            gaps.append(
                f"ch{d['chapter']}: marked device_side but is not a firmware or hardware chapter"
            )
    return gaps


def trace_matrix(spec: dict[str, Any], hazards: list[Any]) -> dict[str, dict[int, int]]:
    """hazard id -> {chapter: number of decisions that cite it}."""
    chapters = sorted({d["chapter"] for d in spec["decisions"]})
    m: dict[str, dict[int, int]] = {}
    for key in [h.id for h in hazards] + list(spec["brief"]):
        m[key] = {c: 0 for c in chapters}
    for d in spec["decisions"]:
        for t in d["traces_to"]:
            if t in m:
                m[t][d["chapter"]] += 1
    return m


def trace_report(
    spec: dict[str, Any], hazards: list[Any], markdown: bool, repo: Path = REPO
) -> tuple[str, int]:
    m = trace_matrix(spec, hazards)
    chapters = sorted({d["chapter"] for d in spec["decisions"]})
    head = ["row", "severity", *[f"ch{c}" for c in chapters], "decisions", "bench test"]
    sev = {h.id: h.severity for h in hazards}
    rows = []
    for key, by_ch in m.items():
        rows.append(
            [
                key,
                sev.get(key, "brief"),
                *[str(by_ch[c]) if by_ch[c] else "." for c in chapters],
                str(sum(by_ch.values())),
                "yes"
                if key in spec.get("hazard_tests", {})
                else ("-" if key not in sev else "NO"),
            ]
        )
    out = [_table(head, rows, markdown), ""]
    gaps = trace_gaps(spec, hazards, repo)
    n = len(spec["decisions"])
    if gaps:
        out.append(f"{n} decisions, {len(gaps)} gap(s):")
        out += [f"  {g}" for g in gaps]
        return "\n".join(out), 1
    out.append(
        f"{n} decisions, 0 gaps: every decision cites a row, every row is answered, "
        "every critical row has a device-side response and a bench test, every artefact exists"
    )
    return "\n".join(out), 0


# ---------------------------------------------------------------------------
# 3. cost: hardware once + cloud and operations over the horizon
# ---------------------------------------------------------------------------


@dataclass
class CostRow:
    devices: int
    shape: str
    hardware: float
    cloud_month: float
    hours_month: float
    months: int
    rate: float
    note: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def cloud(self) -> float:
        return self.cloud_month * self.months

    @property
    def operations(self) -> float:
        return self.hours_month * self.rate * self.months

    @property
    def total(self) -> float:
        return self.hardware + self.cloud + self.operations

    @property
    def per_device_month(self) -> float:
        return self.total / self.devices / self.months


def hardware_for(bom: dict[str, Any], gh: int, plot: int, gw: int) -> float:
    return (
        gh * class_cost(bom, "gh")
        + plot * class_cost(bom, "plot")
        + gw * class_cost(bom, "gw")
    )


def cost_rows(
    bom: dict[str, Any],
    fleet: dict[str, Any],
    prices: dict[str, Any],
    sizes: list[int],
    months: int | None = None,
    rate: float | None = None,
) -> list[CostRow]:
    """Three shapes per fleet size, chapter 8's bill times the horizon plus chapter 10's hardware."""
    ch08 = chapter_module(8, "cloud.py")
    months = months or bom["horizon_months"]
    rate = bom["operations_usd_per_hour"]["value"] if rate is None else rate
    rows: list[CostRow] = []
    for n in sizes:
        gh, plot = ch08.class_counts(fleet, n)
        u, bills = ch08.bills(fleet, prices, n)
        hw = hardware_for(bom, gh, plot, u.gateways)
        for b in bills:
            spark = any("fits the Spark" in w for w in b.warnings)
            cloud = 0.0 if spark else b.total
            note = "Spark plan: $0" if spark else ""
            rows.append(
                CostRow(
                    n,
                    SHAPE_NAMES.get(b.platform, b.platform),
                    hw,
                    cloud,
                    b.hours,
                    months,
                    rate,
                    note,
                    {"gh": gh, "plot": plot, "gw": u.gateways},
                )
            )
    return rows


def farm_cost_row(
    bom: dict[str, Any], fleet: dict[str, Any], prices: dict[str, Any]
) -> CostRow:
    """The brief's farm on shape A: the counts from bom.json, chapter 8's bill at that size."""
    ch08 = chapter_module(8, "cloud.py")
    counts = bom["farm"]["counts"]
    n = counts["gh"] + counts["plot"]
    _, bills = ch08.bills(fleet, prices, n)
    fb = bills[0]
    spark = any("fits the Spark" in w for w in fb.warnings)
    return CostRow(
        n,
        SHAPE_NAMES.get(fb.platform, fb.platform),
        farm_hardware_usd(bom),
        0.0 if spark else fb.total,
        fb.hours,
        bom["horizon_months"],
        bom["operations_usd_per_hour"]["value"],
        "Spark plan: $0" if spark else "",
        dict(counts),
    )


def cost_report(
    bom: dict[str, Any],
    fleet: dict[str, Any],
    prices: dict[str, Any],
    sizes: list[int],
    markdown: bool,
) -> str:
    months = bom["horizon_months"]
    rate = bom["operations_usd_per_hour"]["value"]
    farm = farm_cost_row(bom, fleet, prices)
    farm_line = (
        f"the brief's farm ({farm.counts['gh']} greenhouse, {farm.counts['plot']} plot, {farm.counts['gw']} gateway), "
        f"{months} months on shape A: hardware {_usd(farm.hardware)} once, cloud {_usd(farm.cloud)} "
        f"({farm.note or 'billed'}), total {_usd(farm.total)}"
    )
    out = [farm_line, ""]
    head = [
        "devices",
        "shape",
        "nodes (gh / plot / gw)",
        "hardware once",
        f"cloud {months} mo",
        f"operations {months} mo",
        "total",
        "per device-month",
    ]
    rows = []
    for r in cost_rows(bom, fleet, prices, sizes, months, rate):
        rows.append(
            [
                f"{r.devices:,}",
                r.shape,
                f"{r.counts['gh']} / {r.counts['plot']} / {r.counts['gw']}",
                _usd(r.hardware),
                _usd(r.cloud) + (f" ({r.note})" if r.note else ""),
                _usd(r.operations)
                + (f" ({r.hours_month:.0f} h/mo)" if r.hours_month else ""),
                _usd(r.total),
                _usd(r.per_device_month),
            ]
        )
    out.append(_table(head, rows, markdown))
    out.append("")
    out.append(
        f"hardware from bom.json (assumed prices); cloud from examples/ch08/cloud.py on fleet.json and prices.json "
        f"(AWS prices from the Price List API of 2026-10-28, Firebase and transfer prices unverified); "
        f"operations at an assumed {_usd(rate)}/h on chapter 8's hours; nothing was bought, deployed or invoiced"
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 4. dossier: the design record, every earlier tool re-run
# ---------------------------------------------------------------------------


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue().rstrip()


def handover_missing(design: dict[str, Any], repo: Path = REPO) -> list[str]:
    return [h["file"] for h in design["handover"] if not (repo / h["file"]).exists()]


def dossier(design: dict[str, Any], repo: Path = REPO) -> tuple[str, int]:
    """The farm monitor's design record as Markdown: chapter 10's tables plus every earlier tool's."""
    ch01 = chapter_module(1, "never_list.py")
    ch02 = chapter_module(2, "data_path.py")
    ch03 = chapter_module(3, "telemetry.py")
    ch06 = chapter_module(6, "link.py")
    ch07 = chapter_module(7, "identity.py")
    bom = load_json(HERE / "bom.json")
    trace = load_json(HERE / "trace.json")
    fleet = load_json(EXAMPLES / "ch08" / "fleet.json")
    prices = load_json(EXAMPLES / "ch08" / "prices.json")
    hazards = load_hazards()

    s: list[str] = [f"# {design['name']}: design record", ""]
    s += ["## 1. Brief (chapter 1)", ""]
    s += [f"- **{k}** {v}" for k, v in trace["brief"].items()]
    s += ["", "## 2. Nodes", ""]
    s.append(
        _table(
            ["node", "class", "power", "link", "senses", "switches"],
            [
                [n["id"], n["class"], n["power"], n["link"], n["senses"], n["switches"]]
                for n in design["nodes"]
            ],
            True,
        )
    )
    s += [
        "",
        "## 3. Never-list (chapter 1, `examples/ch01/never_list.json`)",
        "",
        ch01.markdown_table(hazards),
    ]
    s += ["", "## 4. Quality numbers (chapter 1)", ""]
    s.append(
        _table(
            ["requirement", "value", "comes from"],
            [[q["requirement"], q["value"], q["from"]] for q in design["quality"]],
            True,
        )
    )
    s += [
        "",
        "## 5. Where each computation runs (chapter 2)",
        "",
        ch02.place_report(EXAMPLES / "ch02" / "computations.json", True),
    ]
    s += ["", "## 6. Streams (chapter 3)", ""]
    s.append(
        _table(
            ["stream", "writer", "mode", "key", "kept"],
            [
                [t["stream"], t["writer"], t["mode"], t["key"], t["kept"]]
                for t in design["streams"]
            ],
            True,
        )
    )
    s += ["", "status fields: `" + design["streams"][0]["fields"] + "`", ""]
    s += [ch03.growth_report(EXAMPLES / "ch03" / "streams.json", True)]
    s += ["", "## 7. Tasks (chapter 4)", ""]
    s.append(
        _table(
            ["task", "period", "core", "blocks", "feeds watchdog", "writes", "from"],
            [
                [
                    t["task"],
                    t["period"],
                    str(t["core"]),
                    t["blocks"],
                    t["watchdog"],
                    t["writes"],
                    t["from"],
                ]
                for t in design["tasks"]
            ],
            True,
        )
    )
    s += [
        "",
        "safe states: "
        + "; ".join(
            f"{a['actuator']} {a['safe']} ({a['why']})" for a in design["safe_states"]
        ),
    ]
    s += ["", "## 8. Safety layers (chapter 5)", ""]
    s.append(
        _table(
            ["layer", "senses", "acts through", "catches", "response"],
            [
                [
                    ly["layer"],
                    ly["senses"],
                    ly["acts_through"],
                    ly["catches"],
                    ly["response"],
                ]
                for ly in design["layers"]
            ],
            True,
        )
    )
    s += [
        "",
        "accepted: "
        + "; ".join(f"{a['failure']} ({a['why']})" for a in design["accepted"]),
    ]
    s += ["", "## 9. Links (chapter 6)", ""]
    nodes = ch06.load_nodes(EXAMPLES / "ch06" / "nodes.json")
    s.append(
        ch06.select_report(nodes, EXAMPLES / "ch06" / "links.json", True).replace(
            "\nRules in order", "\n\nRules in order"
        )
    )
    s += ["", "## 10. Identity inventory (chapter 7)", ""]
    inv = _capture(
        ch07.print_inventory, ch07.load_inventory(EXAMPLES / "ch07" / "inventory.json")
    )
    s.append(
        "\n".join(ln for ln in inv.splitlines() if not ln.startswith("# ")).strip()
    )
    s += ["", "## 11. Cloud (chapter 8)", ""]
    s += [
        f"- **{k}** {v if isinstance(v, str) else '; '.join(v)}"
        for k, v in design["cloud"].items()
    ]
    s += ["", "## 12. Fleet and rollout (chapter 9)", ""]
    s += [
        f"- **{k}** {v if isinstance(v, str) else (', '.join(map(str, v)) if isinstance(v, list) else v)}"
        for k, v in design["fleet"].items()
    ]
    s += [f"- **{k}** {v}" for k, v in design["rollout"].items()]
    s += ["", "## 13. Bill of materials (chapter 10)", ""]
    bom_text, _ = bom_report(bom, True)
    s.append(bom_text.replace("## ", "### "))
    s += ["", "## 14. Cost over the horizon (chapter 10)", ""]
    s.append(cost_report(bom, fleet, prices, [10, 1000], True))
    s += ["", "## 15. Traceability (chapter 10)", ""]
    tr_text, tr_status = trace_report(trace, hazards, True, repo)
    s.append(tr_text)
    s += ["", "## 16. Hand-over checklist", ""]
    missing = handover_missing(design, repo)
    rows = [
        [
            str(h["chapter"]),
            h["artefact"],
            f"`{h['file']}`",
            "missing" if h["file"] in missing else "yes",
        ]
        for h in design["handover"]
    ]
    s.append(_table(["chapter", "artefact", "file", "in the repo"], rows, True))
    status = 1 if (missing or tr_status) else 0
    s.append("")
    s.append(
        f"{len(design['handover'])} artefacts, {len(missing)} missing"
        + (": " + ", ".join(missing) if missing else "")
    )
    return "\n".join(s), status


# ---------------------------------------------------------------------------
# 5. command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bom", help="bill of materials against the brief's budget")
    b.add_argument("path", nargs="?", default=str(HERE / "bom.json"))
    b.add_argument("--markdown", action="store_true")
    t = sub.add_parser(
        "trace", help="decisions traced to chapter 1's rows; exit 1 on a gap"
    )
    t.add_argument("path", nargs="?", default=str(HERE / "trace.json"))
    t.add_argument("--markdown", action="store_true")
    c = sub.add_parser(
        "cost", help="hardware once plus cloud and operations over the horizon"
    )
    c.add_argument("path", nargs="?", default=str(HERE / "bom.json"))
    c.add_argument("--sizes", default="10,1000", help="fleet sizes, comma separated")
    c.add_argument("--markdown", action="store_true")
    d = sub.add_parser(
        "dossier",
        help="the design record as Markdown; exit 1 if an artefact is missing",
    )
    d.add_argument("path", nargs="?", default=str(HERE / "design.json"))
    a = p.parse_args(argv)

    if a.cmd == "bom":
        text, status = bom_report(load_json(a.path), a.markdown)
        print(text)
        return status
    if a.cmd == "trace":
        text, status = trace_report(load_json(a.path), load_hazards(), a.markdown)
        print(text)
        return status
    if a.cmd == "cost":
        bom = load_json(a.path)
        fleet = load_json(EXAMPLES / "ch08" / "fleet.json")
        prices = load_json(EXAMPLES / "ch08" / "prices.json")
        sizes = [int(x) for x in a.sizes.split(",")]
        print(cost_report(bom, fleet, prices, sizes, a.markdown))
        return 0
    text, status = dossier(load_json(a.path))
    print(text)
    return status


if __name__ == "__main__":
    sys.exit(main())
