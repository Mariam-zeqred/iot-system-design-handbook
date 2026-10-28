"""Tests for chapter 8's cloud-side tools."""

from __future__ import annotations

import json
from pathlib import Path

import cloud as C
import pytest

HERE = Path(__file__).parent
FLEET = C.load(HERE / "fleet.json")
PRICES = C.load(HERE / "prices.json")
REQS = C.load(HERE / "requirements.json")


# --- fit -------------------------------------------------------------------


def test_fit_has_a_level_for_every_job_and_platform():
    rows = C.fit_rows(REQS)
    assert len(rows) == 12
    for r in rows:
        for p in C.PLATFORMS:
            assert r[p] in C.LEVELS
            assert r[p + "_how"]


def test_fit_counts_add_up_per_platform():
    rows = C.fit_rows(REQS)
    for counts in C.fit_counts(rows).values():
        assert sum(counts.values()) == len(rows)


def test_fit_rejects_an_unknown_level():
    bad = {
        "jobs": [
            {
                "job": "x",
                "firebase": ["maybe", "?"],
                "aws": ["native", ""],
                "self": ["native", ""],
            }
        ]
    }
    with pytest.raises(ValueError):
        C.fit_rows(bad)


def test_fit_prints_the_table(capsys):
    C.print_fit(REQS)
    out = capsys.readouterr().out
    assert "you build" in out and "Firebase RTDB" in out and "- ingest" in out


# --- usage -----------------------------------------------------------------


def test_class_counts_split_by_share_and_sum_to_the_fleet():
    assert C.class_counts(FLEET, 10) == [7, 3]
    assert C.class_counts(FLEET, 1000) == [667, 333]
    assert sum(C.class_counts(FLEET, 100_000)) == 100_000


def test_usage_counts_messages_from_chapter_3_rates():
    u = C.usage(FLEET, 1000)
    # 667 greenhouse nodes: 1440 + 144 + 10 + 1 a day; 333 plot nodes: 144 + 144 + 2 + 1 a day
    expected = (667 * (1440 + 144 + 10 + 1) + 333 * (144 + 144 + 2 + 1)) * 30
    assert u.uplink_msgs == expected
    assert u.history_rows == 1000 * 144 * 30
    assert u.status_writes == (667 * 1440 + 333 * 144) * 30


def test_usage_connections_are_wifi_nodes_plus_gateways():
    u = C.usage(FLEET, 1000)
    assert u.gateways == 34  # ceil(333 / 10)
    assert u.connections == 667 + 34


def test_usage_steady_state_storage_is_history_at_retention_plus_events():
    u = C.usage(FLEET, 1000)
    history = 1000 * 144 * 730 * (178 + 10)
    events = (667 * 10 + 333 * 2) * 365 * 2 * (254 + 24)
    latest = 1000 * (120 + 142)
    assert u.stored_bytes == pytest.approx(history + events + latest)
    assert u.stored_hot_bytes < u.stored_bytes


def test_usage_scales_linearly_in_the_fleet():
    a, b = C.usage(FLEET, 1000), C.usage(FLEET, 100_000)
    assert b.uplink_msgs == pytest.approx(100 * a.uplink_msgs)
    assert b.stored_bytes == pytest.approx(100 * a.stored_bytes)
    assert b.apps == 100 * a.apps


def test_usage_of_an_empty_fleet_is_zero():
    u = C.usage(FLEET, 0)
    assert (
        u.uplink_msgs == 0
        and u.stored_bytes == 0
        and u.apps == 0
        and u.connections == 0
    )


# --- pricing helpers -------------------------------------------------------


def test_tiered_pricing_charges_each_band_at_its_rate():
    tiers = [[1_000_000_000, 1.0], [5_000_000_000, 0.8], [None, 0.7]]
    assert C.tiered(500e6, tiers) == pytest.approx(500.0)
    assert C.tiered(1e9, tiers) == pytest.approx(1000.0)
    assert C.tiered(3e9, tiers) == pytest.approx(1000.0 + 2000 * 0.8)
    assert C.tiered(6e9, tiers) == pytest.approx(1000.0 + 4000 * 0.8 + 1000 * 0.7)
    assert C.tiered(0, tiers) == 0.0


