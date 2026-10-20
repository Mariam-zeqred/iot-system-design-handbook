"""Chapter 4 - firmware architecture.

A host-side model of the greenhouse node's firmware, standard-library only:

    python firmware_sim.py table                      # the task table (markdown)
    python firmware_sim.py interlock                  # the interlock's truth table
    python firmware_sim.py run --arch tasks --outage 120:420     # timeline of one scenario
    python firmware_sim.py run --arch superloop --outage 120:420
    python firmware_sim.py compare --outage 120:420   # both architectures, one metrics table

The firmware is six tasks (sensor, safety, control, display, datalog, comms)
that share one snapshot - the status record of chapter 3 plus a few
bookkeeping fields - through a ``SharedState`` that enforces one writer per
field.  The safety task is the only caller of ``Interlock.decide`` and the
only task that drives an actuator; the control task only *requests*.

Two schedulers run the same tasks.  ``tasks`` behaves like an RTOS: a task
that blocks (comms waiting on a dead network) blocks only itself.
``superloop`` is the beginner's ``loop()``: one thread, so a blocking call
stops every task, including safety.  The point of the chapter is the
difference between the two under a network outage, measured.

The greenhouse model (first-order, 240 s time constant, 24 degC outside plus a
12 degC solar gain that a running fan removes 90 % of) is illustrative: it
exists so that the fan has something to do.  Nothing here is a measurement.
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Any

TICK_S = 0.1  # the scheduler's resolution; every period is a whole number of ticks

# ---------------------------------------------------------------------------
# 1. Configuration: every number a chapter-1 hazard or a chapter-3 setting set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    sensor_period_s: float = 15.0  # N2: three misses = 45 s detection delay
    stale_after_s: float = 45.0  # N2
    t_hard_limit_c: float = 35.0  # N1: hard limit ...
    t_hard_limit_hold_s: float = 30.0  # ... held for 30 s before the response
    t_on_c: float = 28.0  # chapter-3 settings (already clamped, N5)
    t_off_c: float = 26.0
    soil_dry_pct: float = 35.0
    soil_wet_pct: float = 45.0
    valve_max_s: float = 600.0  # N1 for the valve: maximum on-time
    watchdog_s: float = 5.0  # the safety task must run at least this often
    t_range_c: tuple[float, float] = (-40.0, 85.0)  # chapter 3's physical range
    comms_period_s: float = 60.0  # N3: status every 60 s
    comms_timeout_s: float = 30.0  # one TCP connect timeout
    comms_retries: int = 3  # the naive "try three times" loop
    boot_s: float = 2.0  # time from reset to the first task


CFG = Config()

# ---------------------------------------------------------------------------
# 2. Shared state: one snapshot, one writer per field
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """The status record of chapter 3, plus what the tasks need to share.

    Every field has exactly one writing task (OWNER).  Readers always get a
    copy taken under the lock, so a reader never sees a half-written record.
    """

    # written by the sensor task
    seq: int = 0
    sample_ts: float = -math.inf  # device clock, seconds since boot
    t: float | None = None
    h: float | None = None
    soil: float | None = None
    tank: float | None = None
    # written by the control task
    req_fan: int = 0
    req_valve: int = 0
    # written by the safety task (the only task that touches an actuator)
    fan: int = 0
    valve: int = 0
    fault: str | None = None  # latched until a person clears it
    forced: str = ""  # why the request was overridden, "" when it was not
    valve_on_since: float | None = None
    # written by the comms task
    link_ok: int = 0
    rssi: int | None = None


OWNER: dict[str, str] = {
    "seq": "sensor",
    "sample_ts": "sensor",
    "t": "sensor",
    "h": "sensor",
    "soil": "sensor",
    "tank": "sensor",
    "req_fan": "control",
    "req_valve": "control",
    "fan": "safety",
    "valve": "safety",
    "fault": "safety",
    "forced": "safety",
    "valve_on_since": "safety",
    "link_ok": "comms",
    "rssi": "comms",
}
assert set(OWNER) == {f.name for f in fields(Snapshot)}


class OwnershipError(RuntimeError):
    """A task wrote a field it does not own."""


class SharedState:
    """Copy-in / copy-out under a lock.

    Real firmware uses a FreeRTOS mutex and ``memcpy``; the rule it enforces
    is the same: take the lock, copy, release.  No task holds the lock while
    it reads a sensor, writes a relay or waits for the network, so the lock is
    held for microseconds and a high-priority task is never stuck behind a
    low-priority one.
    """

    def __init__(self) -> None:
        self._snap = Snapshot()
        self.writes = 0

    def read(self) -> Snapshot:
        return copy.copy(self._snap)  # lock; memcpy; unlock

    def update(self, writer: str, **values: Any) -> None:
        for name in values:
            if OWNER[name] != writer:
                raise OwnershipError(
                    f"{writer} may not write {name} (owner: {OWNER[name]})"
                )
        self._snap = Snapshot(**{**self._snap.__dict__, **values})  # lock; swap; unlock
        self.writes += 1

    def reset(self) -> None:
        self._snap = Snapshot()


# ---------------------------------------------------------------------------
# 3. The single interlock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conditions:
    """What the safety task knows at one instant.  All booleans, on purpose."""

    ready: bool  # at least one valid reading since boot
    fresh: bool  # reading age <= stale_after_s
    in_range: bool  # reading inside the physical range
    latched: bool  # a fault a person has not yet cleared
    over_limit: bool  # t above the hard limit for the hold time
    valve_overtime: bool  # valve on longer than valve_max_s


@dataclass(frozen=True)
class Permit:
    fan: int
    valve: int
    reason: str  # "" when the request passed through unchanged


# Safe states.  The rule: the safe state is the one whose cost is reversible.
# A fan that runs for nothing costs electricity; a valve that opens for
# nothing floods a plot.
SAFE_FAN = 1
SAFE_VALVE = 0


def decide(c: Conditions, req_fan: int, req_valve: int) -> Permit:
    """The one place that decides whether an actuator may run.

    First matching line wins, most conservative first.  Every actuator write
    in the firmware goes through the result of this function.
    """
    if not c.ready:
        return Permit(SAFE_FAN, SAFE_VALVE, "boot: no reading yet, safe states (N4)")
    if c.latched:
        return Permit(SAFE_FAN, SAFE_VALVE, "latched fault, waiting for a person (N1)")
    if not c.fresh or not c.in_range:
        return Permit(SAFE_FAN, SAFE_VALVE, "sensor invalid: safe states (N2)")
    if c.valve_overtime:
        return Permit(req_fan, SAFE_VALVE, "valve max on-time: closed and latched (N1)")
    if c.over_limit:
        return Permit(1, req_valve, "hard limit: fan forced on (N1)")
    return Permit(req_fan, req_valve, "")


def truth_table() -> list[tuple[Conditions, Permit]]:
    """Every reachable combination of conditions, for the chapter's table."""
    rows = []
    for ready in (False, True):
        for latched in (False, True):
            for valid in (False, True):
                for valve_overtime in (False, True):
                    for over_limit in (False, True):
                        if not ready and (
                            latched or valid or valve_overtime or over_limit
                        ):
                            continue  # nothing is known before the first reading
                        if not valid and (valve_overtime or over_limit):
                            continue  # the timers need a valid reading to run
                        c = Conditions(
                            ready, valid, valid, latched, over_limit, valve_overtime
                        )
                        rows.append((c, decide(c, 0, 1)))
    return rows


