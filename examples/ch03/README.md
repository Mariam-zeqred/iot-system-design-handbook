# Chapter 3 example - record schemas, migration, the LoRaWAN frame, settings, growth

```bash
python telemetry.py validate samples/*.json                 # every sample record against its stream schema (exit 1: two are bad on purpose)
python telemetry.py decode 018507ee1b263e3ee40e75 --dev plot1   # the 11-byte path-C status frame -> a v2 status record
python telemetry.py settings samples/settings_desired.json  # what the device reports after clamping the app's request
python telemetry.py growth streams.json                     # rows and bytes per stream per device per year
python telemetry.py growth streams.json --markdown
python -m pytest -q                                         # twenty tests
```

`telemetry.py` holds the four stream schemas of
[chapter 3](../../ch03-data-modelling.md) (`status`, `history`, `event`,
`settings_desired` / `settings_reported`) as field specifications: type,
required, nullable, physical range, allowed values. `validate` first migrates a
record with an older `schema` number (version 1 is chapter 2's payload) and
then checks it; unknown fields are listed, not rejected.

`samples/` has one file per case the chapter discusses; the stream kind is
taken from the file name. `status_bad_units.json` and
`history_bad_order.json` are meant to fail. `streams.json` gives the `growth`
tool one example record, a period or a count per day, a mode (`overwrite` or
`append`) and a retention for each stream; the byte counts are measured from
the examples as compact JSON, with no database overhead, so they are a floor.

The frame layout is in `FRAME_FMT`: version, temperature in 0.01 °C (int16),
humidity in 0.01 % (uint16), soil and tank in %, a flags byte (fan, valve and
four "valid" bits), battery in mV and a sequence counter. The plot node has no
clock, so the gateway stamps the record with its own receive time.
