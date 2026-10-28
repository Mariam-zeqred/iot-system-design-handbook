# Chapter 8 example - the cloud side

```bash
python cloud.py fit requirements.json                 # twelve jobs x three platforms: native, add-on or you build
python cloud.py cost fleet.json prices.json           # line items and totals at 10 / 1 000 / 100 000 devices
python cloud.py cost --devices 300                    # any fleet size
python cloud.py cost --status-period-s 300            # the AWS lever: status every 5 minutes
python cloud.py cost --history-retention-days 30      # the Firebase lever: 30 days of history in the database
python cloud.py fanout fleet.json prices.json         # one phone a month under three listener placements
python cloud.py rules firebase|aws|mosquitto|map      # chapter 7's rules table in each dialect
python -m pytest -q                                   # forty-six tests
```

`fit` reads [`requirements.json`](requirements.json): the jobs the cloud
side must do for chapter 3's four streams, chapter 6's presence, chapter 7's
identity and chapter 9's OTA, and for each job how Firebase Realtime
Database, AWS IoT Core with managed stores, and a self-hosted broker with a
time-series database do it. Each cell is a level (`native`, `add-on`,
`you build`) and one sentence.

`cost` multiplies [`fleet.json`](fleet.json) by [`prices.json`](prices.json).
The fleet is chapter 3's records (bytes and rows a day from
`examples/ch03/streams.json`) on chapter 6's node mix (two greenhouse nodes
on Wi-Fi per plot node on LoRaWAN, one gateway per ten plot nodes), plus
phones watching their devices, whose numbers are assumptions and say so in
the file's `sources`. Every quantity is printed with its unit next to the
price it is multiplied by. The AWS prices were read from the AWS Price List
API on 2026-10-28 (offer versions in the file); the Firebase and
data-transfer prices could not be fetched from the sandbox and are written
from memory with `"verified": false`, which the tool prints. The self-hosted
column prints hours of operations as hours, not dollars. `GB` is 10^9 bytes
everywhere, which overstates AWS's binary units by about 7 %.

`fanout` prices the part of the bill that does not scale with the fleet: one
phone, under a listener on `/status` with an indexed chart query, a listener
on the whole device node, and an unindexed chart query, and the same phone on
the other two platforms.

`rules` writes chapter 7's five-row table as Firebase rules JSON, an AWS IoT
policy for `${iot:Connection.Thing.ThingName}`, and a Mosquitto ACL with the
checks the broker cannot do named for the collector; `map` prints the
correspondence. None of the three has been deployed or run against its
engine here: they are the dialects of one table, to be tested in the
Firebase emulator, with the IoT policy simulator and against a local
Mosquitto before use.
