# IoT System Design Handbook

A ten-chapter course for people who can code but have never shipped a device-to-cloud system. One running example throughout: a small-farm environmental monitor (sensors → MCU → gateway → cloud → app). Two real, open-source systems appear as asides: the [queen-bee-incubator](https://github.com/Mariam-zeqred/queen-bee-incubator) and the [smart-poultry-farm](https://github.com/Mariam-zeqred/smart-poultry-farm) controller.

Each chapter is 1 500–3 000 words with one worked example, 3–5 exercises (answers collapsed), Mermaid diagrams where a picture helps, and a "What to read next" list. Code shown in a chapter lives in `examples/chNN/` and runs with `make check`.

## Chapters

| # | Chapter | One line |
|---|---|---|
| 1 | [Requirements and failure modes](ch01-requirements-and-failure-modes.md) | Write the "what must never happen" list first; a checker turns it into testable requirements. |
| 2 | Choosing the data path | *(not yet written)* edge vs cloud, what to compute where, a decision table. |
| 3 | Data modelling | *(not yet written)* the telemetry record; status vs history vs events; schema evolution. |
| 4 | Firmware architecture | *(not yet written)* tasks, rates, shared state, the single interlock. |
| 5 | Safety layers that do not depend on the network | *(not yet written)* |
| 6 | Connectivity | *(not yet written)* Wi-Fi, MQTT, HTTPS, LoRaWAN, cellular; a selection table with power and cost. |
| 7 | Provisioning, identity and secrets | *(not yet written)* what the two real repos got wrong and how to fix it. |
| 8 | Cloud side | *(not yet written)* Firebase RTDB vs AWS IoT Core vs self-hosted MQTT + Timescale; cost at 10 / 1 000 / 100 000 devices. |
| 9 | Observability and OTA | *(not yet written)* |
| 10 | Case study | *(not yet written)* the farm monitor end to end with every artefact. |

## Running the examples

```bash
pip install ruff pytest
make check          # lint, tests, and every example script
```

Each `examples/chNN/` folder has its own README with the commands for that chapter.

## Licence

MIT, see [LICENSE](LICENSE).
