# Chapter 5 example - safety layers under one failing part

```bash
python layers.py coverage layers.json                         # layers x failures: who still catches what
python layers.py compare                                      # four failures x four layer sets, 180 min each
python layers.py run --fail ssr-short --layers interlock,gate # one timeline
python layers.py run --fail mcu-hang --layers interlock,watchdog,gate,cutout
python layers.py run --fail sensor-stuck --layers interlock   # the 90-min max on-time, alone
python layers.py sizing                                       # the minutes behind the design numbers
python -m pytest -q                                           # nineteen tests
```

`coverage` is chapter 1's Rule B as a table. `layers.json` lists the
layers (what signal each senses, which parts it senses and acts through) and
the failures (which parts each breaks, which signals it produces). A layer
catches a failure only if it senses a signal the failure produces and none
of its parts is broken; the table marks the rest `broken` (shares a failed
part) or `blind` (does not sense that signal).

`run` and `compare` put a heater on the queen-bee incubator's fitted chamber
model, taken from
[incubator-telemetry-analysis](https://github.com/Mariam-zeqred/incubator-telemetry-analysis)
`figures/model_params.json` (second-order physical branch: heater gain
0.5908 °C/min, heater lag 4.92 min, loss coefficient 0.0111 /min; one device,
one day, door shut, ambient 27.2–28.3 °C). The firmware (hysteresis control,
a 40 °C cutoff held 30 s, a 90-min maximum on-time, a latch), the watchdog
with a pull-down, the heartbeat gate and the bimetal cutout are models of the
design in [chapter 5](../../ch05-safety-layers.md), with their numbers in
`Design`. One part fails at the first heater-on of steady cycling; the tool
reports the peak temperature and which layer removed power, how long after.

Every number printed is a property of the design and the fitted model, not a
measurement: the chamber data never went above about 39 °C, so everything
above that is a linear extrapolation, and the real heater, SSR and bimetal
were not characterised for this handbook.
