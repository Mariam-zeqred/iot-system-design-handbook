"""Chapter 1 - turn a "what must never happen" list into checked requirements.

Usage:
    python never_list.py never_list.json            # gap report + table
    python never_list.py never_list.json --markdown # table only, for a README

The input is a JSON list of hazards. Each hazard says how it would be
detected, what the system does about it, and which parts of the system
that response depends on. The script derives one "shall" requirement per
hazard and reports the gaps a reviewer would otherwise find in the field:

* a hazard with no detection, no response or no test;
* a critical hazard whose response depends on the network, the cloud or
  the app (those are the parts that fail most often);
* a hazard that is guarded only by the same parts that can cause it
  (no independent check).

Exit code 1 when any gap exists, so the file can be checked in CI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

PARTS = {"sensor", "mcu", "actuator", "power", "network", "cloud", "app", "human"}
REMOTE_PARTS = {"network", "cloud", "app"}
SEVERITIES = ("critical", "major", "minor")


@dataclass
class Hazard:
    id: str
    hazard: str
    severity: str
    caused_by: list[str]
    detection: str = ""
    guarded_by: list[str] = field(default_factory=list)
    response: str = ""
    response_depends_on: list[str] = field(default_factory=list)
    test: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Hazard:
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)

    def requirement(self) -> str:
        """One testable 'shall' sentence derived from the hazard."""
        if not (self.detection and self.response):
            return "(incomplete: fill in detection and response first)"
        return (
            f"The system shall detect when {self.hazard.lower()} ({self.detection}) "
            f"and shall {self.response}."
        )

    def gaps(self) -> list[str]:
        found: list[str] = []
        if self.severity not in SEVERITIES:
            found.append(f"severity must be one of {SEVERITIES}")
        for name, parts in (
            ("caused_by", self.caused_by),
            ("guarded_by", self.guarded_by),
            ("response_depends_on", self.response_depends_on),
        ):
            unknown = set(parts) - PARTS
            if unknown:
                found.append(f"{name} has unknown parts {sorted(unknown)}")
        if not self.detection:
            found.append("no detection: how would the system know?")
        if not self.response:
            found.append("no response: what does the system do?")
        if not self.test:
            found.append("no test: how would you prove it before shipping?")
        if self.severity == "critical":
            remote = sorted(set(self.response_depends_on) & REMOTE_PARTS)
            if remote:
                found.append(
                    f"critical response depends on {remote}; "
                    "it must work with the network down"
                )
        if self.guarded_by and set(self.guarded_by) <= set(self.caused_by):
            found.append(
                "guarded only by the parts that can cause it; add an independent check"
            )
        return found


def load(path: Path) -> list[Hazard]:
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise TypeError("expected a JSON list of hazards")
    return [Hazard.from_dict(item) for item in raw]


def markdown_table(hazards: list[Hazard]) -> str:
    lines = [
        "| ID | Severity | Must never happen | Requirement | Needs network? |",
        "|---|---|---|---|---|",
    ]
    for h in hazards:
        needs_net = "yes" if set(h.response_depends_on) & REMOTE_PARTS else "no"
        lines.append(
            f"| {h.id} | {h.severity} | {h.hazard} | {h.requirement()} | {needs_net} |"
        )
    return "\n".join(lines)


def gap_report(hazards: list[Hazard]) -> tuple[str, int]:
    lines: list[str] = []
    total = 0
    for h in hazards:
        for gap in h.gaps():
            lines.append(f"{h.id}: {gap}")
            total += 1
    header = f"{len(hazards)} hazards, {total} gap(s)"
    return "\n".join([header, *lines]), total


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    hazards = load(Path(argv[1]))
    if "--markdown" in argv:
        print(markdown_table(hazards))
        return 0
    report, total = gap_report(hazards)
    print(report)
    print()
    print(markdown_table(hazards))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
