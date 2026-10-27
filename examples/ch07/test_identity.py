"""Tests for chapter 7's identity tools."""

from __future__ import annotations

import json
from pathlib import Path

import identity as I
import pytest

HERE = Path(__file__).parent


# --- inventory -------------------------------------------------------------


def test_the_farm_monitor_inventory_has_no_gaps():
    rows = I.inventory_rows(I.load_inventory(HERE / "inventory.json"))
    assert rows and all(r["verdict"] == "ok" for r in rows)


def test_the_real_systems_inventory_gaps_are_the_eight_the_chapter_names():
    rows = I.inventory_rows(I.load_inventory(HERE / "inventory_real.json"))
    gaps = [r for r in rows if r["verdict"] == "gap"]
    assert len(gaps) == 8
    assert all(r["rule"] == "H2" for r in gaps)
    assert all(r["lives_in"] in ("source", "image") for r in gaps)
    assert all("fix" in r for r in gaps)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            {
                "kind": "identifier",
                "scope": "fleet",
                "holder": "device",
                "lives_in": "source",
            },
            ("ok", "H1"),
        ),
        (
            {
                "kind": "secret",
                "scope": "device",
                "holder": "device",
                "lives_in": "image",
            },
            ("gap", "H2"),
        ),
        (
            {"kind": "secret", "scope": "fleet", "holder": "device", "lives_in": "nvs"},
            ("gap", "H3"),
        ),
        (
            {
                "kind": "secret",
                "scope": "device",
                "holder": "device",
                "lives_in": "label",
            },
            ("gap", "H4"),
        ),
        (
            {
                "kind": "secret",
                "scope": "device",
                "holder": "device",
                "lives_in": "nvs",
            },
            ("ok", "H4"),
        ),
        (
            {
                "kind": "secret",
                "scope": "fleet",
                "holder": "person",
                "lives_in": "label",
            },
            ("gap", "H5"),
        ),
        (
            {
                "kind": "secret",
                "scope": "device",
                "holder": "person",
                "lives_in": "label",
            },
            ("ok", "H5"),
        ),
        (
            {
                "kind": "secret",
                "scope": "fleet",
                "holder": "server",
                "lives_in": "server",
            },
            ("ok", "H2"),
        ),
    ],
)
def test_the_home_rules(item, expected):
    assert I.judge({"name": "x", **item}) == expected