# ---------------------------------------------------------------------------
# 4. The plant and the link: illustrative, so the tasks have something to do
# ---------------------------------------------------------------------------


@dataclass
class Plant:
    t_out_c: float = 24.0
    solar_c: float = 12.0  # equilibrium rise above outside with the fan off
    fan_removes: float = 0.9  # fraction of the solar rise a running fan removes
    tau_s: float = 240.0
    t_c: float = 27.0
    soil_pct: float = 30.0
    tank_pct: float = 80.0
    soil_rises: bool = True  # False: hose burst, the valve waters the road

    def step(self, fan: int, valve: int, dt: float) -> None:
        t_eq = self.t_out_c + self.solar_c * (1 - self.fan_removes * fan)
        self.t_c += dt * (t_eq - self.t_c) / self.tau_s
        self.soil_pct -= dt * 0.003  # drying, about 0.2 % per minute
        if valve:
            self.tank_pct = max(0.0, self.tank_pct - dt * 0.01)
            if self.soil_rises:
                self.soil_pct += dt * 0.05  # 3 % per minute of watering
        self.soil_pct = min(100.0, max(0.0, self.soil_pct))


@dataclass
class Scenario:
    arch: str = "tasks"
    duration_s: float = 900.0
    outage: tuple[float, float] | None = None  # link down from .. to .. (s)
    sensor_dies_at: float | None = None  # reads fail (CRC) from this time on
    hose_burst: bool = False
    watchdog: bool = True

    def link_up(self, now: float) -> bool:
        return self.outage is None or not (self.outage[0] <= now < self.outage[1])

    def sensor_ok(self, now: float) -> bool:
        return self.sensor_dies_at is None or now < self.sensor_dies_at


