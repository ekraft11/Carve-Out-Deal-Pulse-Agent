"""Audit logging.

Every data access the engine makes is appended here as one JSON object per
line (a ".jsonl" file). Append-only: the engine never rewrites or deletes
history, so the log is a faithful record of what was read, when, and with
which parameters.

Each line carries:
    timestamp_utc   when the call happened (ISO 8601, UTC)
    run_id          which run it belonged to
    sequence        call order within that run
    kind            "tool_call" for data access, "event" for run milestones
    tool            the tool name (e.g. get_ownership)
    parameters      exactly what was asked for
    data_source     which backend answered (mock / gain / inven / factset)
    result_summary  a short, non-sensitive description of what came back
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Guardrails


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id(prefix: str = "run") -> str:
    """A readable, sortable run identifier, e.g. run-20260907-141203-a1b2c3."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class AuditLogger:
    """Append-only audit log writer."""

    log_path: Path
    run_id: str
    data_source: str = "mock"
    actor: str = "sourcing_engine"
    _sequence: int = 0

    @classmethod
    def for_run(
        cls,
        guardrails: Guardrails,
        run_id: str | None = None,
        command: str | None = None,
    ) -> "AuditLogger":
        """Build a logger from the guardrail config and open a run."""
        logger = cls(
            log_path=guardrails.audit_log_path,
            run_id=run_id or new_run_id(),
            data_source=guardrails.active_data_source,
        )
        logger.log_event(
            "run_started",
            {
                "command": command or "unspecified",
                "data_source": guardrails.active_data_source,
                "allowlisted_tools": list(guardrails.allowlisted_tools),
                "read_only": guardrails.read_only,
                "web_access_enabled": guardrails.web_access_enabled,
            },
        )
        return logger

    # -- writing -------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        record = {
            "timestamp_utc": _utc_now_iso(),
            "run_id": self.run_id,
            "sequence": self._sequence,
            "actor": self.actor,
            **record,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        # Append mode plus an explicit flush: if the process dies mid-run, the
        # calls made up to that point are still on disk.
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def log_tool_call(
        self,
        tool: str,
        parameters: dict[str, Any] | None = None,
        result_summary: str | None = None,
        data_source: str | None = None,
    ) -> dict[str, Any]:
        """Record one data access. Called for every tool invocation."""
        return self._append(
            {
                "kind": "tool_call",
                "tool": tool,
                "parameters": parameters or {},
                "data_source": data_source or self.data_source,
                "result_summary": result_summary,
            }
        )

    def log_event(self, event: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a run milestone (run started, screening finished, ...)."""
        return self._append(
            {
                "kind": "event",
                "event": event,
                "detail": detail or {},
            }
        )

    def log_blocked(self, tool: str, reason: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record an attempt that the guardrails refused."""
        return self._append(
            {
                "kind": "blocked",
                "tool": tool,
                "parameters": parameters or {},
                "reason": reason,
            }
        )

    @property
    def call_count(self) -> int:
        return self._sequence


# ---------------------------------------------------------------------------
# Reading the log back (used by the demo's audit view)
# ---------------------------------------------------------------------------


def read_entries(log_path: Path, run_id: str | None = None) -> list[dict[str, Any]]:
    """Read the audit log, optionally filtered to one run."""
    if not Path(log_path).exists():
        return []
    entries: list[dict[str, Any]] = []
    with Path(log_path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = {"kind": "unreadable", "line_number": line_number, "raw": line}
            if run_id is None or entry.get("run_id") == run_id:
                entries.append(entry)
    return entries


def iter_runs(log_path: Path) -> Iterator[str]:
    """Yield distinct run ids in the order they first appear."""
    seen: set[str] = set()
    for entry in read_entries(log_path):
        run_id = entry.get("run_id")
        if run_id and run_id not in seen:
            seen.add(run_id)
            yield run_id


def summarise_run(log_path: Path, run_id: str) -> dict[str, Any]:
    """Counts per tool for one run, for a quick 'what did the agent read' view."""
    entries = read_entries(log_path, run_id=run_id)
    tool_calls = [e for e in entries if e.get("kind") == "tool_call"]
    per_tool: dict[str, int] = {}
    for entry in tool_calls:
        per_tool[entry.get("tool", "?")] = per_tool.get(entry.get("tool", "?"), 0) + 1
    return {
        "run_id": run_id,
        "entries": len(entries),
        "tool_calls": len(tool_calls),
        "blocked": len([e for e in entries if e.get("kind") == "blocked"]),
        "per_tool": per_tool,
        "started_at": entries[0]["timestamp_utc"] if entries else None,
        "ended_at": entries[-1]["timestamp_utc"] if entries else None,
    }


def format_run_table(log_path: Path, run_id: str, limit: int | None = None) -> str:
    """Human-readable table of one run's tool calls, for terminal output."""
    entries = [e for e in read_entries(log_path, run_id=run_id) if e.get("kind") in ("tool_call", "blocked")]
    if not entries:
        return "(no tool calls recorded for this run)"
    shown = entries if limit is None else entries[:limit]
    rows = [f"{'#':>4}  {'TIMESTAMP (UTC)':<29}  {'TOOL':<18}  PARAMETERS"]
    rows.append("-" * 100)
    for entry in shown:
        params = json.dumps(entry.get("parameters", {}), ensure_ascii=False)
        if len(params) > 44:
            params = params[:41] + "..."
        marker = "BLOCKED " if entry.get("kind") == "blocked" else ""
        rows.append(
            f"{entry.get('sequence', '?'):>4}  {entry.get('timestamp_utc', '?'):<29}  "
            f"{entry.get('tool', '?'):<18}  {marker}{params}"
        )
    if limit is not None and len(entries) > limit:
        rows.append(f"      ... and {len(entries) - limit} more calls in {log_path.name}")
    return "\n".join(rows)
