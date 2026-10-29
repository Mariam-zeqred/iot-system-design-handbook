# Chapter 9 example - observability and OTA

```bash
python fleet.py signals signals.json          # nine fleet signals x three cloud shapes: native, add-on or you build
python fleet.py health status_snapshot.json   # presence, versions, reboots, delivery, memory from one status snapshot
python fleet.py synth --seed 7 --devices 60   # regenerate that snapshot (synthetic; the faults are planted)
python fleet.py presence                      # four detectors of one silence over a day of Wi-Fi hiccups
python fleet.py presence --keepalive-s 60     # a short keep-alive: the will fires sooner and lies more
python fleet.py checks                        # ten candidate images through the device's eight checks
python fleet.py keygen out/                   # an Ed25519 key pair; out/ is not committed
python fleet.py sign manifest.json image.bin --key out/ota_signing.key
python fleet.py verify manifest.signed.json image.bin --pub out/ota_signing.pub --running 1.4.0 --hw gh-rev-b
python fleet.py rollout                       # one staged rollout of an image that fails on 15 % of rev-B units
python fleet.py rollout --defect 0            # a good image: how long three stages take
python fleet.py rollout --trials 500          # the same bad image five hundred times: where it halts, how many units it reaches
python -m pytest -q                           # fifty-odd tests
```

`signals` reads [`signals.json`](signals.json): nine things you want to know
about a fleet you cannot touch (status age, last will, lifecycle events,
broker metrics, version inventory, reboot reasons, a log on demand, OTA
state and the rollout, an alert on a fleet count) and, for each of chapter
8's three shapes, a level (`native`, `add-on`, `you build`) and one
sentence.

`health` reads a snapshot of chapter 3's status records with the four fields
this chapter adds (`up_s`, `rst`, `boots`, `heap_min`) and prints the five
lines of a fleet dashboard: who is silent past N3's window (ten misses of
the device's own period: 600 s for a greenhouse node, 6 000 s for a plot
node), who runs which firmware and which units the rollout cannot reach,
who rebooted in the last day and why, how many expected status writes
arrived since boot, and who is short of heap.
[`status_snapshot.json`](status_snapshot.json) is **synthetic**: `synth`
writes it from a seed, the faults are planted at fixed device names, and the
file says so in `_meta`. No fleet was observed.

`presence` simulates a thousand devices for a day: three short Wi-Fi hiccups
per device-day (log-normal, median 40 s) and one long outage per fifty
device-days (30 to 120 minutes). For each outage it asks when each detector
would notify: the status-age rule (N3), the MQTT last will published raw,
the will with a 60 s hold-down, the will with a hold-down that brings it to
N3's window, and the device's own `online: true` flag. The will is
published once per outage either at 1.5 keep-alive intervals of silence or,
sooner, when the device reconnects with the same client id (MQTT 3.1.1
section 3.1.4). It is a model of a schedule, not a measured fleet.

`checks` builds a 300 kB image and a manifest in memory, signs the manifest,
and runs eleven candidates through the eight checks a device makes: C1 the
signature with the public key compiled into the running image, C2 a strictly
newer `MAJOR.MINOR.PATCH`, C3 the hardware id, C4 `min_fw`, C5 the partition
size, C6 the attempt counter in NVS, C7 the SHA-256 of the streamed bytes
before the boot partition is switched, C8 the health window after the
reboot, which the bootloader answers with a rollback. `keygen`, `sign` and
`verify` do the same on files; `verify` exits 1 when the device would
refuse. The signature is Ed25519 as RFC 8032 writes it, in Python integers
(the tests check both of the RFC's first test vectors); a device uses its
SDK's verifier, not this.

`rollout` plays stages of 1 %, 10 % and 100 % of a thousand-unit fleet
against an image whose defect fails the health check on a share of one
hardware revision. Each stage waits for its last health report plus a soak,
then halts if the rolled-back share reaches the threshold; a halt also
rewrites every unit's desired record, or with `--no-withdraw` shows what
the affected units do when it does not. `--trials N` repeats with
consecutive seeds and reports where the rollout halted and how many units
booted the bad image, against the all-at-once expectation.