def test_inventory_rejects_an_unknown_home(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "system": "x",
                "items": [
                    {
                        "name": "k",
                        "kind": "secret",
                        "scope": "fleet",
                        "holder": "device",
                        "lives_in": "cloud",
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="lives_in"):
        I.load_inventory(bad)


# --- blast -----------------------------------------------------------------


def design(design_id: str) -> I.Design:
    return next(d for d in I.DESIGNS if d.id == design_id)


def test_a_compiled_in_secret_is_lost_with_the_repo_or_the_image():
    for event in ("repo", "image"):
        assert I.exposure(design("S-image"), event) == "whole fleet"
        assert I.exposure(design("S-nvs"), event) == "nothing"
        assert I.exposure(design("T-nvs"), event) == "nothing"


def test_a_stolen_unit_gives_the_fleet_only_under_a_shared_secret():
    assert I.exposure(design("S-nvs"), "stolen") == "whole fleet"
    assert I.exposure(design("T-nvs"), "stolen").startswith("one device")
    assert "until revoked" in I.exposure(design("K-se"), "stolen")


def test_mitm_only_pays_when_tls_is_unverified_and_the_credential_is_a_bearer():
    assert I.exposure(design("S-image"), "mitm") == "whole fleet"
    assert I.exposure(design("S-image-tls"), "mitm").startswith("nothing")
    assert I.exposure(design("K-se"), "mitm").startswith("nothing")


def test_the_server_is_the_fleet_in_every_design():
    assert all(I.exposure(d, "server") == "whole fleet" for d in I.DESIGNS)


def test_recovery_is_one_device_only_for_per_device_designs():
    assert I.recovery(design("T-nvs")).startswith("revoke that device")
    assert I.recovery(design("S-image")).startswith(
        "rotate the secret: a new image on every unit"
    )
    assert I.recovery(design("S-nvs")).startswith("rotate the secret: re-provision")


# --- provision -------------------------------------------------------------


def make_server(start: int = 1_700_000_000, key: bytes = b"k"):
    now = [start]
    return I.Server(key, lambda: now[0]), now


def test_every_scenario_matches_its_expectation():
    rows = I.scenarios()
    assert len(rows) == 14
    assert [r["allowed"] for r in rows] == [r["expect"] for r in rows]


def test_a_token_is_rejected_when_tampered_or_signed_with_another_key():
    server, _ = make_server()
    other, _ = make_server(key=b"other")
    token = server.user_token("u")
    assert server.verify(token) == {
        "uid": "u",
        "iat": 1_700_000_000,
        "exp": 1_700_003_600,
    }
    assert other.verify(token) is None
    payload, sig = token.split(".")
    assert server.verify(payload + "." + sig[:-2] + "AA") is None
    assert server.verify("not-a-token") is None


def test_id_tokens_expire_and_refresh_tokens_renew_them():
    server, now = make_server()
    dev = I.Device("d1")
    dev.provision(server.claim("d1", "u"), server)
    assert server.verify(dev.id_token)["deviceId"] == "d1"
    now[0] += I.ID_TOKEN_LIFETIME_S
    assert server.verify(dev.id_token) is None
    dev.ensure_token(server)
    assert server.verify(dev.id_token)["exp"] == now[0] + I.ID_TOKEN_LIFETIME_S


def test_a_custom_token_cannot_be_used_as_an_id_token():
    server, _ = make_server()
    custom = server.claim("d1", "u")
    claims = server.verify(custom)
    assert claims["kind"] == "custom" and "exp" not in claims
    # the rules never see a custom token: the device must exchange it first
    with pytest.raises(PermissionError):
        server.exchange(server.user_token("u"))


def test_revocation_stops_refresh_and_the_rules_see_disabled():
    server, now = make_server()
    dev = I.Device("d1")
    dev.provision(server.claim("d1", "u"), server)
    server.revoke_device("d1")
    auth = server.verify(dev.id_token)
    allowed, why = I.rules(
        auth,
        "write",
        "/devices/d1/status",
        {"ts": 1, "temp": 1, "hum": 1, "heater": False},
        server.db,
    )
    assert not allowed and "disabled" in why
    now[0] += I.ID_TOKEN_LIFETIME_S
    with pytest.raises(PermissionError):
        dev.ensure_token(server)


@pytest.mark.parametrize(
    ("auth", "op", "path", "value", "expected"),
    [
        (None, "read", "/devices/d1/status", None, False),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "write",
            "/devices/d1/status",
            {"ts": 1, "temp": 1, "hum": 1, "heater": True},
            True,
        ),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "write",
            "/devices/d1/status",
            {"ts": 1, "temp": 1},
            False,
        ),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "write",
            "/devices/d2/status",
            {"ts": 1, "temp": 1, "hum": 1, "heater": True},
            False,
        ),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "write",
            "/devices/d1/settings/desired",
            {"target": 30},
            False,
        ),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "read",
            "/devices/d1/settings/desired",
            None,
            True,
        ),
        (
            {"uid": "dev-d1", "device": True, "deviceId": "d1"},
            "read",
            "/devices/d1/history",
            None,
            False,
        ),
        (
            {"uid": "owner"},
            "write",
            "/devices/d1/settings/desired",
            {"target": 30},
            True,
        ),
        (
            {"uid": "owner"},
            "write",
            "/devices/d1/status",
            {"ts": 1, "temp": 1, "hum": 1, "heater": True},
            False,
        ),
        ({"uid": "owner"}, "write", "/devices/d1/owners/friend", True, False),
        ({"uid": "stranger"}, "read", "/devices/d1/status", None, False),
        ({"uid": "owner"}, "write", "/", {}, False),
        ({"uid": "owner"}, "read", "/users/owner", None, False),
    ],
)
def test_least_privilege_rules(auth, op, path, value, expected):
    db = {"devices": {"d1": {"owners": {"owner": True}}, "d2": {}}}
    allowed, _ = I.rules(auth, op, path, value, db)
    assert allowed is expected


