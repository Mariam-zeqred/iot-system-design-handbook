# TODO

## Open

- ch01 is over the STYLE.md length cap: 3 408 prose words with code fences stripped (`awk '/^```/{f=!f;next} !f' ch01-*.md | wc -w`; limit 1 500–3 000). Candidates to trim without losing the method: section 7 (two real never-lists) could lose its second paragraph's restatement of Rule A, and the exercise answers 1 and 2 could be shortened by about a third.
- README.md is 61 lines after chapter 2 added the data-path diagram and a "Status and limitations" section (STYLE.md asks 80–200). It will pass 80 naturally as chapter rows fill in; nothing to pad.
- ch02 is 2 959 prose words (code fences and table pipes stripped), inside the cap; the `traffic` tool counts payload, MQTT, TLS-record and LoRaWAN overhead only, which the README states. If a reviewer wants TCP/IP framing included, add a `frame_overhead_bytes` field per path rather than changing the measured numbers in the chapter by hand.
- ch03 is 2 997 prose words (code fences and table pipes stripped) after four trimming passes; `wc -w` says 3 796 because of the two Mermaid blocks, the three quoted tool outputs and the tables. Nothing to do unless the cap is re-measured by `wc -w`.
- ch03's `growth` tool counts compact JSON only. If chapter 8 needs database-side sizes (Firebase RTDB stores keys with every row, Timescale compresses), add a `--overhead-bytes-per-row` option rather than editing the chapter's table by hand.
- ch03's exercise 2 (frame version 2 with `t_soil`) is a natural test to add to `test_telemetry.py` once chapter 6 fixes the LoRaWAN payload budget.
- ch04 is 3 000 prose words or just under (code fences and table lines stripped, same measure as ch03), after three trimming passes; `wc -w` is higher because of the four tables and the Mermaid block.
- ch04's simulator keeps the valve timer and the latch in RAM, so the superloop column's late latch (1 012 s vs 617 s) is the point, not a bug; exercise 5 designs the fix and the WALKTHROUGH extension is to implement it as `--persist`.
- ch04 section 6 quotes the two firmware READMEs (`firmware/QueenBeeIncubator_V2/README.md`, `firmware/README.md`) for task rates, interlock inputs and the watchdog. Priorities, stacks, cores and the poultry controller's shared-state structure are stated as "not in the README"; if Mariam wants them in the chapter, they must come from the code, with the file named.
- Chapter 5 should reuse `decide`'s safe-state table and ask what an MCU failure does to it (hardware max-on timer, pull-downs, a second sensor); the simulator could gain an `--mcu-hang` scenario then.
