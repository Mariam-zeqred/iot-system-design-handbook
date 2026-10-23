"""Tests for chapter 5's layer checker and stuck-heater simulation."""

from __future__ import annotations

import json
from pathlib import Path

import layers as L
import pytest

SPEC = Path(__file__).with_name("layers.json")
ALL = "interlock,watchdog,gate,cutout"


# --- coverage: Rule B as a table -------------------------------------------


def test_spec_loads_and_names_only_known_parts():
    spec = L.load_spec(SPEC)
    assert {layer["id"] for layer in spec["layers"]} >= {
        "interlock",
        "watchdog",
        "gate",
        "cutout",
    }


def test_unknown_part_is_rejected(tmp_path):
    spec = json.loads(SPEC.read_text())
    spec["layers"][0]["through"].append("gremlin")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="gremlin"):
        L.load_spec(bad)


def test_firmware_layers_are_broken_by_every_firmware_failure():
    spec = L.load_spec(SPEC)
    by_id = {layer["id"]: layer for layer in spec["layers"]}
    fails = {f["id"]: f for f in spec["failures"]}
    for failure in ("mcu-hang", "bad-setpoint"):
        assert not L.catches(by_id["interlock"], fails[failure])
        assert not L.catches(by_id["control"], fails[failure])


def test_each_n1_failure_has_a_device_layer_and_the_cutout_is_the_last_resort():
    spec = L.load_spec(SPEC)
    rows = {row["failure"]["id"]: row for row in L.coverage(spec)}
    for failure in ("mcu-hang", "ssr-short", "sensor-stuck", "bad-setpoint"):
        assert rows[failure]["rule_b"], failure
        assert rows[failure]["device_layers"], failure
    # the two "firmware alive and wrong" cases are caught by the cutout alone
    for failure in ("sensor-stuck", "bad-setpoint"):
        assert [layer["id"] for layer in rows[failure]["device_layers"]] == ["cutout"]
    # a hang is caught fastest by the gate, then the watchdog
    assert [layer["id"] for layer in rows["mcu-hang"]["device_layers"]][:2] == [
        "gate",
        "watchdog",
    ]


def test_power_loss_is_accepted_not_a_gap_and_network_down_catches_nothing():
    spec = L.load_spec(SPEC)
    rows = {row["failure"]["id"]: row for row in L.coverage(spec)}
    assert rows["power-loss"]["rule_b"] and not rows["power-loss"]["device_layers"]
    assert rows["network-down"]["caught_by"] == []


# --- the chamber -------------------------------------------------------------


def test_equilibrium_with_the_heater_stuck_on_is_far_above_any_setpoint():
    ch = L.Chamber()
    assert ch.equilibrium_on == pytest.approx(27.5 + 0.5908 / 0.0111, abs=0.01)
    assert ch.equilibrium_on > 80


def test_sizing_minutes_are_monotone_and_match_the_chapter():
    cold = L.minutes_until(27.5, 35.0, True, q0=0.0)
    to_cutoff = L.minutes_until(35.0, 40.0, True)
    to_cutout = L.minutes_until(40.0, 45.0, True)
    assert round(cold) == 19
    assert round(to_cutoff) == 10
    assert round(to_cutout) == 12
    assert to_cutoff < to_cutout  # the rise slows as the chamber approaches equilibrium
    assert round(L.minutes_until(35.0, 30.0, False, q0=0.0)) == 99
    assert L.minutes_until(35.0, 90.0, True) == float("inf")  # above equilibrium: never


def test_overshoot_after_power_removed_is_under_one_degree():
    assert 0.5 < L.overshoot_after_off(45.0) < 1.0


def test_cutout_bounds_hold_for_the_default_design():
    rows = dict(L.sizing())
    assert (
        "ok"
        in rows[
            "cutout lower bound: nominal - tolerance > max setpoint + sensor error + gradient"
        ]
    )
    assert rows[
        "cutout upper bound: nominal + tolerance + lag overshoot, must be below harm"
    ].startswith("45 + 2 + 0.8 = 47.8")


