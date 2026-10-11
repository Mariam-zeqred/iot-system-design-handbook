# Chapter 1 example - the never-list checker

```bash
python never_list.py never_list.json             # gap report + requirement table, exit 1 if gaps
python never_list.py never_list.json --markdown  # table only
python -m pytest -q                              # four tests
```

`never_list.json` is the farm monitor's "what must never happen" list from
[chapter 1](../../ch01-requirements-and-failure-modes.md). Hazard N7 is left
incomplete on purpose so the gap report has something to show.
