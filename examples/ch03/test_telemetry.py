import json
from pathlib import Path

import pytest
from telemetry import (
    FRAME_LEN,
    LIMITS,
    SCHEMA_VERSION,
    SCHEMAS,
    apply_settings,
    decode_status_frame,
    encode_status_frame,
    growth_rows,
    migrate,
    missed_frames,
    record_bytes,
    validate,
    validate_files,
)

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"


def load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


# --- migration -------------------------------------------------------------


def test_v1_status_gains_schema_and_dev_only():
    v1 = load("status_v1.json")
    v2 = migrate("status", v1, dev="gh1")
    assert v2["schema"] == SCHEMA_VERSION and v2["dev"] == "gh1"
    for k, v in v1.items():  # nothing that existed changed meaning or type
        assert v2[k] == v and type(v2[k]) is type(v)


def test_v1_history_gets_period_default():
    v2 = migrate("history", load("history_v1.json"), dev="gh1")
    assert v2["period_s"] == 600


def test_v1_without_dev_needs_the_topic():
    with pytest.raises(ValueError, match="dev"):
        migrate("status", load("status_v1.json"))


def test_newer_schema_is_refused_not_guessed():
    with pytest.raises(ValueError, match="newer"):
        migrate("status", {**load("status_v2.json"), "schema": SCHEMA_VERSION + 1})


# --- validation ------------------------------------------------------------


def test_every_sample_classifies_as_the_chapter_says():
    text, bad = validate_files(sorted(SAMPLES.glob("*.json")), "gh1")
    assert bad == 2
    assert "BAD status_bad_units.json" in text
    assert "BAD history_bad_order.json" in text


def test_unit_change_in_place_is_caught_by_range():
    rep = validate("status", load("status_bad_units.json"))
    assert any(e.startswith("t: 274 outside") for e in rep.errors)
    assert any(e.startswith("fan: expected bool01") for e in rep.errors)


def test_unknown_fields_are_tolerated_and_listed():
    rep = validate("status", load("status_v3_from_future.json"))
    assert rep.ok and rep.unknown == ["nh3_ok", "nh3_ppm"]


def test_null_reading_is_a_valid_sensor_fault():
    rep = validate("status", load("status_v2_sensor_fault.json"))
    assert rep.ok


def test_null_where_not_allowed():
    rep = validate("status", {**load("status_v2.json"), "fan": None})
    assert "fan: null is not allowed" in rep.errors


def test_aggregate_order_and_event_context_limit():
    assert not validate("history", load("history_bad_order.json")).ok
    ev = {**load("event_v2.json"), "ctx": [1.0] * 61}
    assert any(e.startswith("ctx: 61") for e in validate("event", ev).errors)


def test_event_type_is_an_enum():
    rep = validate("event", {**load("event_v2.json"), "type": "fan_started"})
    assert any("not one of" in e for e in rep.errors)


def test_every_schema_has_the_common_identity_and_time():
    for kind, fields in SCHEMAS.items():
        assert "schema" in fields and "ts" in fields, kind


# --- the 11-byte frame -----------------------------------------------------


def test_frame_round_trip_including_nulls():
    rec = {
        "t": None,
        "h": 71.5,
        "soil": 38,
        "tank": None,
        "fan": 0,
        "valve": 1,
        "vbat_mv": 3812,
    }
    frame = encode_status_frame(rec, seq=117)
    assert len(frame) == FRAME_LEN == 11
    out = decode_status_frame(frame, "plot1", 1_760_400_000)
    assert out["t"] is None and out["tank"] is None
    assert (
        out["h"] == 71.5
        and out["soil"] == 38
        and out["valve"] == 1
        and out["seq"] == 117
    )
    assert validate("status", out).ok


def test_frame_rejects_wrong_length_and_version():
    with pytest.raises(ValueError, match="bytes"):
        decode_status_frame(b"\x01\x02", "plot1", 0)
    frame = bytearray(encode_status_frame({"fan": 0, "valve": 0}, seq=0))
    frame[0] = 2
    with pytest.raises(ValueError, match="version"):
        decode_status_frame(bytes(frame), "plot1", 0)


def test_missed_frames_wraps_at_256():
    assert missed_frames(117, 118) == 0
    assert missed_frames(117, 121) == 3
    assert missed_frames(254, 1) == 2


# --- settings --------------------------------------------------------------


def test_settings_are_clamped_and_version_echoed():
    rep = apply_settings(load("settings_desired.json"), None, 1_760_400_060)
    assert rep["version"] == 7
    assert (
        rep["t_on"] == LIMITS["t_on"][1]
        and rep["valve_max_s"] == LIMITS["valve_max_s"][1]
    )
    assert rep["clamped"] == ["t_on", "valve_max_s"]
    assert validate("settings_reported", rep).ok


def test_settings_keep_current_values_for_fields_not_sent():
    current = {"t_on": 30.0, "t_off": 27.0, "h_max": 70.0, "valve_max_s": 300}
    rep = apply_settings({"version": 8, "h_max": 75.0}, current, 1_760_400_060)
    assert rep["t_on"] == 30.0 and rep["valve_max_s"] == 300 and rep["h_max"] == 75.0
    assert rep["clamped"] == []


def test_hysteresis_band_is_enforced():
    rep = apply_settings(
        {"version": 9, "t_on": 26.0, "t_off": 26.0}, None, 1_760_400_060
    )
    assert rep["t_on"] - rep["t_off"] >= 1.0 and "t_off" in rep["clamped"]


# --- growth ----------------------------------------------------------------


def test_growth_overwrite_never_grows():
    rows = {
        g.name: g for g in growth_rows(json.loads((HERE / "streams.json").read_text()))
    }
    assert rows["status"].rows_per_year == 0 and rows["status"].steady_rows == 1
    assert rows["history 10-min"].rows_per_year == 144 * 365
    assert rows["raw 15-s samples (path A)"].steady_rows == float("inf")


def test_record_bytes_is_compact_json():
    assert record_bytes({"a": 1, "b": [1, 2]}) == len(b'{"a":1,"b":[1,2]}')
