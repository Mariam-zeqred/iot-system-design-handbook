# TODO

## Open

- ch01 is over the STYLE.md length cap: 3 408 prose words with code fences stripped (`awk '/^```/{f=!f;next} !f' ch01-*.md | wc -w`; limit 1 500–3 000). Candidates to trim without losing the method: section 7 (two real never-lists) could lose its second paragraph's restatement of Rule A, and the exercise answers 1 and 2 could be shortened by about a third.
- README.md is 61 lines after chapter 2 added the data-path diagram and a "Status and limitations" section (STYLE.md asks 80–200). It will pass 80 naturally as chapter rows fill in; nothing to pad.
- ch02 is 2 959 prose words (code fences and table pipes stripped), inside the cap; the `traffic` tool counts payload, MQTT, TLS-record and LoRaWAN overhead only, which the README states. If a reviewer wants TCP/IP framing included, add a `frame_overhead_bytes` field per path rather than changing the measured numbers in the chapter by hand.
