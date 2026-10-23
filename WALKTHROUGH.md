# Walkthrough (for Mariam)

This file is for you, not for readers of the course. It grows by one short section per chapter.

## Why the course is shaped this way

The audience is "can code, has never shipped a device-to-cloud system", which is where most master's applicants and most junior engineers are. Every chapter produces one artefact (a list, a table, a diagram, a schema) that chapter 10 assembles into a full design, so the course reads as one project rather than ten essays. Your two real repos are used as *evidence*, quoted from their READMEs, never embellished: when a chapter says the incubator's safety task runs at 10 Hz or the poultry controller hands control to the LCD after 90 s, that is because the README says so.

## File map

- `chNN-<slug>.md` — one chapter. 1 500–3 000 words, one worked example, exercises with collapsed answers, "What to read next".
- `examples/chNN/` — every piece of code a chapter shows, runnable, with a README and tests where it makes sense. `make check` runs all of them.
- `README.md` — table of contents; a chapter's row is filled in when the chapter lands.

## Chapter 1 - Requirements and failure modes

*What it teaches.* Write the "what must never happen" list before the feature list; walk the data path asking "lies / dies / late"; two rules (a critical response may not depend on network/cloud/app; a hazard may not be guarded only by what can cause it); derive "shall" sentences and numbers from the list.

*The example.* `examples/ch01/never_list.py` reads `never_list.json` and prints a gap report. Hazard N7 (open database access) is deliberately incomplete so the report has something to show; exercise 1 completes it. The N4 story in the chapter (pull-down resistors on relay pins) is a genuine case of the rule catching a design hole.

*Questions a professor may ask.*
1. *Isn't this just FMEA / STPA?* Yes, a deliberately small subset: one hazard per row, two mechanical rules, no probability column. The chapter cites the STPA handbook as the full method. Say that you kept the part a two-person startup will actually maintain.
2. *Why must a critical response not depend on the cloud?* Because the three remote parts fail together and most often; the incubator and poultry READMEs both describe on-device safety for exactly this reason, and the poultry controller's 90-second LCD takeover is the concrete example.
3. *Where do the numbers in section 6 come from?* Each has a "comes from" column; the ones that are engineering judgement (±0.5 °C, 10 min offline) are labelled as derived from a hazard, not measured. Be ready to say which you would revisit with field data.

*One thing you could extend.* Add a `--diagram` flag to the checker that emits a Mermaid graph from `caused_by` and `guarded_by`, so the data-path figure in the chapter is generated rather than drawn.

## Chapter 2 - Choosing the data path

*What it teaches.* Turn features into computations with four attributes (critical, deadline, inputs, how often it changes); apply five rules in a fixed order (critical → device; user-facing → app; inputs the device cannot have → cloud, or gateway when multi-device and under a minute; deadline shorter than the accepted outage → device; otherwise cost of change decides); then size what crosses each link as status / history / events and check the status period against N3's detection delay.

*The example.* `examples/ch02/data_path.py place` applies the rules to `computations.json` and prints the decision table (7 device, 1 gateway, 5 cloud, 1 app). `data_path.py traffic` measures the example payloads in `paths.json` and prints messages and bytes per day for three candidate paths; the chapter's table is that output. The one-dollar-per-million-messages cost is a parameter in the JSON and is labelled illustrative everywhere it appears; do not quote it as a price.

*Questions a professor may ask.*
1. *Why not a cost model (latency, bandwidth, energy) instead of ordered rules?* Because the first rule is a hard constraint from safety, not a cost, and once it is applied the remaining decisions are rarely close calls on a farm-sized system. Say that a weighted model is the right tool at city scale (the Shi et al. survey in "What to read next") and that the rules are the subset a small team will keep honest.
2. *Isn't "gateway" just a smaller cloud?* No: its defining property is being on the farm's side of the internet link, so it survives the outage that removes the cloud, and it has local-network latency to every node. It is also one more box to secure and update, which is why R3 only sends work to it when a multi-device computation has a deadline under a minute.
3. *Your poultry controller records history from the app. Was that a mistake?* The README states the mechanism; the consequence (history only while an app is open) follows from the chapter's table. A fair answer is that it was the quickest way to get a chart in a prototype, that the device already drains readings to the cloud at a fixed rate so moving the history write to the device or to a cloud function is a small change, and that chapter 3 designs that stream properly.

*One thing you could extend.* Give `traffic` a `--firebase` mode that estimates downloaded bytes per listening app instead of messages, using the per-stream payload sizes it already measures, so the chapter 8 comparison between per-message and per-byte billing starts from the same JSON.