# ---------------------------------------------------------------------------
# 5. The tasks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpec:
    name: str
    period_s: float
    priority: int  # higher wins when several are due; the RTOS meaning
    core: int  # ESP32: the Wi-Fi stack lives on core 0, keep it off core 1
    blocks: bool  # may wait on I/O or the network
    kicks_wdt: bool
    writes: str
    why: str


TASK_TABLE: tuple[TaskSpec, ...] = (
    TaskSpec(
        "sensor",
        CFG.sensor_period_s,
        3,
        1,
        False,
        False,
        "t h soil tank sample_ts seq",
        "N2 sets the period: three misses = 45 s",
    ),
    TaskSpec(
        "safety",
        0.1,
        4,
        1,
        False,
        True,
        "fan valve fault forced",
        "N1's 30 s hold needs a timer finer than the sensor period; the only actuator writer",
    ),
    TaskSpec(
        "control",
        1.0,
        3,
        1,
        False,
        False,
        "req_fan req_valve",
        "hysteresis on a 240 s plant; faster buys nothing",
    ),
    TaskSpec(
        "display",
        0.2,
        2,
        1,
        False,
        False,
        "(nothing shared)",
        "a 1 s deadline for a human; chapter 2 placed it on the device",
    ),
    TaskSpec(
        "datalog",
        600.0,
        1,
        1,
        True,
        False,
        "(history outbox)",
        "chapter 3's 10-min aggregate; SD writes block",
    ),
    TaskSpec(
        "comms",
        CFG.comms_period_s,
        1,
        0,
        True,
        False,
        "link_ok rssi",
        "N3's 60 s status; TLS connects block for up to 30 s",
    ),
)


@dataclass
class Event:
    ts: float
    task: str
    text: str


@dataclass
class Result:
    arch: str
    events: list[Event] = field(default_factory=list)
    safety_runs: int = 0
    max_safety_gap_s: float = 0.0
    watchdog_reboots: int = 0
    max_t_c: float = -math.inf
    false_sensor_faults: int = 0
    buffered_max: int = 0
    forced_s: float = 0.0  # seconds the request was overridden
    replayed: int = 0
    fault: str | None = None
    final: Snapshot = field(default_factory=Snapshot)