def test_every_price_carries_a_source_and_a_verified_flag():
    for key, block in PRICES.items():
        if isinstance(block, dict) and "source" in block:
            assert isinstance(block["verified"], bool), key
            if block["verified"]:
                assert "Price List API" in block["source"], key
            else:
                assert "memory" in block["source"], key


def test_the_unfetched_prices_are_exactly_firebase_and_egress():
    unverified = sorted(
        k
        for k, v in PRICES.items()
        if isinstance(v, dict) and v.get("verified") is False
    )
    assert unverified == ["aws_data_transfer", "firebase_rtdb"]


# --- bills -----------------------------------------------------------------


@pytest.mark.parametrize("n", [10, 1000, 100_000])
def test_each_bill_total_is_the_sum_of_its_lines(n):
    _, bs = C.bills(FLEET, PRICES, n)
    for b in bs:
        assert b.total == pytest.approx(sum(ln.usd for ln in b.lines))
        assert all(ln.usd >= 0 for ln in b.lines)


def test_firebase_bill_is_dominated_by_history_storage_at_a_thousand():
    _, bs = C.bills(FLEET, PRICES, 1000)
    fb = bs[0]
    storage = next(ln for ln in fb.lines if ln.item.startswith("storage"))
    assert storage.usd / fb.total > 0.95


def test_firebase_charges_nothing_for_device_writes():
    _, bs = C.bills(FLEET, PRICES, 1000)
    writes = next(ln for ln in bs[0].lines if ln.item.startswith("device writes"))
    assert writes.usd == 0


def test_ten_devices_fit_the_spark_plan_and_a_thousand_do_not():
    _, bs = C.bills(FLEET, PRICES, 10)
    assert any("Spark (free) plan" in w for w in bs[0].warnings)
    _, bs = C.bills(FLEET, PRICES, 1000)
    assert any("Blaze plan required" in w for w in bs[0].warnings)
    assert not any("Spark (free)" in w for w in bs[0].warnings)


def test_a_hundred_thousand_devices_stay_under_one_rtdb_instance_but_three_hundred_thousand_do_not():
    _, bs = C.bills(FLEET, PRICES, 100_000)
    assert not any("shard" in w for w in bs[0].warnings)
    _, bs = C.bills(FLEET, PRICES, 300_000)
    assert any("shard" in w for w in bs[0].warnings)


def test_aws_messages_include_deliveries_to_phones():
    u, bs = C.bills(FLEET, PRICES, 1000)
    msgs = next(ln for ln in bs[1].lines if ln.item.startswith("IoT Core messages"))
    assert msgs.qty == pytest.approx(
        u.uplink_msgs + u.app_status_deliveries + u.desired_writes
    )


def test_aws_status_driven_lines_are_most_of_its_bill_at_a_thousand():
    _, bs = C.bills(FLEET, PRICES, 1000)
    aws = bs[1]
    per_message = sum(
        ln.usd
        for ln in aws.lines
        if ln.item.startswith(
            ("IoT Core messages", "rules triggered", "rule actions", "DynamoDB writes")
        )
    )
    assert per_message / aws.total > 0.9


def test_aws_message_tiers_apply_at_a_hundred_thousand():
    _, bs = C.bills(FLEET, PRICES, 100_000)
    msgs = next(ln for ln in bs[1].lines if ln.item.startswith("IoT Core messages"))
    assert msgs.qty > 1e9
    assert msgs.usd < msgs.qty / 1e6 * 1.0  # cheaper than the first tier alone
    assert msgs.usd > msgs.qty / 1e6 * 0.7


def test_firehose_rounds_each_record_up_to_five_kilobytes():
    u, bs = C.bills(FLEET, PRICES, 1000)
    fh = next(ln for ln in bs[1].lines if ln.item.startswith("Firehose"))
    assert fh.qty == pytest.approx((u.history_rows + u.event_rows) * 5120 / 1e9)


