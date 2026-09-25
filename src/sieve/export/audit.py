"""Audit logging (PRD non-functional requirement 3).

Every scoring decision, flag assignment and committee promotion is written to
`sieve_audit_log.jsonl`, one JSON object per line, with a timestamp and the
execution context that produced it.

The log is append-only and streamed, so a 5,000-row run never holds the whole
log in memory. Timestamps are the one non-deterministic element in the system,
which is why they live here and nowhere near a scored value.
"""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

__all__ = ["AuditLog", "ExecutionContext"]


class ExecutionContext:
    """Identifies one run, so log lines can be grouped after the fact."""

    def __init__(self, run_id: str, rubric_version: str, taxonomy_version: str) -> None:
        self.run_id = run_id
        self.rubric_version = rubric_version
        self.taxonomy_version = taxonomy_version
        self.started_at = datetime.now(timezone.utc)
        self.host = platform.node()
        self.pid = os.getpid()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "rubric_version": self.rubric_version,
            "taxonomy_version": self.taxonomy_version,
            "started_at": self.started_at.isoformat(),
            "host": self.host,
            "pid": self.pid,
        }


class AuditLog:
    """Streams decisions to a JSONL file.

    Used as a context manager::

        with AuditLog(path, context) as log:
            log.record("score", account_id=..., score=..., breakdown=[...])

    Passing no path makes every call a no-op, so callers do not branch on
    whether logging is enabled.
    """

    def __init__(self, path: Path | str | None, context: ExecutionContext) -> None:
        self.path = Path(path) if path else None
        self.context = context
        self._handle: TextIO | None = None
        self.lines_written = 0

    def __enter__(self) -> "AuditLog":
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
            self.record("run_started", **self.context.as_dict())
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            self.record("run_finished", lines=self.lines_written)
            self._handle.close()
            self._handle = None

    def record(self, event: str, **payload: Any) -> None:
        """Append one event. Silently does nothing when logging is off."""
        if self._handle is None:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.context.run_id,
            "event": event,
            **payload,
        }
        self._handle.write(json.dumps(entry, default=str) + "\n")
        self.lines_written += 1