class Node:
    """The six tasks, their private state, and the scheduler."""

    def __init__(self, cfg: Config, scn: Scenario, plant: Plant | None = None) -> None:
        self.cfg, self.scn = cfg, scn
        self.plant = plant or Plant(soil_rises=not scn.hose_burst)
        self.state = SharedState()
        self.outbox: deque[dict[str, Any]] = deque()  # events waiting for the link (N6)
        self.res = Result(scn.arch)
        self.now = 0.0
        self._limit_since: float | None = None
        self._last_safety: float | None = None
        self._sensor_fault = False
        self._next: dict[str, float] = {}
        self._busy_until: dict[str, float] = {}
        self._cpu_busy_until = 0.0
        self._boot(first=True)

    # -- helpers -----------------------------------------------------------
    def log(self, task: str, text: str) -> None:
        self.res.events.append(Event(self.now, task, text))

    def _boot(self, first: bool = False) -> None:
        # N4: actuators to their boot state before anything else runs.
        self.state.reset()
        self.state.update(
            "safety",
            fan=SAFE_FAN,
            valve=SAFE_VALVE,
            forced="boot: no reading yet, safe states (N4)",
        )
        self._limit_since = None
        self._last_safety = None
        self._sensor_fault = False
        start = self.now + self.cfg.boot_s
        self._next = {
            t.name: start
            + (t.period_s if t.name in ("sensor", "comms", "datalog") else 0)
            for t in TASK_TABLE
        }
        self._busy_until = {t.name: 0.0 for t in TASK_TABLE}
        self._cpu_busy_until = start
        self.log(
            "boot",
            f"fan {SAFE_FAN} valve {SAFE_VALVE} (safe states), snapshot empty, tasks start in {self.cfg.boot_s:.0f} s"
            + ("" if first else " (watchdog reset)"),
        )

    # -- the tasks -----------------------------------------------------------
    def task_sensor(self) -> float:
        s = self.state.read()
        if not self.scn.sensor_ok(self.now):
            return 0.0  # CRC failure: write nothing, so the age keeps growing
        p = self.plant
        self.state.update(
            "sensor",
            seq=s.seq + 1,
            sample_ts=self.now,
            t=round(p.t_c, 2),
            h=60.0,
            soil=round(p.soil_pct, 1),
            tank=round(p.tank_pct, 1),
        )
        if s.seq == 0:
            self.log(
                "sensor", f"first valid reading t={p.t_c:.1f} soil={p.soil_pct:.0f}"
            )
        return 0.0

    def task_control(self) -> float:
        s = self.state.read()
        if s.t is None or s.soil is None:
            return 0.0
        fan = s.req_fan
        if s.t >= self.cfg.t_on_c:
            fan = 1
        elif s.t <= self.cfg.t_off_c:
            fan = 0
        valve = s.req_valve
        if s.soil <= self.cfg.soil_dry_pct:
            valve = 1
        elif s.soil >= self.cfg.soil_wet_pct:
            valve = 0
        if (fan, valve) != (s.req_fan, s.req_valve):
            self.state.update("control", req_fan=fan, req_valve=valve)
        return 0.0

    def task_safety(self) -> float:
        cfg, s = self.cfg, self.state.read()
        if self._last_safety is not None:
            self.res.max_safety_gap_s = max(
                self.res.max_safety_gap_s, self.now - self._last_safety
            )
        self._last_safety = self.now
        self.res.safety_runs += 1

        ready = s.t is not None
        age = self.now - s.sample_ts
        fresh = ready and age <= cfg.stale_after_s
        in_range = ready and cfg.t_range_c[0] <= (s.t or 0) <= cfg.t_range_c[1]
        if ready and (fresh and in_range) == self._sensor_fault:  # edge
            self._sensor_fault = not (fresh and in_range)
            if self._sensor_fault:
                self.log(
                    "safety",
                    f"sensor_fault raised: reading age {age:.0f} s > {cfg.stale_after_s:.0f} s (N2)",
                )
                self.outbox.append({"type": "sensor_fault", "ts": self.now})
                if self.scn.sensor_ok(self.now):
                    self.res.false_sensor_faults += 1
            else:
                self.log("safety", "sensor_fault cleared: fresh reading")
        if fresh and in_range and s.t is not None and s.t >= cfg.t_hard_limit_c:
            self._limit_since = (
                self._limit_since if self._limit_since is not None else self.now
            )
        else:
            self._limit_since = None
        over_limit = (
            self._limit_since is not None
            and self.now - self._limit_since >= cfg.t_hard_limit_hold_s
        )
        valve_overtime = (
            s.valve_on_since is not None
            and self.now - s.valve_on_since >= cfg.valve_max_s
        )

        c = Conditions(
            ready, fresh, in_range, s.fault is not None, over_limit, valve_overtime
        )
        p = decide(c, s.req_fan, s.req_valve)
        updates: dict[str, Any] = {}
        if valve_overtime and s.fault is None:
            updates["fault"] = "valve_max_on"
            self.log(
                "safety",
                f"valve on for {cfg.valve_max_s:.0f} s: closed and latched (N1)",
            )
            self.outbox.append(
                {"type": "limit_hit", "ts": self.now, "reason": "valve max on-time"}
            )
        if over_limit and not s.forced.startswith("hard limit"):
            self.log(
                "safety",
                f"t={s.t} >= {cfg.t_hard_limit_c} for {cfg.t_hard_limit_hold_s:.0f} s: fan forced on (N1)",
            )
            self.outbox.append(
                {"type": "limit_hit", "ts": self.now, "reason": "t hard limit"}
            )
        if (p.fan, p.valve) != (s.fan, s.valve):
            what = " ".join(
                f"{n} {a}->{b}"
                for n, a, b in (("fan", s.fan, p.fan), ("valve", s.valve, p.valve))
                if a != b
            )
            self.log(
                "actuator",
                what + (f" [{p.reason}]" if p.reason else " (request permitted)"),
            )
            updates.update(fan=p.fan, valve=p.valve)
            if p.valve and not s.valve:
                updates["valve_on_since"] = self.now
            if s.valve and not p.valve:
                updates["valve_on_since"] = None
        if p.reason != s.forced:
            updates["forced"] = p.reason
        if updates:
            self.state.update("safety", **updates)
        return 0.0

    def task_display(self) -> float:
        self.state.read()  # renders the snapshot; nothing to share back
        return 0.0

    def task_datalog(self) -> float:
        return 0.2  # an SD write: short, but it blocks

    def task_comms(self) -> float:
        if not self.scn.link_up(self.now):
            block = self.cfg.comms_timeout_s * self.cfg.comms_retries
            self.state.update("comms", link_ok=0, rssi=None)
            self.log(
                "comms",
                f"connect failed, blocked {block:.0f} s ({self.cfg.comms_retries} x {self.cfg.comms_timeout_s:.0f} s timeout); {len(self.outbox)} events buffered",
            )
            self.res.buffered_max = max(self.res.buffered_max, len(self.outbox))
            return block
        if self.state.read().link_ok == 0:
            self.log(
                "comms",
                f"link up, status written, {len(self.outbox)} buffered events replayed in order (N6)",
            )
        self.res.replayed += len(self.outbox)
        self.outbox.clear()
        self.state.update("comms", link_ok=1, rssi=-66)
        return 0.5  # one TLS round trip

    # -- the scheduler -------------------------------------------------------
    def run(self) -> Result:
        cfg, scn = self.cfg, self.scn
        funcs = {
            "sensor": self.task_sensor,
            "safety": self.task_safety,
            "control": self.task_control,
            "display": self.task_display,
            "datalog": self.task_datalog,
            "comms": self.task_comms,
        }
        for k in range(round(scn.duration_s / TICK_S)):
            self.now = round(k * TICK_S, 1)
            # the hardware watchdog: fed only by the safety task
            fed_at = (
                self._last_safety
                if self._last_safety is not None
                else self._cpu_busy_until
            )
            if scn.watchdog and self.now - fed_at >= cfg.watchdog_s:
                self.res.watchdog_reboots += 1
                self.res.max_safety_gap_s = max(
                    self.res.max_safety_gap_s, self.now - fed_at
                )
                self.log(
                    "watchdog", f"safety task silent for {cfg.watchdog_s:.0f} s: reset"
                )
                self._boot()
                continue
            for spec in sorted(TASK_TABLE, key=lambda t: -t.priority):
                if self.now + 1e-9 < self._next[spec.name]:
                    continue
                busy = (
                    self._cpu_busy_until
                    if scn.arch == "superloop"
                    else self._busy_until[spec.name]
                )
                if self.now + 1e-9 < busy:
                    continue  # superloop: the one thread is inside a blocking call
                cost = funcs[spec.name]()
                self._next[spec.name] = self.now + spec.period_s
                if cost:
                    if scn.arch == "superloop":
                        self._cpu_busy_until = self.now + cost
                    else:
                        self._busy_until[spec.name] = self.now + cost
            s = self.state.read()
            self.plant.step(s.fan, s.valve, TICK_S)
            if s.forced:
                self.res.forced_s += TICK_S
            self.res.max_t_c = max(self.res.max_t_c, self.plant.t_c)
        self.res.final = self.state.read()
        self.res.fault = self.res.final.fault
        return self.res


