# Chapter 4 example - the greenhouse node's firmware, simulated on the host

```bash
python firmware_sim.py table                           # the task table (markdown)
python firmware_sim.py interlock                       # the interlock's truth table
python firmware_sim.py run --arch tasks --outage 120:420        # one scenario, timeline + metrics
python firmware_sim.py run --arch superloop --outage 120:420 --no-watchdog
python firmware_sim.py run --sensor-dies 300           # the sensor stops answering at 300 s
python firmware_sim.py run --hose-burst                # the valve opens and the soil never rises
python firmware_sim.py compare --outage 120:420        # tasks vs superloop vs superloop without watchdog
python -m pytest -q                                    # twenty-one tests
```

`firmware_sim.py` models the six tasks of
[chapter 4](../../ch04-firmware-architecture.md) (sensor, safety, control,
display, datalog, comms), the snapshot they share, the one interlock, the
hardware watchdog and a toy greenhouse, at a 100 ms tick. `SharedState`
refuses a write from any task but the field's owner (`OWNER`), so the
"one writer per field" rule is enforced, not described. `decide` is the
interlock: six booleans in, a permit per actuator out, first matching line
wins. The safety task is its only caller and the only task that changes
`fan` or `valve`; the control task only writes `req_fan` and `req_valve`.

`--arch tasks` runs the tasks as an RTOS would: a task that blocks (comms
waiting on a dead link, 3 × 30 s) blocks only itself. `--arch superloop` runs
the same functions from one `loop()`, so the blocking call stops every task
including safety, and the watchdog (fed by the safety task alone) resets the
node. The chapter's comparison table is the output of `compare`.

The plant is a first-order model (24 °C outside, a 12 °C solar rise that a
running fan removes 90 % of, 240 s time constant) and is illustrative: it
exists so that the controller has work to do. Every number the chapter
quotes from this tool is a property of the model and the task design, not a
measurement of any greenhouse.