## Chapter 3 - Data modelling

*What it teaches.* One record is the contract (identity, time, values with unit and range, `null` not sentinels, provenance, link health); the four streams are four storage shapes with a writer, a key, a mode (overwrite or append) and a retention; settings are two documents, desired from the app and reported from the device, joined by a version the device echoes after clamping; history is ordered by the device's clock and offline detection by the server's; five rules for changing a schema while several firmware versions are in the field (add only, never change meaning in place, `schema` in every record and migrate on read, tolerate unknown fields but refuse unknown versions, retire in two steps).

*The example.* `examples/ch03/telemetry.py` has four tools. `validate` migrates and checks the sample records (two are bad on purpose: tenths-of-a-degree units and a mis-ordered aggregate; one is "from the future" and passes with its unknown fields listed). `decode` turns the plot node's 11-byte LoRaWAN frame into a status record stamped with the gateway's time. `settings` is the device's clamp-and-echo step. `growth` measures each stream's example record and multiplies out; the chapter's table is its output. All byte counts are measured compact JSON and are labelled a floor.

*Questions a professor may ask.*
1. *Why not just use JSON Schema or Protobuf?* Either would do, and the "What to read next" list points at Protobuf's evolution rules because they are the same five. The hand-written validator exists so the chapter can show the rules as thirty lines of code rather than a library; say that in a product you would generate it from one schema file shared by firmware, gateway and app.
2. *Isn't desired/reported over-engineering for a thermostat?* The queen-bee incubator already does it in all but name (the app writes `/settings/targetTemp`, the firmware clamps to 30–40 °C, the app watches `status.targetTemp` to confirm). The missing piece there is the version, which is what lets the app distinguish "clamped" from "not yet applied". So the pattern cost one integer.
3. *Your poultry schema keeps desired and reported in one node (`control/state`). What goes wrong?* The firmware fully replaces the node after each local change and the app partially updates it, so a device write landing after an app write can drop the app's field. The `FIREBASE_SCHEMA.md` "who writes what" table was the right first step; the fix is to split the node, which is additive and does not break the current app.

*One thing you could extend.* Generate `SCHEMAS` and the Markdown field tables from one `schema.json` so the chapter's tables, the validator and (in chapter 4) a C header for the firmware all come from the same file.

## Chapter 4 - Firmware architecture

*What it teaches.* Cut the device's computations into tasks by three rules (by rate, by blocking, by what they write), with every period traced to a chapter-1 or chapter-3 number; share one snapshot by copy-in/copy-out under a short lock with one writer per field, and pass events through a queue so the safety task never touches the network; separate detect / decide / drive, with one boolean-input interlock whose whole behaviour is a generated truth table and whose safe states are chosen by the cost of being wrong per actuator; feed the watchdog from the one task whose silence is the hazard, with the timeout fitted inside the 30 s deadline.

*The example.* `examples/ch04/firmware_sim.py` runs the six tasks at a 100 ms tick against a first-order greenhouse. `SharedState.update` raises if a task writes a field it does not own (a test checks that control cannot write `fan`). `decide` is the interlock and `truth_table` enumerates its eleven reachable rows. `compare --outage 120:420` is the chapter's headline table: under `tasks` the safety task never misses; under `superloop` with the watchdog the node resets five times and every RAM timer restarts (the hose-burst latch comes at 1 012 s instead of 617 s); without the watchdog the safety task stalls 90 s and raises a false sensor fault. All numbers are the model's, labelled so in the chapter and the example README.

*Questions a professor may ask.*
1. *Why a 10 Hz safety task when the sensor reports every 15 s?* Because the safety task runs timers (30 s hold, 45 s staleness, 600 s valve on-time), not the sensor; it reads a copy of the record, so 10 Hz costs microseconds, and it gives the watchdog something frequent to watch. Your incubator runs the same 10 Hz beside a 1 Hz control loop, which is the sentence to say first.
2. *Isn't the interlock just the control logic with extra steps?* No: control decides what it *wants*, the interlock decides what is *allowed*, and the two have different inputs (thresholds vs. validity, latches and timers) and different owners. The payoff is the truth table: eleven rows a reviewer can check without reading code. In your incubator `Interlock.*` is documented as "the ONE place" with five listed inputs; in the poultry controller it is `applyStates()` plus the priority order.
3. *The superloop column barely changes the temperature. So what did the architecture buy?* Honest answer: in this scenario the fallback (fan-on safe state, slow plant) covers the thermal hazard, and the chapter says so. What the superloop loses is control (102 s or 288 s forced), truth (a false sensor alarm during a router outage) and timers (a flood latch seven minutes late). On the incubator the same stall is a heater-off question with the brood cooling, which is why its safety task is separate.

