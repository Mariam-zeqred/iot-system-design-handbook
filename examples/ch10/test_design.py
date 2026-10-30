"""Tests for chapter 10's design tool. Run with `pytest -q` from this folder."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import design

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
REPO = EXAMPLES.parent


def _bom() -> dict:
    return design.load_json(HERE / "bom.json")


def _trace() -> dict:
    return design.load_json(HERE / "trace.json")


def _design() -> dict:
    return design.load_json(HERE / "design.json")


def _fleet_prices() -> tuple[dict, dict]:
    return (
        design.load_json(EXAMPLES / "ch08" / "fleet.json"),
        design.load_json(EXAMPLES / "ch08" / "prices.json"),
    )


# --- plumbing ---------------------------------------------------------------


def test_chapter_module_imports_each_earlier_tool_once():
    a = design.chapter_module(1, "never_list.py")
    b = design.chapter_module(1, "never_list.py")
    assert a is b
    assert hasattr(a, "markdown_table")
    assert hasattr(design.chapter_module(8, "cloud.py"), "bills")


# --- bom --------------------------------------------------------------------


def test_bom_totals_and_budget():
    bom = _bom()
    bills = {cb.id: cb for cb in design.class_bills(bom)}
    assert round(bills["gh"].total, 2) == 60.20
    assert round(bills["plot"].total, 2) == 43.00
    assert round(bills["gw"].total, 2) == 137.00
    assert bills["gh"].within_budget is True
    assert bills["plot"].within_budget is None  # no budget line in the brief
    assert round(design.farm_hardware_usd(bom), 2) == 300.40


def test_every_price_names_a_source():
    for cb in design.class_bills(_bom()):
        assert cb.unsourced == []
        for ln in cb.lines:
            assert ln["source"] == "assumption"


def test_bom_report_exits_1_over_budget_or_unsourced():
    bom = _bom()
    text, status = design.bom_report(bom, markdown=False)
    assert status == 0 and "within budget" in text
    over = copy.deepcopy(bom)
    over["classes"][0]["parts"][0]["unit_usd"] = 200.0
    text, status = design.bom_report(over, markdown=False)
    assert status == 1 and "OVER BUDGET" in text
    unsourced = copy.deepcopy(bom)
    unsourced["classes"][1]["parts"][0]["source"] = ""
    _, status = design.bom_report(unsourced, markdown=False)
    assert status == 1


def test_bom_markdown_has_one_table_per_class():
    text, _ = design.bom_report(_bom(), markdown=True)
    assert text.count("| part | qty | unit | line | why (chapter) |") == 3


# --- trace ------------------------------------------------------------------


def test_hazards_come_from_chapter_1():
    ids = [h.id for h in design.load_hazards()]
    assert ids == ["N1", "N2", "N3", "N4", "N5", "N6", "N7"]


def test_trace_has_no_gaps():
    assert design.trace_gaps(_trace(), design.load_hazards()) == []


def test_trace_matrix_counts_decisions():
    m = design.trace_matrix(_trace(), design.load_hazards())
    assert sum(m["N1"].values()) == 7
    assert m["N7"][7] == 2  # chapter 7 answers N7 twice
    assert sum(m["B1"].values()) == 2


def test_trace_finds_an_unanswered_row():
    spec = copy.deepcopy(_trace())
    for d in spec["decisions"]:
        d["traces_to"] = [t for t in d["traces_to"] if t != "N6"]
    gaps = design.trace_gaps(spec, design.load_hazards())
    assert any(g.startswith("N6: no decision") for g in gaps)


def test_trace_finds_a_critical_row_without_a_device_side_response():
    spec = copy.deepcopy(_trace())
    for d in spec["decisions"]:
        if d.get("device_side"):
            d["traces_to"] = [t for t in d["traces_to"] if t != "N4"]
    gaps = design.trace_gaps(spec, design.load_hazards())
    assert any("N4: critical" in g and "Rule A" in g for g in gaps)


def test_trace_finds_an_unknown_id_a_missing_artefact_and_a_missing_test():
    spec = copy.deepcopy(_trace())
    spec["decisions"][0]["traces_to"].append("N9")
    spec["decisions"][1]["artefact"] = "examples/ch02/does_not_exist.json"
    del spec["hazard_tests"]["N5"]
    gaps = design.trace_gaps(spec, design.load_hazards())
    assert any("cites N9" in g for g in gaps)
    assert any("does_not_exist.json is not in the repository" in g for g in gaps)
    assert any(g == "N5: no bench test (chapter 1 field 8)" for g in gaps)


def test_trace_report_status():
    text, status = design.trace_report(_trace(), design.load_hazards(), markdown=True)
    assert status == 0
    assert "23 decisions, 0 gaps" in text
    assert "| N7 | critical |" in text


# --- cost -------------------------------------------------------------------


def test_cost_rows_add_up():
    bom = _bom()
    fleet, prices = _fleet_prices()
    rows = design.cost_rows(bom, fleet, prices, [10, 1000])
    assert len(rows) == 6
    for r in rows:
        assert abs(r.total - (r.hardware + r.cloud + r.operations)) < 1e-6
        assert abs(r.per_device_month - r.total / r.devices / 36) < 1e-9
    ten = {r.shape: r for r in rows if r.devices == 10}
    assert (
        ten["A Firebase RTDB"].cloud == 0.0
        and ten["A Firebase RTDB"].note == "Spark plan: $0"
    )
    assert ten["C self-hosted MQTT + Timescale"].hours_month == 2
    assert ten["A Firebase RTDB"].counts == {"gh": 7, "plot": 3, "gw": 1}
    assert round(ten["A Firebase RTDB"].hardware, 2) == round(
        7 * 60.20 + 3 * 43.00 + 137.00, 2
    )


def test_hardware_dominates_the_three_year_total_except_self_hosted_at_ten():
    bom = _bom()
    fleet, prices = _fleet_prices()
    for r in design.cost_rows(bom, fleet, prices, [10, 1000]):
        if r.devices == 10 and r.shape.startswith("C"):
            assert (
                r.cloud + r.operations > r.hardware
            )  # one instance and 2 h a month outweigh ten nodes
        else:
            assert r.hardware > r.cloud + r.operations


def test_farm_cost_row_is_the_brief():
    bom = _bom()
    fleet, prices = _fleet_prices()
    r = design.farm_cost_row(bom, fleet, prices)
    assert r.devices == 3 and r.counts == {"gh": 2, "plot": 1, "gw": 1}
    assert round(r.hardware, 2) == 300.40 and r.cloud == 0.0
    assert round(r.total, 2) == 300.40


def test_cost_report_mentions_sources():
    bom = _bom()
    fleet, prices = _fleet_prices()
    text = design.cost_report(bom, fleet, prices, [10], markdown=True)
    assert "assumed prices" in text and "unverified" in text
    assert text.count("| 10 |") == 3


# --- dossier ----------------------------------------------------------------


def test_dossier_has_sixteen_sections_and_no_missing_artefact():
    text, status = design.dossier(_design())
    assert status == 0
    assert text.count("\n## ") == 16
    assert "12 artefacts, 0 missing" in text
    assert "| N1 |" in text  # the never-list table came from chapter 1's tool
    assert "| Plot node (soil, tank) |" in text  # chapter 6's selection
    assert "12 values, 0 gaps" in text  # chapter 7's inventory


def test_dossier_reports_a_missing_artefact():
    d = copy.deepcopy(_design())
    d["handover"][0]["file"] = "ch99-missing.md"
    text, status = design.dossier(d)
    assert status == 1 and "1 missing: ch99-missing.md" in text


def test_design_json_is_consistent_with_chapters():
    d = _design()
    assert [t["task"] for t in d["tasks"]] == [
        "sensor",
        "safety",
        "control",
        "display",
        "datalog",
        "comms",
    ]
    assert d["fleet"]["status_fields_added"] == ["up_s", "rst", "boots", "heap_min"]
    assert {a["actuator"]: a["safe"] for a in d["safe_states"]} == {
        "fan": "on",
        "valve": "closed",
    }
    assert all((REPO / h["file"]).exists() for h in d["handover"])


# --- command line -----------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HERE / "design.py"), *args],
        capture_output=True,
        text=True,
        cwd=HERE,
        check=False,
    )


def test_cli_bom_trace_cost_dossier():
    assert _run("bom").returncode == 0
    assert _run("trace", "--markdown").returncode == 0
    out = _run("cost", "--sizes", "10")
    assert out.returncode == 0 and "the brief's farm" in out.stdout
    out = _run("dossier")
    assert out.returncode == 0 and out.stdout.startswith(
        "# farm monitor: design record"
    )


def test_cli_trace_exits_1_on_a_gap(tmp_path):
    spec = _trace()
    spec["decisions"] = spec["decisions"][:3]
    p = tmp_path / "t.json"
    p.write_text(json.dumps(spec))
    out = _run("trace", str(p))
    assert out.returncode == 1 and "gap(s)" in out.stdout
