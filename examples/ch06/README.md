# Chapter 6 example - choosing the link

```bash
python link.py airtime                         # LoRa time on air per SF for an 11-byte frame, and the two budgets
python link.py airtime --bytes 84              # the same for chapter 2's 84-byte status record
python link.py protocol                        # bytes per status record: MQTT vs HTTPS vs a handshake per request
python link.py budget nodes.json links.json    # mAh per day and battery days, every node x every link
python link.py select nodes.json links.json    # the five exclusion rules, then the cheapest survivor
python link.py backoff                         # the reconnect schedule and 1 000 nodes after a broker outage
python link.py backoff --capacity 200          # a bigger broker
python -m pytest -q                            # twenty-six tests
```

`airtime` is the SX127x time-on-air formula for a LoRaWAN uplink (application
bytes plus the 13-byte header) at every EU868 spreading factor, with the
messages per day the two airtime budgets allow: the regulatory 1 % duty cycle
(36 s per hour) and The Things Network's 30 s per day fair use.

`protocol` counts the bytes one status record costs under MQTT on a kept TLS
session, HTTPS on a kept connection and HTTPS with a new TLS handshake per
request, with the keep-alive traffic of the kept session beside them. The
header sizes are typical values named in the output, not measurements.

`budget` is the energy model: for every node in `nodes.json` and every link
in `links.json`, mAh per day from sleep current plus the per-message wake,
transmit and receive currents, and the days a battery lasts at the usable
fraction in `links.json`.

`select` applies [chapter 6](../../ch06-connectivity.md)'s rules in order
(X1 reach, X2 payload, X3 airtime budget, X4 downlink latency, X5 battery
life) to every node x link pair, prints the first rule that excludes each
pair or its monthly cost and energy, and picks the cheapest survivor.

`backoff` prints the exponential schedule with and without full jitter and
simulates a fleet reconnecting after a 120 s broker outage against a broker
that accepts a fixed number of connections per second.

Every number printed is a property of the parameters in the two JSON files
(each with a `source` string saying where it comes from) and of the formulas
in `link.py`; nothing here was measured on a radio or a battery. Prices are
illustrative orders of magnitude, not quotations.
