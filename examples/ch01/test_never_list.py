"""Smoke tests for the chapter 1 never-list checker (run with pytest)."""

from pathlib import Path

from never_list import Hazard, gap_report, load, markdown_table

HERE = Path(__file__).parent


def _ok() -> Hazard:
    return Hazard(
        id="X",
        hazard="Something bad",
        severity="critical",
        caused_by=["actuator"],
        detection="a limit",
        guarded_by=["mcu"],
        response="go safe",
        response_depends_on=["mcu"],
        test="bench",
    )


def test_complete_hazard_has_no_gaps() -> None:
    assert _ok().gaps() == []


def test_critical_response_must_not_need_network() -> None:
    h = _ok()
    h.response_depends_on = ["mcu", "cloud"]
    assert any("network down" in g for g in h.gaps())


def test_detection_must_be_independent_of_cause() -> None:
    h = _ok()
    h.caused_by = ["mcu"]
    h.guarded_by = ["mcu"]
    assert any("independent" in g for g in h.gaps())


def test_example_file_reports_the_deliberate_gap() -> None:
    hazards = load(HERE / "never_list.json")
    report, total = gap_report(hazards)
    assert total == 3, report  # N7 is left incomplete on purpose
    assert "N7" in report
    assert "| N1 |" in markdown_table(hazards)
