"""Pipeline state.

A JSON file, not a database. It holds the things a human decided, which is
exactly the information the engine must not re-derive on its own:

  approved_promotions  companies a human moved into the active pipeline
  pending_proposals    promotion proposals waiting for a decision
  declined_proposals   proposals a human rejected, so they are not re-proposed

Screening and monitoring are otherwise stateless: they recompute from the data
source every run. That means a criteria change takes effect immediately, while
human decisions survive.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, Guardrails

SEED_STATE_PATH = DATA_DIR / "seed_pipeline_state.json"
STATE_FILENAME = "pipeline_state.json"

STATUS_PENDING = "pending_human_approval"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"


@dataclass
class PromotionRecord:
    """A watchlist-to-pipeline promotion, proposed or decided."""

    company_id: str
    company_name: str
    status: str
    trigger_signal_ids: list[str] = field(default_factory=list)
    trigger_category: str = ""
    trigger_headline: str = ""
    proposed_on: str = ""
    proposed_by: str = "sourcing_engine (monitoring run)"
    decided_on: str = ""
    decided_by: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "PromotionRecord":
        return cls(
            company_id=str(record["company_id"]),
            company_name=str(record.get("company_name", "")),
            status=str(record.get("status", STATUS_PENDING)),
            trigger_signal_ids=list(record.get("trigger_signal_ids", [])),
            trigger_category=record.get("trigger_category", ""),
            trigger_headline=record.get("trigger_headline", ""),
            proposed_on=record.get("proposed_on", ""),
            proposed_by=record.get("proposed_by", ""),
            decided_on=record.get("decided_on", ""),
            decided_by=record.get("decided_by", ""),
            note=record.get("note", ""),
        )


@dataclass
class PipelineState:
    """Everything the engine remembers between runs."""

    promotions: list[PromotionRecord] = field(default_factory=list)
    last_screening_run: str = ""
    last_monitoring_run: str = ""
    data_label: str = "MOCK DATA"
    path: Path | None = None

    # -- queries -------------------------------------------------------------

    @property
    def approved_ids(self) -> set[str]:
        return {p.company_id for p in self.promotions if p.status == STATUS_APPROVED}

    @property
    def pending(self) -> list[PromotionRecord]:
        return [p for p in self.promotions if p.status == STATUS_PENDING]

    @property
    def declined_ids(self) -> set[str]:
        return {p.company_id for p in self.promotions if p.status == STATUS_DECLINED}

    def record_for(self, company_id: str) -> PromotionRecord | None:
        for promotion in self.promotions:
            if promotion.company_id == company_id:
                return promotion
        return None

    # -- mutation (only ever called with a human decision or a proposal) -----

    def propose(self, record: PromotionRecord) -> bool:
        """Add a pending proposal. Returns False if one already exists."""
        existing = self.record_for(record.company_id)
        if existing is not None:
            return False
        record.status = STATUS_PENDING
        self.promotions.append(record)
        return True

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, guardrails: Guardrails) -> "PipelineState":
        """Load state, seeding from the mock seed file on a first run."""
        path = guardrails.state_dir / STATE_FILENAME
        if not path.exists() and SEED_STATE_PATH.exists():
            raw = json.loads(SEED_STATE_PATH.read_text(encoding="utf-8"))
        elif path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = {}
        state = cls(
            promotions=[PromotionRecord.from_dict(r) for r in raw.get("promotions", [])],
            last_screening_run=raw.get("last_screening_run", ""),
            last_monitoring_run=raw.get("last_monitoring_run", ""),
            data_label=raw.get("data_label", "MOCK DATA"),
            path=path,
        )
        return state

    def save(self, guardrails: Guardrails) -> Path:
        path = self.path or (guardrails.state_dir / STATE_FILENAME)
        guardrails.assert_write_allowed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Human decisions and proposal history for the sourcing engine. "
                "Screening and monitoring recompute everything else from the data "
                "source on each run."
            ),
            "data_label": self.data_label,
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_screening_run": self.last_screening_run,
            "last_monitoring_run": self.last_monitoring_run,
            "promotions": [asdict(p) for p in self.promotions],
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path
