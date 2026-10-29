"""Tests for chapter 9's fleet tools."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import fleet as F
import pytest

HERE = Path(__file__).parent
SIGNALS = F.load(HERE / "signals.json")
SNAPSHOT = F.load(HERE / "status_snapshot.json")
REPORT = F.health(SNAPSHOT)


# --- signals ---------------------------------------------------------------


def test_signal_rows_have_a_level_and_a_sentence_per_shape():
    rows = F.signal_rows(SIGNALS)
    assert len(rows) == 9
    for r in rows:
        for p in F.SHAPES:
            assert r[p] in F.LEVELS
            assert r[p + "_how"]


def test_signal_counts_add_up_per_shape():
    rows = F.signal_rows(SIGNALS)
    for counts in F.signal_counts(rows).values():
        assert sum(counts.values()) == len(rows)


def test_signals_reject_an_unknown_level():
    bad = {
        "signals": [
            {
                "signal": "x",
                "question": "?",
                "firebase": ["maybe", ""],
                "aws": ["native", ""],
                "self": ["native", ""],
            }
        ]
    }
    with pytest.raises(ValueError):
        F.signal_rows(bad)


def test_signals_print(capsys):
    F.print_signals(SIGNALS)
    out = capsys.readouterr().out
    assert "last will" in out and "you build" in out and "- OTA state" in out


# --- synth and health ------------------------------------------------------


def test_synth_is_deterministic_and_labelled_synthetic():
    a = F.synth_snapshot(7, 60)
    assert a == F.synth_snapshot(7, 60)
    assert a["_meta"]["synthetic"] is True
    assert "synth --seed 7" in a["_meta"]["generator"]
    assert len(a["status"]) == 60
    assert a != F.synth_snapshot(8, 60)


def test_committed_snapshot_is_the_generator_output():
    assert F.synth_snapshot(7, 60) == SNAPSHOT


def test_status_records_carry_chapter_3_and_chapter_9_fields():
    for r in SNAPSHOT["status"]:
        for k in (
            "schema",
            "dev",
            "ts",
            "rssi",
            "fw",
            "seq",
            "up_s",
            "rst",
            "boots",
            "heap_min",
        ):
            assert k in r, k
        assert r["rst"] in F.RESET_REASONS
        assert "_missed" not in r
        F.parse_version(r["fw"])


def test_presence_uses_a_window_per_device_class():
    assert {x["dev"] for x in REPORT["offline"]} == {"gh07", "gh12", "pl03"}
    assert REPORT["online"] == 57
    pl05 = next(x for x in REPORT["rows"] if x["dev"] == "pl05")
    assert pl05["age"] > 600 and pl05["online"] and pl05["window"] == 6000
    assert REPORT["offline"][0]["dev"] == "gh07"  # longest silence first


def test_versions_behind_the_current_one_are_listed_with_their_presence():
    assert REPORT["by_fw"] == {"1.5.0": 54, "1.4.0": 6}
    behind = {x["dev"]: x["online"] for x in REPORT["behind"]}
    assert set(behind) == {"gh01", "gh02", "gh03", "gh07", "pl01", "pl02"}
    assert behind["gh07"] is False
    assert F.health(SNAPSHOT, current="1.4.0")["behind"] == []


def test_reboots_in_the_last_day_and_loop_suspects():
    assert REPORT["rebooted_24h"] == 4
    assert REPORT["reasons"] == {"wdt": 1, "panic": 1, "brownout": 1, "sw": 1}
    assert {x["dev"] for x in REPORT["loop_suspects"]} == {"gh20", "gh21"}
    gh30 = next(x for x in REPORT["rows"] if x["dev"] == "gh30")
    assert gh30["rebooted_24h"] and not gh30["loop_suspect"]


def test_delivery_is_bounded_and_weak_links_are_the_planted_ones():
    assert all(0.0 <= x["delivery"] <= 1.0 for x in REPORT["rows"])
    assert [x["dev"] for x in REPORT["weak"]] == ["gh36", "gh34", "gh35", "gh33"]
    assert 0.97 < REPORT["fleet_delivery"] < 0.99


def test_low_heap_is_the_planted_unit():
    assert [x["dev"] for x in REPORT["low_heap"]] == ["gh40"]


def test_now_override_moves_every_unit_out_of_its_window():
    later = F.health(SNAPSHOT, now=SNAPSHOT["_meta"]["now"] + 2 * 3600)
    assert later["online"] == 0


def test_health_prints_the_five_lines(capsys):
    F.print_health(REPORT)
    out = capsys.readouterr().out
    for word in (
        "presence",
        "firmware",
        "reboots",
        "delivery",
        "memory",
        "synthetic",
        "gh07",
    ):
        assert word in out


def test_parse_version_is_strict():
    assert F.parse_version("1.4.0") == (1, 4, 0)
    assert F.parse_version("1.10.0") > F.parse_version("1.9.9")
    for bad in ("1.4.0-ota", "1.4", "v1.4.0", "1.4.0.1", "", "a.b.c"):
        with pytest.raises(ValueError):
            F.parse_version(bad)


def test_device_class_rejects_an_unknown_prefix():
    with pytest.raises(ValueError):
        F.device_class("xx01")


# --- presence --------------------------------------------------------------


def test_short_outage_only_the_raw_will_notifies():
    d = F.detect(30.0, 10.0, 60.0, 600.0, 300.0)
    assert d[F.DETECTORS[0]] is None
    assert d[F.DETECTORS[1]] == 30.0  # published at the reconnect: session take-over
    assert (
        d[F.DETECTORS[2]] is None
        and d[F.DETECTORS[3]] is None
        and d[F.DETECTORS[4]] is None
    )


def test_long_outage_delays_follow_the_arithmetic():
    d = F.detect(3600.0, 20.0, 60.0, 600.0, 300.0)
    assert d[F.DETECTORS[0]] == 580.0  # window minus phase
    assert d[F.DETECTORS[1]] == 430.0  # 1.5 keep-alive minus phase
    assert d[F.DETECTORS[2]] == 490.0
    assert d[F.DETECTORS[3]] == 580.0  # the hold-down brings it back to N3
    assert d[F.DETECTORS[4]] is None


def test_will_with_hold_down_to_n3_is_the_status_age_detector():
    rng = random.Random(5)
    for _ in range(2000):
        d, phase = rng.lognormvariate(5.0, 1.5), rng.uniform(0.0, 60.0)
        r = F.detect(d, phase, 60.0, 600.0, 300.0)
        assert (r[F.DETECTORS[0]] is None) == (r[F.DETECTORS[3]] is None)
        if r[F.DETECTORS[0]] is not None:
            assert abs(r[F.DETECTORS[0]] - r[F.DETECTORS[3]]) < 1e-9


def test_presence_simulation_counts_are_consistent():
    res = F.simulate_presence(F.PresenceParams(devices=200, seed=3))
    det = res["detectors"]
    for c in det.values():
        assert c["notified"] == c["wanted"] + c["noise"]
        assert c["missed"] == res["wanted"] - c["wanted"]
    assert det[F.DETECTORS[1]]["notified"] == res["outages"]
    assert (
        det[F.DETECTORS[4]]["notified"] == 0
        and det[F.DETECTORS[4]]["missed"] == res["wanted"]
    )
    assert det[F.DETECTORS[0]]["missed"] == 0
    assert det[F.DETECTORS[2]]["noise"] <= det[F.DETECTORS[1]]["noise"]


def test_poisson_draws_have_the_requested_mean():
    rng = random.Random(1)
    n = 20000
    assert abs(sum(F._poisson(rng, 3.0) for _ in range(n)) / n - 3.0) < 0.1


def test_presence_prints(capsys):
    F.print_presence(F.simulate_presence(F.PresenceParams(devices=50, seed=2)))
    out = capsys.readouterr().out
    assert "device's own online flag" in out and "never" in out


# --- Ed25519 ---------------------------------------------------------------

RFC_8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


@pytest.mark.parametrize("sk, pk, msg, sig", RFC_8032)
def test_ed25519_matches_rfc_8032_test_vectors(sk, pk, msg, sig):
    sk_b, msg_b = bytes.fromhex(sk), bytes.fromhex(msg)
    assert F.ed25519_publickey(sk_b).hex() == pk
    assert F.ed25519_sign(msg_b, sk_b, bytes.fromhex(pk)).hex() == sig
    assert F.ed25519_verify(bytes.fromhex(sig), msg_b, bytes.fromhex(pk))


def test_ed25519_rejects_tampering_wrong_keys_and_malformed_input():
    sk = bytes(range(32))
    pk = F.ed25519_publickey(sk)
    sig = F.ed25519_sign(b"manifest", sk, pk)
    assert F.ed25519_verify(sig, b"manifest", pk)
    assert not F.ed25519_verify(sig, b"manifesT", pk)
    assert not F.ed25519_verify(sig, b"manifest", F.ed25519_publickey(bytes(32)))
    assert not F.ed25519_verify(sig[:-1], b"manifest", pk)
    assert not F.ed25519_verify(sig, b"manifest", pk[:-1])
    big_s = sig[:32] + (F._l).to_bytes(32, "little")
    assert not F.ed25519_verify(big_s, b"manifest", pk)
    with pytest.raises(ValueError):
        F.ed25519_publickey(b"short")


# --- manifest and the device's checks ---------------------------------------


def _signed(sk: bytes, **kw) -> tuple[dict, bytes]:
    image = F.make_image(kw.get("version", "1.5.0"))
    return F.sign_manifest(
        F.make_manifest(image, **{"version": "1.5.0", **kw}), sk
    ), image


def test_canonical_bytes_ignore_key_order_whitespace_and_extra_fields():
    image = F.make_image("1.5.0")
    m = F.make_manifest(image, "1.5.0")
    shuffled = dict(reversed(list(m.items())))
    shuffled["notes"] = "not signed"
    assert F.canonical(m) == F.canonical(shuffled)
    assert b" " not in F.canonical(m)
    with pytest.raises(ValueError):
        F.canonical({k: v for k, v in m.items() if k != "sha256"})


def test_good_manifest_passes_all_eight_checks():
    sk = bytes(range(32))
    signed, image = _signed(sk)
    results = F.device_checks(
        signed, F.ed25519_publickey(sk), F.Unit(), image, healthy=True
    )
    assert [s for _, s, _ in results] == ["pass"] * 8
    assert F.verdict(results) == ("FLASHED", "all eight")


def test_each_candidate_is_decided_by_the_named_check():
    rows = F.checks_demo(seed=1)
    assert [(r["outcome"], r["by"]) for r in rows] == [
        ("FLASHED", "all eight"),
        ("REFUSE", "C7 digest"),
        ("REFUSE", "C1 signature"),
        ("REFUSE", "C1 signature"),
        ("REFUSE", "C2 newer"),
        ("REFUSE", "C2 newer"),
        ("REFUSE", "C3 hardware"),
        ("REFUSE", "C4 stepping stone"),
        ("REFUSE", "C5 size"),
        ("REFUSE", "C6 attempts"),
        ("ROLLBACK", "C8 health"),
    ]


def test_checks_stop_at_the_first_refusal():
    rows = F.checks_demo(seed=1)
    hw = next(r for r in rows if r["by"] == "C3 hardware")
    states = {n: s for n, s, _ in hw["results"]}
    assert states["C1 signature"] == "pass" and states["C2 newer"] == "pass"
    assert states["C3 hardware"] == "REFUSE"
    assert all(
        states[n] == "not reached"
        for n in (
            "C4 stepping stone",
            "C5 size",
            "C6 attempts",
            "C7 digest",
            "C8 health",
        )
    )


def test_without_a_download_the_digest_and_health_checks_are_not_reached():
    sk = bytes(range(32))
    signed, _ = _signed(sk)
    results = F.device_checks(signed, F.ed25519_publickey(sk), F.Unit(), None)
    states = {n: s for n, s, _ in results}
    assert states["C6 attempts"] == "pass"
    assert states["C7 digest"] == "not reached" and states["C8 health"] == "not reached"
    assert F.verdict(results) == ("FLASHED", "all eight")


def test_sideways_version_is_refused_and_a_patch_is_accepted():
    sk = bytes(range(32))
    pk = F.ed25519_publickey(sk)
    same, image = _signed(sk, version="1.4.0")
    assert F.verdict(F.device_checks(same, pk, F.Unit(), image)) == (
        "REFUSE",
        "C2 newer",
    )
    patch, image = _signed(sk, version="1.4.1")
    assert F.verdict(F.device_checks(patch, pk, F.Unit(), image))[0] == "FLASHED"


def test_a_manifest_edited_after_signing_fails_the_signature_first():
    sk = bytes(range(32))
    signed, image = _signed(sk)
    signed["manifest"]["hw"] = "pl-rev-a"
    assert F.verdict(
        F.device_checks(signed, F.ed25519_publickey(sk), F.Unit(), image)
    ) == ("REFUSE", "C1 signature")


def test_cli_keygen_sign_verify_round_trip(tmp_path, capsys):
    image = tmp_path / "farm-1.5.0.bin"
    image.write_bytes(F.make_image("1.5.0", size=4096))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "product": "farm-monitor",
                "hw": "gh-rev-b",
                "version": "1.5.0",
                "min_fw": "1.0.0",
                "size": 0,
                "sha256": "",
                "url": "https://updates.example.net/x.bin",
            }
        )
    )
    assert F.main(["keygen", str(tmp_path / "keys")]) == 0
    key, pub = (
        tmp_path / "keys" / "ota_signing.key",
        tmp_path / "keys" / "ota_signing.pub",
    )
    assert len(bytes.fromhex(key.read_text().strip())) == 32
    assert F.main(["sign", str(manifest), str(image), "--key", str(key)]) == 0
    signed = tmp_path / "manifest.signed.json"
    body = json.loads(signed.read_text())
    assert body["manifest"]["size"] == 4096 and len(base64.b64decode(body["sig"])) == 64
    assert F.main(["verify", str(signed), str(image), "--pub", str(pub)]) == 0
    assert F.main(["verify", str(signed), "--pub", str(pub)]) == 0
    assert (
        F.main(
            ["verify", str(signed), str(image), "--pub", str(pub), "--running", "1.5.0"]
        )
        == 1
    )
    assert (
        F.main(
            ["verify", str(signed), str(image), "--pub", str(pub), "--hw", "pl-rev-a"]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "would flash" in out and "C2 newer" in out


def test_no_signing_key_is_committed_with_the_example():
    assert not list(HERE.glob("*.key")) and not list(HERE.glob("**/*.key"))


# --- rollout ---------------------------------------------------------------


def test_stage_sizes_are_cumulative_and_cover_the_fleet():
    assert F.stage_sizes(1000, (1, 10, 100)) == [10, 90, 900]
    assert F.stage_sizes(1000, (100,)) == [1000]
    assert sum(F.stage_sizes(7, (1, 10, 100))) == 7
    assert sum(F.stage_sizes(333, (5, 25, 100))) == 333


def test_rollout_without_a_defect_completes_in_three_stages():
    p = F.RolloutParams(defect=0.0, flaky=0.0, seed=2)
    r = F.simulate_rollout(p)
    assert not r.halted and r.reached == p.fleet and r.affected == 0
    assert [s.offered for s in r.stages] == [10, 90, 900]
    for s in r.stages:
        assert s.flashed == s.offered == s.healthy
        assert (
            p.health_s + p.soak_s
            <= s.duration_s
            <= p.poll_s + p.download_s + p.health_s + p.soak_s
        )
    assert r.total_s == sum(s.duration_s for s in r.stages)


def test_rollout_with_a_sure_defect_halts_at_stage_one():
    r = F.simulate_rollout(F.RolloutParams(rev_b_share=1.0, defect=1.0, flaky=0.0))
    assert r.halted and r.halted_at == 1
    assert r.affected == 10 and r.reached == 0 and len(r.stages) == 1
    assert r.extra_flashes == 0


def test_halt_without_withdrawing_the_record_costs_extra_flash_cycles():
    r = F.simulate_rollout(
        F.RolloutParams(rev_b_share=1.0, defect=1.0, withdraw=False, max_attempts=3)
    )
    assert r.extra_flashes == r.affected * 2


def test_an_interrupted_flash_is_retried_not_counted_as_a_failure():
    r = F.simulate_rollout(F.RolloutParams(defect=0.0, flaky=1.0))
    assert not r.halted and r.reached == 1000
    assert all(s.retried == s.offered for s in r.stages)


def test_rollout_is_deterministic_per_seed():
    a, b = (
        F.simulate_rollout(F.RolloutParams(seed=9)),
        F.simulate_rollout(F.RolloutParams(seed=9)),
    )
    assert a == b


def test_trials_summary_accounts_for_every_run():
    p = F.RolloutParams(seed=1)
    t = F.rollout_trials(p, 30)
    assert sum(t["halted_at"].values()) + t["completed"] == 30
    assert t["all_at_once"] == pytest.approx(45.0)
    assert t["affected_median"] <= t["affected_max"]


def test_rollout_and_trials_print(capsys):
    F.print_rollout(F.simulate_rollout(F.RolloutParams(seed=1)))
    F.print_rollout(F.simulate_rollout(F.RolloutParams(defect=0.0)))
    F.print_rollout(
        F.simulate_rollout(F.RolloutParams(rev_b_share=1.0, defect=1.0, withdraw=False))
    )
    p = F.RolloutParams()
    F.print_trials(F.rollout_trials(p, 10), p)
    out = capsys.readouterr().out
    assert (
        "HALT" in out
        and "completed:" in out
        and "more flash-reboot-rollback cycles" in out
        and "rollouts of the same image" in out
    )


# --- CLI -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["signals"],
        ["health"],
        ["health", "--current", "1.4.0", "--misses", "3"],
        ["presence", "--devices", "100"],
        ["checks"],
        ["rollout"],
        ["rollout", "--defect", "0", "--stages", "5,25,100"],
        ["rollout", "--trials", "5", "--no-withdraw"],
    ],
)
def test_cli_subcommands_run(argv, capsys):
    assert F.main(argv) == 0
    assert capsys.readouterr().out


def test_cli_synth_writes_json(capsys):
    assert F.main(["synth", "--seed", "1", "--devices", "12"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert len(body["status"]) == 12 and body["_meta"]["synthetic"]
