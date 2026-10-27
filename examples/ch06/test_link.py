"""Tests for chapter 6's link tools."""

from __future__ import annotations

import json
from pathlib import Path

import link as L
import pytest

HERE = Path(__file__).parent
NODES = HERE / "nodes.json"
LINKS = HERE / "links.json"


# --- airtime ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("app_bytes", "sf", "expected_ms"),
    [
        (11, 7, 61.7),  # 24 B on air: the plot node's frame at SF7
        (11, 12, 1482.8),  # the same frame at SF12, low-data-rate optimisation on
        (0, 7, 46.3),  # a 13-byte LoRaWAN header alone
    ],
)
def test_airtime_matches_the_published_formula(app_bytes, sf, expected_ms):
    assert L.lorawan_airtime_s(app_bytes, sf) * 1000 == pytest.approx(
        expected_ms, abs=0.1
    )


def test_airtime_grows_with_sf_and_with_bytes():
    times = [L.lorawan_airtime_s(11, sf) for sf in L.EU868_SFS]
    assert times == sorted(times)
    assert L.lorawan_airtime_s(50, 9) > L.lorawan_airtime_s(11, 9)


def test_airtime_rejects_an_impossible_sf():
    with pytest.raises(ValueError, match="spreading factor"):
        L.lora_airtime_s(24, 5)


def test_fair_use_is_tighter_than_the_duty_cycle_at_every_sf():
    for sf in L.EU868_SFS:
        t = L.lorawan_airtime_s(11, sf)
        assert L.TTN_FAIR_USE_S_PER_DAY / t < 24 * L.DUTY_CYCLE_S_PER_HOUR / t


# --- protocol --------------------------------------------------------------


def test_mqtt_publish_matches_chapter_2s_count():
    # chapter 2's traffic tool: 84 B body on the wire is 132 B
    assert L.mqtt_publish_bytes(84) == 132


def test_a_handshake_per_request_costs_an_order_of_magnitude():
    assert L.https_new_connection_bytes(84) > 10 * L.https_keepalive_bytes(84) / 2
    assert L.https_keepalive_bytes(84) > 3 * L.mqtt_publish_bytes(84)


# --- budget ----------------------------------------------------------------


def nodes_by_id() -> dict[str, L.Node]:
    return {n.id: n for n in L.load_nodes(NODES)}


def links_by_id() -> tuple[float, dict[str, dict]]:
    frac, links = L.load_links(LINKS)
    return frac, {lk["id"]: lk for lk in links}


def test_plot_node_lasts_years_on_lorawan_and_months_on_wifi():
    frac, links = links_by_id()
    plot = nodes_by_id()["plot"]
    lora = L.battery_life_days(links["lorawan"], plot, frac)
    wifi = L.battery_life_days(links["wifi"], plot, frac)
    assert lora > 3 * 365
    assert wifi < 120
    assert lora / wifi > 10


def test_class_c_receiver_dominates_a_mains_lorawan_node():
    _, links = links_by_id()
    pump = nodes_by_id()["pump"]
    e = L.energy_per_day_mah(links["lorawan"], pump)
    assert e > 0.9 * links["lorawan"]["class_c_rx_ma"] * 24


def test_mains_nodes_have_no_battery_life():
    frac, links = links_by_id()
    gh1 = nodes_by_id()["gh1"]
    assert L.battery_life_days(links["wifi"], gh1, frac) is None


# --- select ----------------------------------------------------------------


def test_the_farm_monitor_gets_wifi_lorawan_lorawan():
    chosen = {c.node.id: c.link_id for c in L.select(L.load_nodes(NODES), LINKS)}
    assert chosen == {
        "gh1": "wifi",
        "gh2": "wifi",
        "plot": "lorawan",
        "pump": "lorawan",
    }


def test_rules_fire_in_order_with_a_reason():
    verdicts = {c.node.id: c.verdicts for c in L.select(L.load_nodes(NODES), LINKS)}
    assert verdicts["plot"]["wifi"].startswith("X1 reach")
    assert verdicts["gh1"]["lorawan"].startswith("X3 airtime")
    assert verdicts["plot"]["cellular"].startswith("X5 energy")


def test_the_plot_node_breaks_fair_use_at_sf9(tmp_path):
    data = json.loads(NODES.read_text())
    for n in data["nodes"]:
        if n["id"] == "plot":
            n["sf"] = 9
    p = tmp_path / "nodes.json"
    p.write_text(json.dumps(data))
    verdicts = {c.node.id: c.verdicts for c in L.select(L.load_nodes(p), LINKS)}
    assert verdicts["plot"]["lorawan"].startswith("X3 airtime")


def test_a_battery_node_hears_commands_only_when_it_wakes(tmp_path):
    data = json.loads(NODES.read_text())
    for n in data["nodes"]:
        if n["id"] == "plot":
            n["command_deadline_s"] = 60
    p = tmp_path / "nodes.json"
    p.write_text(json.dumps(data))
    verdicts = {c.node.id: c.verdicts for c in L.select(L.load_nodes(p), LINKS)}
    assert verdicts["plot"]["lorawan"].startswith("X4 downlink")


def test_a_frame_too_big_for_the_sf_is_excluded(tmp_path):
    data = json.loads(NODES.read_text())
    for n in data["nodes"]:
        if n["id"] == "pump":
            n["sf"] = 12
            n["messages"][0]["bytes"] = 60
    p = tmp_path / "nodes.json"
    p.write_text(json.dumps(data))
    verdicts = {c.node.id: c.verdicts for c in L.select(L.load_nodes(p), LINKS)}
    assert verdicts["pump"]["lorawan"].startswith("X2 payload")


# --- backoff ---------------------------------------------------------------


def test_schedule_doubles_and_caps():
    assert [L.backoff_delay(k, None) for k in range(5)] == [1, 2, 4, 8, 16]
    assert L.backoff_delay(20, None) == 300


def test_jitter_never_exceeds_the_ceiling():
    import random

    rng = random.Random(3)
    for k in range(12):
        assert 0 <= L.backoff_delay(k, rng) <= min(300, 2**k)


def test_the_herd_without_jitter_is_rejected_in_rounds():
    r = L.herd(1000, 50, jitter=False)
    assert r.peak_attempts_per_s == 1000
    assert r.rejected == 9500
    assert r.tall_s is not None and r.tall_s > 5000


def test_jitter_spreads_the_herd_inside_the_broker_capacity():
    r = L.herd(1000, 50, jitter=True)
    assert r.rejected == 0
    assert r.peak_attempts_per_s <= 50
    assert r.t99_s is not None and r.t99_s < 300


# --- CLI -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["airtime"],
        ["airtime", "--bytes", "84", "--markdown"],
        ["protocol"],
        ["budget", str(NODES), str(LINKS)],
        ["select", str(NODES), str(LINKS), "--markdown"],
        ["backoff", "--nodes", "100", "--capacity", "10"],
    ],
)
def test_cli_runs(argv, capsys):
    assert L.main(argv) == 0
    assert capsys.readouterr().out.strip()
