import json
from pathlib import Path

import pytest
from data_path import (
    Computation,
    load_computations,
    message_bytes,
    messages_per_day,
    payload_bytes,
    place_report,
    total_for,
    traffic_report,
)

HERE = Path(__file__).parent


def comp(**kw) -> Computation:
    base: dict = {
        "name": "x",
        "critical": False,
        "deadline_s": None,
        "inputs": "local",
        "changes": "rarely",
    }
    base.update(kw)
    return Computation(**base)


def test_critical_always_goes_to_device_even_with_remote_inputs():
    place, rule = comp(critical=True, inputs="multi-device").place(600)
    assert place == "device" and rule.startswith("R1")


def test_user_facing_goes_to_app():
    assert comp(inputs="user", deadline_s=1).place(600)[0] == "app"


def test_absence_cannot_be_detected_on_the_device():
    assert comp(inputs="absence", deadline_s=600).place(600)[0] == "cloud"


def test_multi_device_with_tight_deadline_gets_a_gateway():
    assert comp(inputs="multi-device", deadline_s=30).place(600)[0] == "gateway"
    assert comp(inputs="multi-device", deadline_s=60).place(600)[0] == "cloud"


def test_tight_deadline_with_local_inputs_stays_on_device():
    assert comp(deadline_s=1).place(600)[0] == "device"


def test_slow_and_often_changing_goes_to_cloud_otherwise_device():
    assert comp(changes="often").place(600)[0] == "cloud"
    assert comp(changes="rarely").place(600)[0] == "device"
    assert comp(changes="often", deadline_s=5).place(600)[0] == "device"


def test_every_critical_row_in_the_chapter_file_lands_on_the_device():
    outage, comps = load_computations(HERE / "computations.json")
    for c in comps:
        if c.critical:
            assert c.place(outage)[0] == "device", c.name


def test_payload_is_measured_compactly():
    s = {"example": {"t": 27.4, "h": 61.2}}
    assert payload_bytes(s) == len('{"t":27.4,"h":61.2}')
    assert payload_bytes({"bytes": 11}) == 11


def test_message_bytes_adds_mqtt_and_link_overhead():
    s = {"topic": "farm/gh1/status", "example": {"t": 1}}
    body = len('{"t":1}')
    assert message_bytes(s, 29) == body + 4 + len("farm/gh1/status") + 29
    assert message_bytes({"topic": "", "bytes": 11}, 13) == 24


def test_messages_per_day_from_period_or_count():
    assert messages_per_day({"period_s": 60}) == 1440
    assert messages_per_day({"per_day": 10}) == 10


def test_path_totals_and_offline_delay():
    path = {
        "name": "p",
        "link": "l",
        "link_overhead_bytes": 0,
        "status_stream": "s",
        "streams": [{"name": "s", "topic": "", "period_s": 60, "bytes": 10}],
    }
    t = total_for(path, 10)
    assert t.msgs_per_day == 1440
    assert t.bytes_per_day == 14400
    assert t.offline_delay_s == 600
    assert t.mb_per_month == pytest.approx(0.432)


def test_chapter_paths_rank_as_the_chapter_says():
    data = json.loads((HERE / "paths.json").read_text())
    totals = [total_for(p, data["offline_after_misses"]) for p in data["paths"]]
    a, b, c = totals
    assert a.bytes_per_day > 3 * b.bytes_per_day > 3 * c.bytes_per_day
    assert a.offline_delay_s < b.offline_delay_s < c.offline_delay_s


def test_reports_render_in_both_modes():
    for md in (False, True):
        assert "computations:" in place_report(HERE / "computations.json", md) or md
        assert "illustrative" in traffic_report(HERE / "paths.json", md)
    assert place_report(HERE / "computations.json", True).startswith("| Computation |")