def test_history_is_append_only():
    db = {"devices": {"d1": {"history": {"100": {}}}}}
    auth = {"uid": "dev-d1", "device": True, "deviceId": "d1"}
    assert I.rules(auth, "write", "/devices/d1/history/101", {"ts": 1}, db)[0]
    assert not I.rules(auth, "write", "/devices/d1/history/100", {"ts": 1}, db)[0]


# --- rotate ----------------------------------------------------------------


def test_rotation_costs():
    r = I.rotation(design("S-image"), 1000, 14, 180, 1)
    assert r == {
        "touch": 1000,
        "leak_usable_days": 14,
        "offline_device_days_if_cut_now": 7000,
    }
    r = I.rotation(design("S-nvs"), 1000, 14, 180, 1)
    assert r["touch"] == 1000 and r["leak_usable_days"] == 180
    r = I.rotation(design("T-nvs"), 1000, 14, 180, 1)
    assert r["touch"] == 1 and r["leak_usable_days"] == pytest.approx(1 / 24)
    assert I.rotation(design("K-se"), 1000, 14, 180, 1)["leak_usable_days"] == 0


# --- scan ------------------------------------------------------------------


def test_scan_finds_the_four_planted_secrets_and_two_identifiers():
    findings = I.scan_tree(HERE / "samples")
    secrets = [f for f in findings if f["kind"] == "secret"]
    assert len(secrets) == 4
    assert {f["what"] for f in secrets} == {
        "C define",
        "auth query parameter",
        "assignment",
    }
    assert all(f["file"] != "secrets.example.h" for f in secrets)
    assert sum(f["kind"] == "identifier" for f in findings) == 2


@pytest.mark.parametrize(
    "line",
    [
        '#define WIFI_PASS "YOUR_WIFI_PASSWORD"',
        '#define TOKEN "PASTE-HERE"',
        "DB_PASSWORD=change-me",
        "password: <set on first login>",
        'api_key = ""',
        "MAINTENANCE_PHONE 0000000000",
    ],
)
def test_scan_ignores_placeholders(line):
    assert I.scan_text(line) == []


@pytest.mark.parametrize(
    "line",
    [
        '#define FIREBASE_AUTH "AbCdEf0123456789AbCdEf0123456789AbCdEf01"',
        "curl 'https://x.firebaseio.com/a.json?auth=AbCdEf0123'",
        "mqtt_password = s3cr3t-value",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_scan_flags_real_looking_values(line):
    assert [f["kind"] for f in I.scan_text(line) if f["kind"] == "secret"] == ["secret"]


def test_scan_skips_excluded_directories(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "x.h").write_text('#define OTA_PASS "real-looking"\n')
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert I.scan_tree(tmp_path) and I.scan_tree(tmp_path, exclude=("vendor",)) == []


# --- CLI -------------------------------------------------------------------


def test_cli_exit_codes(capsys):
    assert I.main(["inventory", str(HERE / "inventory.json")]) == 0
    assert I.main(["blast"]) == 0
    assert I.main(["provision"]) == 0
    assert I.main(["rotate", "--fleet", "10"]) == 0
    assert I.main(["scan", str(HERE / "samples")]) == 1
    assert I.main(["scan", str(HERE / "samples"), "--exclude", "samples"]) == 0
    out = capsys.readouterr().out
    assert "14 scenarios, 0 mismatches" in out