# --- the simulation ----------------------------------------------------------


def test_no_failure_regulates_at_the_setpoint():
    r = L.simulate("none", "interlock", duration_s=120 * 60)
    assert r.power_removed_by == "nothing"
    assert r.minutes_above_cutoff == 0
    assert 34.5 < r.end_c < 37.0


def test_mcu_hang_firmware_only_never_recovers():
    r = L.simulate("mcu-hang", "interlock")
    assert r.power_removed_at_s is None
    assert r.peak_c > 65


def test_watchdog_catches_a_hang_in_five_seconds_and_the_firmware_comes_back():
    r = L.simulate("mcu-hang", "interlock,watchdog")
    assert r.power_removed_by == "watchdog"
    assert r.power_removed_at_s - r.failed_at_s == pytest.approx(5.0)
    assert r.resets == 1
    assert r.peak_c < 37.5
    assert 33.0 < r.end_c < 37.0  # control resumed after the reset


def test_gate_beats_the_watchdog_and_recloses_when_the_heartbeat_returns():
    r = L.simulate("mcu-hang", "interlock,watchdog,gate")
    assert r.power_removed_by == "gate"
    assert r.power_removed_at_s - r.failed_at_s == pytest.approx(3.0)
    assert any("closed again" in line for line in r.log)
    assert 33.0 < r.end_c < 37.0


def test_ssr_short_defeats_watchdog_but_not_the_gate():
    without = L.simulate("ssr-short", "interlock,watchdog")
    assert without.power_removed_at_s is None and without.peak_c > 65
    with_gate = L.simulate("ssr-short", "interlock,watchdog,gate")
    assert with_gate.power_removed_by == "gate"
    assert any("latched: cutoff 40" in line for line in with_gate.log)
    assert with_gate.peak_c < 42.0


def test_stuck_sensor_is_caught_by_max_on_time_late_and_by_the_cutout_early():
    fw = L.simulate("sensor-stuck", "interlock")
    assert fw.power_removed_by == "interlock"
    assert fw.power_removed_at_s - fw.failed_at_s == pytest.approx(90 * 60)
    assert fw.peak_c > 60
    hw = L.simulate("sensor-stuck", ALL)
    assert hw.power_removed_by == "cutout"
    assert hw.peak_c < 47.0
    assert hw.power_removed_at_s < fw.power_removed_at_s


def test_bad_setpoint_regulates_at_sixty_unless_the_cutout_is_there():
    fw = L.simulate("bad-setpoint", "interlock,watchdog,gate")
    assert fw.power_removed_at_s is None
    assert 59.0 < fw.end_c < 61.0
    hw = L.simulate("bad-setpoint", ALL)
    assert hw.power_removed_by == "cutout"


def test_compare_table_shape_and_headline_cells():
    rows = L.compare()
    assert rows[0] == ["failure", "firmware", "+ watchdog", "+ gate", "+ cutout"]
    assert len(rows) == 5
    cells = {row[0]: row[1:] for row in rows[1:]}
    assert cells["mcu-hang"][1].endswith("watchdog after 5 s")
    assert cells["mcu-hang"][2].endswith("gate after 3 s")
    assert cells["ssr-short"][2].endswith("gate after 17 min")
    assert cells["sensor-stuck"][3].endswith("cutout after 31 min")
    assert "no layer acted" in cells["bad-setpoint"][2]


def test_unknown_layer_and_failure_are_rejected():
    with pytest.raises(ValueError):
        L.simulate("mcu-hang", "interlock,prayer")
    with pytest.raises(ValueError):
        L.simulate("meteor", "interlock")


def test_cli_runs(capsys):
    assert L.main(["coverage", str(SPEC)]) == 0
    assert L.main(["sizing"]) == 0
    assert (
        L.main(["run", "--fail", "ssr-short", "--layers", ALL, "--minutes", "90"]) == 0
    )
    out = capsys.readouterr().out
    assert "Rule B ok" in out and "power removed by gate" in out
