# Chapter 10 example - the farm monitor's design record

```bash
python design.py bom bom.json          # parts and assumed prices per node class against the brief's budget
python design.py trace trace.json      # every decision traced to a chapter-1 row; exit 1 on a gap
python design.py cost bom.json         # hardware once + cloud and operations over 36 months, 10 / 1 000 devices
python design.py dossier design.json   # the whole design record as Markdown, every earlier chapter's tool re-run
python -m pytest -q                    # the tests
```

`bom.json` is the bill of materials for the greenhouse node, the plot node and
the gateway: every unit price is an **assumption** labelled as such, not a
quotation. `trace.json` names, for each decision of chapters 2-9, the hazard
(N1-N7, read from `../ch01/never_list.json`) or brief line (B1-B5) it answers
and the file that holds the artefact; the `trace` tool exits 1 if a decision
cites nothing, a row is unanswered, a critical row has no device-side response
or bench test, or an artefact is missing. `design.json` holds what no earlier
tool prints (quality numbers, stream and task tables as decided, the farm
monitor's own safety layers, fleet and rollout parameters, the hand-over
checklist); `dossier` prints it together with chapter 1's never-list, chapter
2's placement table, chapter 3's growth table, chapter 6's link selection,
chapter 7's inventory and chapter 8's bill, each produced by that chapter's own
script imported from its folder.

Nothing is measured: the cost tool is chapter 8's arithmetic on chapter 3's
records plus this chapter's assumed hardware prices; nothing was bought,
deployed or invoiced.