*One thing you could extend.* Give the simulator a `--persist` flag that keeps the latch and the valve's open-since across watchdog resets (exercise 5), and add the resulting latch time as a fourth column of `compare`, so the chapter's "roughly 700 s" becomes a measured number.

## Chapter 5 - Safety layers that do not depend on the network

*What it teaches.* Chapter 4's interlock is firmware, and chapter 1's N1 row is guarded only by the parts that can cause it. Each guarding part has a characteristic failure (a hung MCU holds its pin level; a wrong firmware feeds the watchdog perfectly; an SSR fails shorted; a sensor sticks in range; power loss is the heater's safe state by accident). A layer is defined by what it senses, what it acts through and what it shares with the layers above; the stack is control, interlock, watchdog + pull-down, heartbeat gate, thermal cutout, person. Four rules: act in the energy path, sense the physical quantity or the absence of life, share no part with the neighbour, re-closing layers above the latching one. The cutout is sized by two bounds (never trips in normal use; trips before harm, where "harm" is the biologist's number).

*The example.* `examples/ch05/layers.py coverage layers.json` is Rule B as a table (a layer catches a failure if it senses one of its signals and none of its parts is broken). `compare` puts a heater on *your* incubator's fitted chamber (the second-order fit from incubator-telemetry-analysis `figures/model_params.json`: a = 0.5908 °C/min, τh = 4.92 min, b = 0.0111 /min) and fails one part at the first heater-on of steady cycling under four cumulative layer sets. Headline cells: a hung MCU with firmware alone runs away (68.4 °C at 180 min, equilibrium 81 °C); the watchdog fixes that row in 5 s; a shorted SSR defeats the watchdog and is caught by the gate 17 min after the failure at 41.3 °C; a stuck sensor is caught by the 90-min max on-time at 62.8 °C or by the bimetal at 46.6 °C after 31 min; a bad setpoint regulates at 60 °C with every software layer happy. `sizing` prints the minutes behind the arguments (10 min from 35 to 40 °C stuck on, 12 more to 45 °C, +0.8 °C carry after power off, 99 min to lose five degrees without power). All numbers are the model's and the chapter says so.

*Questions a professor may ask.*
1. *Your incubator has a 40 °C software cutoff, a 90-min max on-time and a watchdog. Isn't that enough?* Say what each catches: the cutoff and the max on-time catch what the sensor can see; the watchdog catches a stopped firmware. None of them opens the circuit when the SSR fails shorted (SSRs fail as conductors), and none sees a sensor that sticks at 34 °C. The chapter's table says that plainly, and so did your chapter-1 N1 row (`guarded_by: sensor, mcu`). The honest answer is "the README does not mention a thermal cutout or a second switch; that is the next hardware revision", and the sizing sheet (45 °C ±2 part; a ±5 part needs 50 °C) shows you have already done the arithmetic.
2. *Why a heartbeat gate and a watchdog? Aren't they the same thing?* Both sense silence; they act through different parts. The watchdog acts through a reset and only helps because of the pull-down and only if the hang is transient; the gate acts in the energy path and also gives the firmware a second switch (it stops the heartbeat when it latches), which is what covers the welded SSR. The gate is retriggerable so a transient hang recovers; the latch for real faults stays in your NVS, as it already does.
3. *Why 45 °C for the cutout, and why bimetal rather than a second SHT31?* Two bounds: never trip in normal use (nominal − tolerance > 40 + 0.3 + 1 = 41.3) and trip before harm (nominal + tolerance + 0.8 °C carry). A second SHT31 shares the firmware, the supply and usually the bus with the first, so it shortens the firmware column but does not add a layer; the bimetal has no software, no supply and no bus. The number the chapter cannot supply is the harm temperature: say that it is a question for an apiculture source, and that the sheet marks it as such.

*One thing you could extend.* Give `run` a `--second-sensor` option: an ambient SHT31 in the chamber air that the firmware compares with the control sensor, and a `--gate-latching` switch, so exercises 3 and 4 become measured rows instead of predictions. Then check GPIO12 on the real board (strapping pin, section 3) and write what you find into the incubator README.
