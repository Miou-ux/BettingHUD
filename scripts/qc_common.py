"""Structures communes pour les contrôles qualité (QC) matinaux."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QcIssue:
    level: str  # "blocking" | "warning"
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class QcReport:
    name: str
    blocking: list[QcIssue] = field(default_factory=list)
    warnings: list[QcIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def add_blocking(self, code: str, message: str, **ctx: Any) -> None:
        self.blocking.append(QcIssue("blocking", code, message, dict(ctx)))

    def add_warning(self, code: str, message: str, **ctx: Any) -> None:
        self.warnings.append(QcIssue("warning", code, message, dict(ctx)))

    def merge(self, other: QcReport) -> None:
        self.blocking.extend(other.blocking)
        self.warnings.extend(other.warnings)

    def summary_lines(self) -> list[str]:
        lines = [f"[QC {self.name}] blocking={len(self.blocking)} warnings={len(self.warnings)}"]
        for issue in self.blocking:
            lines.append(f"  BLOCK {issue.code}: {issue.message}")
        for issue in self.warnings:
            lines.append(f"  WARN {issue.code}: {issue.message}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "blocking": [
                {"code": i.code, "message": i.message, "context": i.context} for i in self.blocking
            ],
            "warnings": [
                {"code": i.code, "message": i.message, "context": i.context} for i in self.warnings
            ],
        }