def simulate(scn: Scenario, cfg: Config = CFG) -> Result:
    return Node(cfg, scn).run()


# ---------------------------------------------------------------------------
# 6. Printing
# ---------------------------------------------------------------------------


def task_table_markdown() -> str:
    lines = [
        "| Task | Period | Prio | Core | Blocks | Feeds WDT | Writes | Why this rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in TASK_TABLE:
        period = (
            f"{t.period_s:g} s" if t.period_s >= 1 else f"{t.period_s * 1000:.0f} ms"
        )
        lines.append(
            f"| {t.name} | {period} | {t.priority} | {t.core} | {'yes' if t.blocks else 'no'} | {'yes' if t.kicks_wdt else 'no'} | `{t.writes}` | {t.why} |"
        )
    return "\n".join(lines)


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def truth_table_markdown() -> str:
    lines = [
        "| ready | fresh+in range | latched | valve overtime | over limit | fan | valve | reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c, p in truth_table():
        lines.append(
            f"| {_yn(c.ready)} | {_yn(c.fresh)} | {_yn(c.latched)} | {_yn(c.valve_overtime)} | {_yn(c.over_limit)} | {p.fan} | {p.valve} | {p.reason or 'request passes (fan 0, valve 1 here)'} |"
        )
    return "\n".join(lines)


def metrics(r: Result) -> dict[str, str]:
    return {
        "safety-task runs": str(r.safety_runs),
        "longest gap between safety runs": f"{r.max_safety_gap_s:.1f} s",
        "watchdog resets": str(r.watchdog_reboots),
        "false sensor faults": str(r.false_sensor_faults),
        "max temperature": f"{r.max_t_c:.1f} degC",
        "seconds in a forced state": f"{r.forced_s:.0f} s",
        "events buffered (peak) / replayed": f"{r.buffered_max} / {r.replayed}",
        "latched fault at the end": r.fault or "none",
    }


def compare_markdown(results: list[Result]) -> str:
    names = [r.arch for r in results]
    cols = [metrics(r) for r in results]
    lines = ["| Metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for key in cols[0]:
        lines.append(f"| {key} | " + " | ".join(c[key] for c in cols) + " |")
    return "\n".join(lines)


def timeline(r: Result) -> str:
    return "\n".join(f"t={e.ts:7.1f}  {e.task:<9} {e.text}" for e in r.events)


def _parse_outage(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    a, b = text.split(":")
    return float(a), float(b)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("table")
    sub.add_parser("interlock")
    for name in ("run", "compare"):
        p = sub.add_parser(name)
        if name == "run":
            p.add_argument("--arch", choices=("tasks", "superloop"), default="tasks")
        p.add_argument("--duration", type=float, default=900.0)
        p.add_argument("--outage", help="link down from:to in seconds, e.g. 120:420")
        p.add_argument(
            "--sensor-dies", type=float, help="sensor reads fail from this second on"
        )
        p.add_argument(
            "--hose-burst",
            action="store_true",
            help="the valve opens but soil never rises",
        )
        p.add_argument("--no-watchdog", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "table":
        print(task_table_markdown())
    elif a.cmd == "interlock":
        print(truth_table_markdown())
    else:
        variants = (
            [(a.arch, not a.no_watchdog)]
            if a.cmd == "run"
            else [("tasks", True), ("superloop", True), ("superloop", False)]
        )
        results = []
        for arch, wdt in variants:
            scn = Scenario(
                arch,
                a.duration,
                _parse_outage(a.outage),
                a.sensor_dies,
                a.hose_burst,
                wdt,
            )
            r = simulate(scn)
            if not wdt:
                r.arch += " (no WDT)"
            results.append(r)
            if a.cmd == "run":
                print(timeline(r))
                print()
        print(compare_markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
