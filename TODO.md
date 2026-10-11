# TODO

## Open

- ch01 is over the STYLE.md length cap: 3 408 prose words with code fences stripped (`awk '/^```/{f=!f;next} !f' ch01-*.md | wc -w`; limit 1 500–3 000). Candidates to trim without losing the method: section 7 (two real never-lists) could lose its second paragraph's restatement of Rule A, and the exercise answers 1 and 2 could be shortened by about a third.
- README.md is 33 lines (STYLE.md asks 80–200). Acceptable while only one chapter exists; revisit once chapters 2–4 land (add the farm-monitor data-path Mermaid diagram and a short "Status / limitations" section).
