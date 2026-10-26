from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

BLOCK = "block"
WARN = "warn"
INFO = "info"

_ICON = {BLOCK: "BLOCK", WARN: "warn ", INFO: "info "}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    where: str = ""


@dataclass
class LintResult:
    subject: str
    findings: List[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def blocks(self) -> int:
        return sum(1 for f in self.findings if f.severity == BLOCK)

    @property
    def warns(self) -> int:
        return sum(1 for f in self.findings if f.severity == WARN)

    @property
    def passed(self) -> bool:
        return self.blocks == 0

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "passed": self.passed,
            "blocks": self.blocks,
            "warns": self.warns,
            "stats": self.stats,
            "findings": [f.__dict__ for f in self.findings],
        }

    def render(self) -> str:
        out = [f"{self.subject}: {'PASS' if self.passed else 'FAIL'} "
               f"({self.blocks} blocking, {self.warns} warnings)"]
        order = {BLOCK: 0, WARN: 1, INFO: 2}
        for f in sorted(self.findings, key=lambda x: order[x.severity]):
            loc = f" [{f.where}]" if f.where else ""
            out.append(f"  {_ICON[f.severity]} {f.rule}{loc}: {f.message}")
        return "\n".join(out)


class Collector:
    """Adds findings, applying severity overrides from the rules file."""

    def __init__(self, subject: str, overrides: dict):
        self.result = LintResult(subject)
        self._ov = overrides or {}

    def add(self, rule: str, severity: str, message: str, where: str = "") -> None:
        sev = self._ov.get(rule, severity)
        if sev == "off":
            return
        self.result.findings.append(Finding(rule, sev, message, where))
