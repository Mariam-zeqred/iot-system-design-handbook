"""Chapter 5 - safety layers that do not depend on the network (or the MCU).

Standard-library only:

    python layers.py coverage layers.json       # which layer still catches which failure
    python layers.py run --fail ssr-short --layers interlock,gate   # one timeline
    python layers.py compare                    # four failures x four layer sets
    python layers.py sizing                     # the minutes behind the design numbers

``coverage`` is chapter 1's Rule B made mechanical: a layer catches a failure
only if it senses a signal the failure produces and none of the parts it
senses or acts through is among the parts the failure breaks.

``run`` and ``compare`` put a heater on the queen-bee incubator's fitted
chamber model (incubator-telemetry-analysis, figures/model_params.json,
second-order physical branch: heater gain a = 0.5908 degC/min, heater time
constant tau_h = 4.92 min, loss coefficient b = 0.0111 /min, fitted on one
device on one day with the door shut; ambient that day 27.2-28.3 degC) and
make one part fail while the heater is on.  The chamber model is real; the
heater's failure, the firmware, the watchdog, the heartbeat gate and the
bimetal cutout are models of the design, so every number printed is a
property of the design and the fitted model, not a measurement.  Above
about 40 degC the chamber model is an extrapolation: the data never went
there.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Rule B as a table: layers x failures
# ---------------------------------------------------------------------------


def load_spec(path: str | Path) -> dict:
    spec = json.loads(Path(path).read_text())
    parts = set(spec["parts"])
    for layer in spec["layers"]:
        for key in ("through", "acts_through"):
            unknown = set(layer[key]) - parts
            if unknown:
                raise ValueError(
                    f"layer {layer['id']}: unknown parts {sorted(unknown)}"
                )
        unknown = set(layer["senses"]) - set(spec["signals"])
        if unknown:
            raise ValueError(f"layer {layer['id']}: unknown signals {sorted(unknown)}")
    for failure in spec["failures"]:
        unknown = set(failure["breaks"]) - parts
        if unknown:
            raise ValueError(
                f"failure {failure['id']}: unknown parts {sorted(unknown)}"
            )
    return spec


def catches(layer: dict, failure: dict) -> bool:
    """A layer catches a failure if it can sense it and nothing it needs is broken."""
    broken = set(failure["breaks"])
    if not set(layer["senses"]) & set(failure["produces"]):
        return False
    return not (broken & set(layer["through"]) or broken & set(layer["acts_through"]))


def coverage(spec: dict) -> list[dict]:
    rows = []
    for failure in spec["failures"]:
        caught_by = [
            layer
            for layer in spec["layers"]
            if layer["acts"] and catches(layer, failure)
        ]
        caught_by.sort(key=lambda layer: layer["response_s"])
        on_device = [
            layer for layer in caught_by if "human" not in layer["acts_through"]
        ]
        rows.append(
            {
                "failure": failure,
                "caught_by": caught_by,
                "device_layers": on_device,
                "rule_b": bool(on_device) or not failure.get("critical", True),
            }
        )
    return rows


def fmt_seconds(s: float) -> str:
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min"


def print_coverage(spec: dict) -> None:
    layers = spec["layers"]
    head = (
        "| failure | "
        + " | ".join(layer["id"] for layer in layers)
        + " | first device layer |"
    )
    print(head)
    print("|" + "---|" * (len(layers) + 2))
    for row in coverage(spec):
        f = row["failure"]
        cells = []
        for layer in layers:
            if not f["produces"]:
                cells.append("-")
            elif catches(layer, f):
                cells.append("yes")
            elif set(f["breaks"]) & (
                set(layer["through"]) | set(layer["acts_through"])
            ):
                cells.append("broken")
            else:
                cells.append("blind")
        first = row["device_layers"][0] if row["device_layers"] else None
        last = (
            "nothing to catch"
            if not f["produces"]
            else f"{first['id']} ({fmt_seconds(first['response_s'])})"
            if first
            else "NONE on the device"
        )
        print(f"| {f['id']} | " + " | ".join(cells) + f" | {last} |")
    print()
    for row in coverage(spec):
        f = row["failure"]
        if not f["produces"]:
            continue
        names = ", ".join(
            f"{layer['id']} ({fmt_seconds(layer['response_s'])})"
            for layer in row["caught_by"]
        )
        verdict = (
            "Rule B ok"
            if row["rule_b"] and row["device_layers"]
            else "accepted"
            if row["rule_b"]
            else "Rule B GAP"
        )
        print(
            f"{f['id']:<14} {verdict:<11} hazard {f['hazard']}; caught by: {names or 'nothing'}"
        )


# ---------------------------------------------------------------------------
# 2. The chamber: the incubator's fitted second-order model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chamber:
    """dq/dt = (u - q) / tau_h ;  dT/dt = a q - b (T - T_out).  Minutes."""

    a: float = 0.5908  # degC/min at full heater power
    tau_h: float = 4.92  # min, heater element lag
    b: float = 0.0111  # 1/min
    t_out: float = 27.5  # degC, the fitted day's ambient

    @property
    def equilibrium_on(self) -> float:
        return self.t_out + self.a / self.b


CHAMBER = Chamber()


@dataclass(frozen=True)
class Design:
    """Every number the chapter argues about, in one place."""

    setpoint_c: float = 35.0
    band_c: float = 0.3  # hysteresis half-width
    cutoff_c: float = 40.0  # firmware hard limit (the incubator's number)
    cutoff_hold_s: float = 30.0  # held this long before the latch (chapter 4)
    max_on_s: float = (
        90 * 60
    )  # firmware maximum heater on-time (the incubator's number)
    watchdog_s: float = 5.0  # feed timeout
    boot_s: float = 2.0  # reset to the first task
    gate_timeout_s: float = 3.0  # heartbeat gate opens this long after the last pulse
    max_setpoint_c: float = (
        40.0  # highest setpoint the firmware accepts (the incubator's clamp)
    )
    cutout_c: float = 45.0  # bimetal nominal opening temperature
    cutout_tol_c: float = 2.0  # its datasheet tolerance (cheap parts are +-5)
    cutout_tau_s: float = 120.0  # the bimetal follows the air with this lag
    gradient_c: float = 1.0  # air at the bimetal vs air at the control sensor
    sensor_err_c: float = 0.3  # control sensor accuracy (SHT31 class)


DESIGN = Design()

FAILURES = ("mcu-hang", "ssr-short", "sensor-stuck", "bad-setpoint")
LAYER_SETS: tuple[tuple[str, str], ...] = (
    ("firmware", "interlock"),
    ("+ watchdog", "interlock,watchdog"),
    ("+ gate", "interlock,watchdog,gate"),
    ("+ cutout", "interlock,watchdog,gate,cutout"),
)


@dataclass
class State:
    t_c: float
    q: float = 0.0  # heater element state 0..1
    cutout_c: float = 0.0  # bimetal temperature
    heater_cmd: int = 0  # what the firmware drives on the pin
    pin: int = 0  # what the pin actually is (held high by a hung MCU)
    ssr_on: bool = False  # what the SSR does (shorted: True whatever the pin)
    gate_closed: bool = True
    cutout_closed: bool = True
    heartbeat_ok: bool = True
    fw_alive: bool = True
    fw_latched: str | None = None
    booting_until: float | None = None
    last_feed_s: float = 0.0
    last_pulse_s: float = 0.0
    over_since_s: float | None = None
    on_since_s: float | None = None
    sensed_c: float = 0.0
    resets: int = 0
    log: list[str] = field(default_factory=list)


@dataclass
class Result:
    failure: str
    layers: str
    peak_c: float
    minutes_above_cutoff: float
    power_removed_by: str
    power_removed_at_s: float | None
    failed_at_s: float | None
    end_c: float
    resets: int
    log: list[str]


def simulate(
    failure: str,
    layers: str,
    fail_at_s: float | None = None,
    duration_s: float = 180 * 60,
    chamber: Chamber = CHAMBER,
    design: Design = DESIGN,
    dt_s: float = 1.0,
) -> Result:
    """Run the heater on the chamber; at ``fail_at_s`` (default: the first
    moment the heater is on in steady cycling) make one part fail."""
    active = set(layers.split(",")) if layers else set()
    unknown = active - {"interlock", "watchdog", "gate", "cutout"}
    if unknown:
        raise ValueError(f"unknown layers {sorted(unknown)}")
    if failure not in FAILURES and failure != "none":
        raise ValueError(f"unknown failure {failure}")

    s = State(t_c=chamber.t_out, cutout_c=chamber.t_out, sensed_c=chamber.t_out)
    s.heater_cmd = 1
    s.pin = 1
    s.on_since_s = 0.0
    failed = False
    stuck_value: float | None = None
    setpoint = design.setpoint_c
    cutoff = design.cutoff_c
    peak = s.t_c
    above_s = 0.0
    removed_by = "nothing"
    removed_at: float | None = None
    heater_was_on = True
    cycled_off = False  # the heater has been off at least once: steady cycling
    failed_at: float | None = None

    def log(now: float, who: str, what: str) -> None:
        s.log.append(f"t={now / 60:7.1f} min  {who:<9} {what}")

    n = int(duration_s / dt_s)
    for i in range(n + 1):
        now = i * dt_s

        # --- the sensor
        s.sensed_c = stuck_value if stuck_value is not None else s.t_c

        # --- the firmware (control + interlock), when it runs
        if s.booting_until is not None and now >= s.booting_until:
            s.booting_until = None
            s.fw_alive = True
            s.over_since_s = None
            s.on_since_s = None
            log(now, "boot", "first task runs; heater off until control asks again")
        if s.fw_alive and s.booting_until is None:
            s.last_feed_s = now
            if s.fw_latched is None:
                # control: hysteresis
                if s.sensed_c < setpoint - design.band_c:
                    want = 1
                elif s.sensed_c > setpoint + design.band_c:
                    want = 0
                else:
                    want = s.heater_cmd
                # interlock
                if "interlock" in active:
                    if s.sensed_c > cutoff:
                        s.over_since_s = (
                            s.over_since_s if s.over_since_s is not None else now
                        )
                        if now - s.over_since_s >= design.cutoff_hold_s:
                            s.fw_latched = f"cutoff {cutoff:.0f} degC held {design.cutoff_hold_s:.0f} s"
                    else:
                        s.over_since_s = None
                    if (
                        s.on_since_s is not None
                        and now - s.on_since_s >= design.max_on_s
                    ):
                        s.fw_latched = f"max on-time {design.max_on_s / 60:.0f} min"
                    if s.fw_latched:
                        log(
                            now,
                            "interlock",
                            f"latched: {s.fw_latched}; heater commanded off, heartbeat stopped",
                        )
                        want = 0
                s.heater_cmd = want
            else:
                s.heater_cmd = 0
            s.pin = s.heater_cmd
            s.heartbeat_ok = s.fw_latched is None
            if s.heartbeat_ok:
                s.last_pulse_s = now
        # a hung MCU: pin frozen, no feed, no pulse (nothing to do: the state stays)

        # --- the failure, injected after the firmware ran, once the heater is on in steady cycling
        if (
            not failed
            and (
                (fail_at_s is not None and now >= fail_at_s)
                or (
                    fail_at_s is None
                    and cycled_off
                    and s.heater_cmd == 1
                    and s.fw_alive
                )
            )
            and failure != "none"
        ):
            failed = True
            failed_at = now
            if failure == "mcu-hang":
                s.fw_alive = False  # pin frozen high, no feeds, no pulses
                log(
                    now,
                    "FAILURE",
                    "MCU hangs; heater pin stays high, no watchdog feed, no heartbeat",
                )
            elif failure == "ssr-short":
                log(
                    now, "FAILURE", "SSR fails shorted: heater on whatever the pin says"
                )
            elif failure == "sensor-stuck":
                stuck_value = s.sensed_c
                log(
                    now,
                    "FAILURE",
                    f"control sensor sticks at {stuck_value:.1f} degC (valid CRC)",
                )
            elif failure == "bad-setpoint":
                setpoint, cutoff = 60.0, 65.0
                log(
                    now,
                    "FAILURE",
                    "settings write sets target 60 degC; the firmware's cutoff is target + 5",
                )

        # --- the watchdog + pull-down
        if (
            "watchdog" in active
            and s.booting_until is None
            and now - s.last_feed_s >= design.watchdog_s
        ):
            s.resets += 1
            s.booting_until = now + design.boot_s
            s.pin = 0  # pull-down: the pin is low from the reset edge
            s.heater_cmd = 0
            s.fw_alive = False
            s.fw_latched = None  # the latch was in RAM
            log(
                now,
                "watchdog",
                f"no feed for {design.watchdog_s:.0f} s: reset; pull-down holds the pin low",
            )

        # --- the heartbeat gate (retriggerable: it re-closes when the pulses resume)
        if "gate" in active:
            if s.gate_closed and now - s.last_pulse_s >= design.gate_timeout_s:
                s.gate_closed = False
                log(
                    now,
                    "gate",
                    f"no heartbeat for {design.gate_timeout_s:.0f} s: series switch opened",
                )
            elif not s.gate_closed and now - s.last_pulse_s < design.gate_timeout_s:
                s.gate_closed = True
                log(now, "gate", "heartbeat back: series switch closed again")

        # --- the thermal cutout (bimetal in the chamber, manual reset)
        if "cutout" in active and s.cutout_closed and s.cutout_c >= design.cutout_c:
            s.cutout_closed = False
            log(
                now,
                "cutout",
                f"bimetal reached {design.cutout_c:.0f} degC: heater circuit opened",
            )

        # --- what the heater actually does
        ssr_conducts = s.pin == 1 or (failed and failure == "ssr-short")
        heater_on = ssr_conducts and s.gate_closed and s.cutout_closed
        if heater_was_on and not heater_on and failed and removed_at is None:
            if not s.gate_closed:
                removed_by = "gate"
            elif not s.cutout_closed:
                removed_by = "cutout"
            elif s.booting_until is not None:
                removed_by = "watchdog"
            elif s.fw_latched:
                removed_by = "interlock"
            if removed_by != "nothing":  # control merely cycling is not a layer acting
                removed_at = now
                log(now, "heater", f"power removed by {removed_by}")
        heater_was_on = heater_on
        if s.on_since_s is None and heater_on:
            s.on_since_s = now
        if not heater_on and s.fw_alive:
            s.on_since_s = None

        # --- the plant (minutes)
        dt_min = dt_s / 60.0
        u = 1.0 if heater_on else 0.0
        s.q += dt_min * (u - s.q) / chamber.tau_h
        s.t_c += dt_min * (chamber.a * s.q - chamber.b * (s.t_c - chamber.t_out))
        s.cutout_c += dt_s * (s.t_c - s.cutout_c) / design.cutout_tau_s
        if not heater_on:
            cycled_off = True
        peak = max(peak, s.t_c)
        if s.t_c > design.cutoff_c:
            above_s += dt_s

    return Result(
        failure,
        layers,
        peak,
        above_s / 60.0,
        removed_by,
        removed_at,
        failed_at,
        s.t_c,
        s.resets,
        s.log,
    )


def compare(duration_s: float = 180 * 60) -> list[list[str]]:
    table = [["failure"] + [name for name, _ in LAYER_SETS]]
    for failure in FAILURES:
        row = [failure]
        for _, layers in LAYER_SETS:
            r = simulate(failure, layers, duration_s=duration_s)
            if r.power_removed_at_s is None or r.failed_at_s is None:
                who = f"no layer acted, {r.end_c:.1f} degC at {duration_s / 60:.0f} min"
            else:
                who = f"{r.power_removed_by} after {fmt_seconds(r.power_removed_at_s - r.failed_at_s)}"
            row.append(f"peak {r.peak_c:.1f} degC; {who}")
        table.append(row)
    return table


def print_table(rows: list[list[str]]) -> None:
    print("| " + " | ".join(rows[0]) + " |")
    print("|" + "---|" * len(rows[0]))
    for row in rows[1:]:
        print("| " + " | ".join(row) + " |")


# ---------------------------------------------------------------------------
# 3. Sizing: the minutes behind the numbers
# ---------------------------------------------------------------------------


def minutes_until(
    start_c: float,
    target_c: float,
    heater_on: bool,
    chamber: Chamber = CHAMBER,
    q0: float = 1.0,
) -> float:
    """Minutes for the chamber to go from start_c to target_c with the heater
    held on (or off), the element starting at state q0.  inf if never."""
    t, q, minutes, dt = start_c, q0, 0.0, 1 / 60
    rising = target_c > start_c
    while minutes < 24 * 60:
        u = 1.0 if heater_on else 0.0
        q += dt * (u - q) / chamber.tau_h
        t += dt * (chamber.a * q - chamber.b * (t - chamber.t_out))
        minutes += dt
        if (rising and t >= target_c) or (not rising and t <= target_c):
            return minutes
    return math.inf


def sizing(
    chamber: Chamber = CHAMBER, design: Design = DESIGN
) -> list[tuple[str, str]]:
    lower = design.cutout_c - design.cutout_tol_c
    floor = design.max_setpoint_c + design.sensor_err_c + design.gradient_c
    carry = overshoot_after_off(design.cutout_c, chamber)
    rows = [
        (
            "chamber with the heater stuck on, equilibrium",
            f"{chamber.equilibrium_on:.0f} degC (t_out + a/b; an extrapolation of a linear fit)",
        ),
        (
            f"cold start {chamber.t_out:.1f} -> {design.setpoint_c:.0f} degC, full power",
            f"{minutes_until(chamber.t_out, design.setpoint_c, True, chamber, 0.0):.0f} min",
        ),
        (
            f"stuck on at the setpoint: {design.setpoint_c:.0f} -> {design.cutoff_c:.0f} degC",
            f"{minutes_until(design.setpoint_c, design.cutoff_c, True, chamber):.0f} min",
        ),
        (
            f"stuck on: {design.cutoff_c:.0f} -> {design.cutout_c:.0f} degC (cutout)",
            f"{minutes_until(design.cutoff_c, design.cutout_c, True, chamber):.0f} min",
        ),
        (
            f"stuck on: {design.setpoint_c:.0f} -> 50 degC",
            f"{minutes_until(design.setpoint_c, 50.0, True, chamber):.0f} min",
        ),
        (
            f"power removed at {design.cutout_c:.0f} degC: how far the lag carries it",
            f"+{overshoot_after_off(design.cutout_c, chamber):.1f} degC",
        ),
        (
            f"power lost at {design.setpoint_c:.0f} degC: minutes to 30 degC (the cold side)",
            f"{minutes_until(design.setpoint_c, 30.0, False, chamber, 0.0):.0f} min at {chamber.t_out:.1f} degC ambient",
        ),
        (
            "cutout lower bound: nominal - tolerance > max setpoint + sensor error + gradient",
            (
                f"{design.cutout_c:.0f} - {design.cutout_tol_c:.0f} = {lower:.0f} > "
                f"{design.max_setpoint_c:.0f} + {design.sensor_err_c:.1f} + {design.gradient_c:.0f} = "
                f"{floor:.1f} degC: {'ok' if lower > floor else 'FAILS'}"
                " (a +-5 degC part would need a 50 degC nominal)"
            ),
        ),
        (
            "cutout upper bound: nominal + tolerance + lag overshoot, must be below harm",
            (
                f"{design.cutout_c:.0f} + {design.cutout_tol_c:.0f} + {carry:.1f} = "
                f"{design.cutout_c + design.cutout_tol_c + carry:.1f} degC "
                "(the harm temperature is the biologist's number, not the engineer's)"
            ),
        ),
        (
            "heartbeat gate timeout vs watchdog",
            (
                f"gate {design.gate_timeout_s:.0f} s < watchdog {design.watchdog_s:.0f} s "
                f"+ boot {design.boot_s:.0f} s: the gate acts first, the reset brings the firmware back"
            ),
        ),
    ]
    return rows


def overshoot_after_off(from_c: float, chamber: Chamber = CHAMBER) -> float:
    t, q, peak, dt = from_c, 1.0, from_c, 1 / 60
    for _ in range(60 * 60):
        q += dt * (0.0 - q) / chamber.tau_h
        t += dt * (chamber.a * q - chamber.b * (t - chamber.t_out))
        peak = max(peak, t)
    return peak - from_c


# ---------------------------------------------------------------------------
# 4. Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coverage", help="layers x failures table from a JSON spec")
    c.add_argument("spec")
    r = sub.add_parser("run", help="one failure, one layer set, timeline")
    r.add_argument("--fail", default="ssr-short", choices=FAILURES + ("none",))
    r.add_argument(
        "--layers",
        default="interlock",
        help="comma list of interlock,watchdog,gate,cutout",
    )
    r.add_argument("--minutes", type=float, default=180.0)
    r.add_argument(
        "--fail-at",
        type=float,
        default=None,
        help="minutes; default: first heater-on in steady cycling",
    )
    sub.add_parser("compare", help="four failures x four layer sets")
    sub.add_parser("sizing", help="the minutes behind the design numbers")
    a = p.parse_args(argv)

    if a.cmd == "coverage":
        print_coverage(load_spec(a.spec))
    elif a.cmd == "run":
        res = simulate(
            a.fail,
            a.layers,
            fail_at_s=None if a.fail_at is None else a.fail_at * 60,
            duration_s=a.minutes * 60,
        )
        for line in res.log:
            print(line)
        print()
        print(
            f"peak {res.peak_c:.1f} degC; {res.minutes_above_cutoff:.0f} min above 40 degC; "
            f"power removed by {res.power_removed_by}"
            + (
                f" at {res.power_removed_at_s / 60:.1f} min"
                if res.power_removed_at_s is not None
                else ""
            )
            + f"; {res.resets} watchdog reset(s); end {res.end_c:.1f} degC after {a.minutes:.0f} min"
        )
    elif a.cmd == "compare":
        print_table(compare())
    elif a.cmd == "sizing":
        for what, value in sizing():
            print(f"{what:<70} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
