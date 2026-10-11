# Walkthrough (for Mariam)

This file is for you, not for readers of the course. It grows by one short section per chapter.

## Why the course is shaped this way

The audience is "can code, has never shipped a device-to-cloud system", which is where most master's applicants and most junior engineers are. Every chapter produces one artefact (a list, a table, a diagram, a schema) that chapter 10 assembles into a full design, so the course reads as one project rather than ten essays. Your two real repos are used as *evidence*, quoted from their READMEs, never embellished: when a chapter says the incubator's safety task runs at 10 Hz or the poultry controller hands control to the LCD after 90 s, that is because the README says so.

## File map

- `chNN-<slug>.md` — one chapter. 1 500–3 000 words, one worked example, exercises with collapsed answers, "What to read next".
- `examples/chNN/` — every piece of code a chapter shows, runnable, with a README and tests where it makes sense. `make check` runs all of them.
- `README.md` — table of contents; a chapter's row is filled in when the chapter lands.

## Chapter 1 - Requirements and failure modes

*What it teaches.* Write the "what must never happen" list before the feature list; walk the data path asking "lies / dies / late"; two rules (a critical response may not depend on network/cloud/app; a hazard may not be guarded only by what can cause it); derive "shall" sentences and numbers from the list.

*The example.* `examples/ch01/never_list.py` reads `never_list.json` and prints a gap report. Hazard N7 (open database access) is deliberately incomplete so the report has something to show; exercise 1 completes it. The N4 story in the chapter (pull-down resistors on relay pins) is a genuine case of the rule catching a design hole.

*Questions a professor may ask.*
1. *Isn't this just FMEA / STPA?* Yes, a deliberately small subset: one hazard per row, two mechanical rules, no probability column. The chapter cites the STPA handbook as the full method. Say that you kept the part a two-person startup will actually maintain.
2. *Why must a critical response not depend on the cloud?* Because the three remote parts fail together and most often; the incubator and poultry READMEs both describe on-device safety for exactly this reason, and the poultry controller's 90-second LCD takeover is the concrete example.
3. *Where do the numbers in section 6 come from?* Each has a "comes from" column; the ones that are engineering judgement (±0.5 °C, 10 min offline) are labelled as derived from a hazard, not measured. Be ready to say which you would revisit with field data.

*One thing you could extend.* Add a `--diagram` flag to the checker that emits a Mermaid graph from `caused_by` and `guarded_by`, so the data-path figure in the chapter is generated rather than drawn.