def test_dynamodb_storage_is_free_under_25_gb():
    _, bs = C.bills(FLEET, PRICES, 1000)
    ddb = next(ln for ln in bs[1].lines if ln.item.startswith("DynamoDB storage"))
    assert ddb.qty < 25 and ddb.usd == 0
    _, bs = C.bills(FLEET, PRICES, 100_000)
    ddb = next(ln for ln in bs[1].lines if ln.item.startswith("DynamoDB storage"))
    assert ddb.usd == pytest.approx((ddb.qty - 25) * 0.25)


def test_self_hosted_tier_follows_the_fleet_size():
    assert C.pick_tier(FLEET, 10)["instance"] == "t4g.small"
    assert C.pick_tier(FLEET, 1000)["instance"] == "t4g.medium"
    assert C.pick_tier(FLEET, 100_000)["count"] == 2


def test_self_hosted_hours_are_reported_not_priced():
    _, bs = C.bills(FLEET, PRICES, 1000)
    assert bs[2].hours == 8
    assert not any("hour" in ln.item and "operation" in ln.item for ln in bs[2].lines)


def test_self_hosted_disk_has_a_floor_and_grows_with_compressed_data():
    _, bs = C.bills(FLEET, PRICES, 1000)
    disk = next(ln for ln in bs[2].lines if ln.item.startswith("EBS"))
    assert disk.qty == 20
    _, bs = C.bills(FLEET, PRICES, 100_000)
    disk = next(ln for ln in bs[2].lines if ln.item.startswith("EBS"))
    assert disk.qty == pytest.approx(2125.16 / 10 * 1.5, abs=1)


def test_the_cheapest_platform_changes_with_the_fleet():
    totals = {}
    for n in (10, 1000, 100_000):
        _, bs = C.bills(FLEET, PRICES, n)
        totals[n] = {b.platform: b.total for b in bs}
    assert min(totals[10], key=totals[10].get) in (
        "firebase",
        "aws",
    )  # both under a dollar
    assert totals[1000]["self"] < totals[1000]["aws"] < totals[1000]["firebase"]
    assert (
        totals[100_000]["self"] < totals[100_000]["aws"] < totals[100_000]["firebase"]
    )


def test_the_chapter_table_numbers():
    """The three summary rows quoted in the chapter, to the cent."""
    rows = {}
    for n in (10, 1000, 100_000):
        _, bs = C.bills(FLEET, PRICES, n)
        rows[n] = tuple(round(b.total, 2) for b in bs)
    assert rows[10] == (1.07, 0.81, 13.75)
    assert rows[1000] == (106.71, 73.88, 25.94)
    assert rows[100_000] == (10671.43, 6887.69, 147.05)


# --- what-if ---------------------------------------------------------------


def test_what_if_slows_status_but_never_speeds_it_up():
    slow = C.what_if(FLEET, status_period_s=300)
    assert slow["classes"][0]["streams"][0]["per_day"] == 288
    assert (
        slow["classes"][1]["streams"][0]["per_day"] == 144
    )  # the plot node already sends every 600 s
    fast = C.what_if(FLEET, status_period_s=10)
    assert fast["classes"][0]["streams"][0]["per_day"] == 1440


def test_what_if_retention_cuts_firebase_storage_tenfold_or_more():
    _, base = C.bills(FLEET, PRICES, 1000)
    _, short = C.bills(C.what_if(FLEET, history_retention_days=30), PRICES, 1000)
    assert short[0].total < base[0].total / 8


def test_what_if_status_period_cuts_the_aws_bill_but_not_firebase():
    _, base = C.bills(FLEET, PRICES, 1000)
    _, slow = C.bills(C.what_if(FLEET, status_period_s=300), PRICES, 1000)
    assert slow[1].total < base[1].total / 2
    assert slow[0].total == pytest.approx(base[0].total, rel=0.01)


def test_what_if_does_not_mutate_the_input():
    before = json.dumps(FLEET, sort_keys=True)
    C.what_if(FLEET, status_period_s=300, history_retention_days=30)
    assert json.dumps(FLEET, sort_keys=True) == before


