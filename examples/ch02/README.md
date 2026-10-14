# Chapter 2 example - placing computations and sizing the data path

```bash
python data_path.py place computations.json              # where each computation runs, and which rule put it there
python data_path.py place computations.json --markdown   # the same as a table
python data_path.py traffic paths.json                   # bytes/day, msg/day, offline delay for three candidate paths
python data_path.py traffic paths.json --markdown
python -m pytest -q                                      # thirteen tests
```

`computations.json` lists the farm monitor's computations from
[chapter 2](../../ch02-choosing-the-data-path.md) with the four attributes the
placement rules need (`critical`, `deadline_s`, `inputs`, `changes`).
`paths.json` describes three candidate data paths by their streams and
example payloads; the tool measures the payloads rather than trusting a
guessed byte count.

Assumptions baked into `traffic`: MQTT adds 4 bytes plus the topic per
message; TLS 1.2 with AES-GCM adds 29 bytes per record (5 header, 8 nonce,
16 tag), which is the `link_overhead_bytes` of paths A and B; LoRaWAN adds 13
bytes of header for path C. TCP/IP and Wi-Fi framing are not counted, so the
"on the wire" numbers are a floor. The cost column uses the illustrative price
in `paths.json`, not a live price page; chapter 8 prices real providers.
