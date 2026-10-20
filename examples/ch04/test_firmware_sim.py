import pytest

from firmware_sim import (
    CFG,
    OWNER,
    SAFE_FAN,
    SAFE_VALVE,
    TASK_TABLE,
    Conditions,
    OwnershipError,
    Plant,
    Scenario,
    SharedState,
    Snapshot,
    compare_markdown,
    decide,
    simulate,
    task_table_markdown,
    truth_table,
    truth_table_markdown,
)

# --- shared state: one writer per field -------------------------------------


def test_every_snapshot_field_has_exactly_one_owner():
    assert set(OWNER) == set(Snapshot().__dict__)
    assert set(OWNER.values()) == {"sensor", "control", "safety", "comms"}


def test_update_by_the_owner_is_visible_to_readers():
    st = SharedState()
    st.update("sensor", t=27.5, seq=1)
    assert st.read().t == 27.5


def test_update_by_a_non_owner_is_refused():
    st = SharedState()
    with pytest.raises(OwnershipError):
        st.update("control", fan=1)  # control may request, never drive
    with pytest.raises(OwnershipError):
        st.update("comms", t=99.0)  # the cloud may not write a reading


def test_read_returns_a_copy_not_the_live_record():
    st = SharedState()
    snap = st.read()
    snap.t = 50.0
    assert st.read().t is None


# --- the interlock -------------------------------------------------------------


def c(**kw) -> Conditions:
    base = {
        "ready": True,
        "fresh": True,
        "in_range": True,
        "latched": False,
        "over_limit": False,
        "valve_overtime": False,
    }
    base.update(kw)
    return Conditions(**base)


def test_request_passes_when_everything_is_fine():
    p = decide(c(), 1, 1)
    assert (p.fan, p.valve, p.reason) == (1, 1, "")


def test_boot_and_invalid_sensor_give_the_same_safe_states():
    boot = decide(c(ready=False, fresh=False, in_range=False), 0, 1)
    stale = decide(c(fresh=False), 0, 1)
    bad = decide(c(in_range=False), 0, 1)
    assert (
        (boot.fan, boot.valve)
        == (stale.fan, stale.valve)
        == (bad.fan, bad.valve)
        == (SAFE_FAN, SAFE_VALVE)
    )


def test_hard_limit_forces_the_fan_but_leaves_the_valve_to_the_request():
    p = decide(c(over_limit=True), 0, 1)
    assert (p.fan, p.valve) == (1, 1)


def test_valve_overtime_closes_the_valve_whatever_the_request():
    p = decide(c(valve_overtime=True), 1, 1)
    assert (p.fan, p.valve) == (1, 0)


def test_latched_fault_beats_everything_except_boot():
    p = decide(c(latched=True, over_limit=True, valve_overtime=True), 0, 1)
    assert (p.fan, p.valve) == (SAFE_FAN, SAFE_VALVE)
    assert "latched" in p.reason


def test_truth_table_covers_every_reachable_combination_once():
    rows = truth_table()
    assert len(rows) == 11
    assert len({r[0] for r in rows}) == 11
    assert sum(1 for _, p in rows if p.reason == "") == 1
    assert truth_table_markdown().count("\n") == 12


# --- the task table -------------------------------------------------------------


def test_safety_is_the_only_watchdog_feeder_and_never_blocks():
    feeders = [t for t in TASK_TABLE if t.kicks_wdt]
    assert [t.name for t in feeders] == ["safety"]
    assert not feeders[0].blocks
    assert feeders[0].priority == max(t.priority for t in TASK_TABLE)


def test_blocking_tasks_have_the_lowest_priority():
    lowest = min(t.priority for t in TASK_TABLE)
    assert all(t.priority == lowest for t in TASK_TABLE if t.blocks)
    assert "| comms |" in task_table_markdown()


# --- the simulation ----------------------------------------------------------------


def test_quiet_day_cycles_the_fan_and_never_forces_anything_after_boot():
    r = simulate(Scenario("tasks", 900))
    assert r.watchdog_reboots == 0 and r.false_sensor_faults == 0 and r.fault is None
    assert r.max_safety_gap_s == pytest.approx(0.1)
    # forced only during boot, until the first reading at 17 s
    assert r.forced_s == pytest.approx(17.0, abs=0.2)
    changes = [e for e in r.events if e.task == "actuator"]
    assert len(changes) >= 4


def test_outage_does_not_touch_the_safety_task_in_the_task_architecture():
    r = simulate(Scenario("tasks", 900, outage=(120, 420)))
    assert r.max_safety_gap_s == pytest.approx(0.1)
    assert r.watchdog_reboots == 0 and r.false_sensor_faults == 0
    assert any("blocked 90 s" in e.text for e in r.events)


def test_outage_resets_the_superloop_through_the_watchdog():
    r = simulate(Scenario("superloop", 900, outage=(120, 420)))
    assert r.watchdog_reboots == 5
    assert r.max_safety_gap_s == pytest.approx(CFG.watchdog_s)
    assert r.forced_s > 90


def test_outage_stalls_the_superloop_without_a_watchdog_and_fakes_a_sensor_fault():
    r = simulate(Scenario("superloop", 900, outage=(120, 420), watchdog=False))
    assert r.max_safety_gap_s == pytest.approx(CFG.comms_timeout_s * CFG.comms_retries)
    assert r.false_sensor_faults == 1
    assert r.buffered_max == 1 and r.replayed == 1


def test_dead_sensor_is_caught_after_three_periods_and_actuators_go_safe():
    r = simulate(Scenario("tasks", 600, sensor_dies_at=300))
    raised = [e for e in r.events if "sensor_fault raised" in e.text]
    assert len(raised) == 1
    # last good sample at 287 s (17 + 18 x 15), stale at 287 + 45 = 332 s
    assert raised[0].ts == pytest.approx(332.1, abs=0.2)
    assert (r.final.fan, r.final.valve) == (SAFE_FAN, SAFE_VALVE)
    assert r.false_sensor_faults == 0


def test_hose_burst_latches_the_valve_at_max_on_time():
    r = simulate(Scenario("tasks", 900, hose_burst=True))
    assert r.fault == "valve_max_on"
    latched = [e for e in r.events if "latched" in e.text and e.task == "safety"]
    assert latched[0].ts == pytest.approx(17.1 + CFG.valve_max_s, abs=0.2)
    assert r.final.valve == 0


def test_watchdog_resets_wipe_the_valve_timer_so_the_latch_comes_late():
    tasks = simulate(Scenario("tasks", 1500, outage=(120, 420), hose_burst=True))
    loop = simulate(Scenario("superloop", 1500, outage=(120, 420), hose_burst=True))
    t_tasks = next(e.ts for e in tasks.events if "latched" in e.text)
    t_loop = next(e.ts for e in loop.events if "latched" in e.text)
    assert t_tasks == pytest.approx(617.1, abs=0.2)  # 17.1 s + 600 s, resets or not
    assert t_loop > t_tasks + 300  # the timer restarted with every reset


def test_plant_fan_cools_and_valve_wets():
    p = Plant()
    p.step(fan=1, valve=1, dt=60)
    assert p.t_c < 27.0 and p.soil_pct > 30.0
    q = Plant(soil_rises=False)
    q.step(fan=0, valve=1, dt=60)
    assert q.t_c > 27.0 and q.soil_pct < 30.0


def test_compare_table_has_one_row_per_metric():
    rows = [simulate(Scenario("tasks", 300)), simulate(Scenario("superloop", 300))]
    md = compare_markdown(rows)
    assert md.count("\n") == 9  # header, rule, 8 metrics