# --- fanout ----------------------------------------------------------------


def test_fanout_listener_on_the_device_node_costs_thousands_of_times_more():
    rows = C.fanout_rows(FLEET, PRICES)
    a, b, c = rows[0], rows[1], rows[2]
    assert (
        a["placement"].startswith("A")
        and b["placement"].startswith("B")
        and c["placement"].startswith("C")
    )
    assert b["firebase_gb"] / a["firebase_gb"] > 1000
    assert c["firebase_gb"] / a["firebase_gb"] > 100
    assert b["firebase_usd"] == pytest.approx(b["firebase_gb"] * 1.0)


def test_fanout_placement_a_is_cents_on_every_platform():
    rows = C.fanout_rows(FLEET, PRICES)
    assert rows[0]["firebase_usd"] < 0.05
    assert rows[3]["firebase_usd"] < 0.05
    assert rows[4]["firebase_usd"] < 0.05


# --- rules -----------------------------------------------------------------


def test_firebase_rules_cover_the_five_rows_of_chapter_7():
    r = C.rules_firebase()["rules"]["devices"]["$id"]
    assert "auth.token.deviceId === $id" in r["status"][".write"]
    assert "disabled" in r["status"][".write"]
    assert r["status"]["$other"][".validate"] is False
    for f in C.STATUS_FIELDS:
        assert f in r["status"][".validate"]
    assert "!data.exists()" in r["history"]["$ts"][".write"]
    assert "owners" in r["settings"]["desired"][".write"]
    assert ".write" not in r["owners"] and ".write" not in r["disabled"]
    assert (
        ".read" not in C.rules_firebase()["rules"]
        and ".write" not in C.rules_firebase()["rules"]
    )


def test_firebase_rules_are_valid_json():
    json.loads(json.dumps(C.rules_firebase()))


def test_aws_policy_binds_every_resource_to_the_connecting_thing():
    pol = C.rules_aws()
    thing = "${iot:Connection.Thing.ThingName}"
    for st in pol["Statement"]:
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert all(thing in r for r in res), st["Sid"]
    publish = next(st for st in pol["Statement"] if st["Sid"] == "PublishOwnStreams")
    assert any(r.endswith("/status") for r in publish["Resource"])
    assert any(r.endswith("/history") for r in publish["Resource"])
    assert any(r.endswith("/events") for r in publish["Resource"])
    assert not any(
        "*" in r
        for st in pol["Statement"]
        for r in (
            [st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]
        )
    )


def test_mosquitto_acl_uses_the_username_pattern_for_every_device_topic():
    acl = C.rules_mosquitto()
    for stream in ("status", "history", "events", "settings/reported"):
        assert f"pattern write farm/%u/{stream}" in acl
    assert "pattern read  farm/%u/settings/desired" in acl
    assert "pattern write farm/%u/settings/desired" not in acl


def test_rules_map_has_three_dialects_for_each_row():
    rows = {}
    for row, d, _ in C.RULES_MAP:
        rows.setdefault(row, set()).add(d)
    assert len(rows) == 5
    assert all(ds == {"firebase", "aws", "mosquitto"} for ds in rows.values())


# --- command line ----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["fit", str(HERE / "requirements.json")],
        [
            "cost",
            str(HERE / "fleet.json"),
            str(HERE / "prices.json"),
            "--devices",
            "300",
        ],
        ["cost", "--status-period-s", "300", "--devices", "1000"],
        ["fanout", str(HERE / "fleet.json"), str(HERE / "prices.json")],
        ["rules", "firebase"],
        ["rules", "aws"],
        ["rules", "mosquitto"],
        ["rules", "map"],
    ],
)
def test_every_subcommand_runs(argv, capsys):
    assert C.main(argv) == 0
    assert capsys.readouterr().out.strip()


def test_cost_output_names_the_unfetched_prices(capsys):
    C.main(["cost", "--devices", "10"])
    out = capsys.readouterr().out
    assert "from memory, not fetched: firebase_rtdb, aws_data_transfer" in out
    assert "Not costed" in out
