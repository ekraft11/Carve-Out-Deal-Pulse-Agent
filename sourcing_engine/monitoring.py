"""The monitoring engine.

Screening asks "does this company fit our criteria". Monitoring asks "has
something happened".

Two separate judgements, deliberately kept apart:

1. Is a signal a *confirmed trigger*? A signal has to clear the confidence
   threshold, be corroborated, be recent enough, and fall in a known category.
   Every rejected signal keeps the reason it was rejected, so "why did we not
   act on that?" is answerable months later.

2. Does a confirmed trigger justify a *promotion proposal*? That depends on
   category weight, per triggers.promotion_policy in criteria.json. A founder
   opening a succession process stands on its own; a new CFO does not.

What monitoring never does is promote anything. It writes proposals. A human
approves them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import Criteria
from .data_sources.base import DataSource
from .models import Signal
from .screening import CLASS_ACTIVE, CLASS_EXCLUDED, ScreeningVerdict
from .state import PipelineState, PromotionRecord, STATUS_PENDING

# why a promotion was or was not proposed
OUTCOME_PROPOSED = "promotion_proposed"
OUTCOME_ALREADY_ACTIVE = "already_in_active_pipeline"
OUTCOME_EXCLUDED = "company_excluded"
OUTCOME_NO_TRIGGER = "no_confirmed_trigger"
OUTCOME_INSUFFICIENT = "supporting_signals_insufficient"
OUTCOME_PROPOSAL_OPEN = "proposal_already_awaiting_decision"
OUTCOME_PREVIOUSLY_DECLINED = "previously_declined_by_human"


@dataclass
class TriggerAssessment:
    """One signal, judged against the confirmation rules."""

    signal: Signal
    confirmed: bool
    weight: str  # standalone | supporting | unweighted
    age_days: int
    rejection_reasons: list[str] = field(default_factory=list)

    @property
    def verdict_text(self) -> str:
        if self.confirmed:
            return (
                f"CONFIRMED ({self.weight}): confidence {self.signal.confidence:.2f}, "
                f"corroborated by {len(self.signal.corroborating_sources)} sources, "
                f"{self.age_days} days old."
            )
        return "NOT CONFIRMED: " + " ".join(self.rejection_reasons)


@dataclass
class CompanyTriggerStatus:
    """The trigger picture for one company, and what follows from it."""

    company_id: str
    company_name: str
    classification: str
    structural_fit: bool
    assessments: list[TriggerAssessment] = field(default_factory=list)
    outcome: str = OUTCOME_NO_TRIGGER
    outcome_detail: str = ""
    proposal: PromotionRecord | None = None

    @property
    def confirmed(self) -> list[TriggerAssessment]:
        return [a for a in self.assessments if a.confirmed]

    @property
    def standalone_confirmed(self) -> list[TriggerAssessment]:
        return [a for a in self.confirmed if a.weight == "standalone"]

    @property
    def supporting_confirmed(self) -> list[TriggerAssessment]:
        return [a for a in self.confirmed if a.weight == "supporting"]

    @property
    def summary(self) -> str:
        """One line for the watchlist's 'Trigger status' column."""
        if not self.assessments:
            return "no signals on file"
        confirmed = self.confirmed
        if not confirmed:
            return (
                f"{len(self.assessments)} signal(s) on file, none confirmed "
                f"({self.outcome_detail})"
            )
        categories = ", ".join(sorted({a.signal.category for a in confirmed}))
        return f"{len(confirmed)} confirmed trigger(s): {categories} - {self.outcome_detail}"


class TriggerMonitor:
    """Assesses signals and proposes promotions. Never executes one."""

    def __init__(
        self,
        criteria: Criteria,
        source: DataSource,
        state: PipelineState,
        as_of: date,
    ) -> None:
        self.criteria = criteria
        self.source = source
        self.state = state
        self.as_of = as_of

    # -- signal level --------------------------------------------------------

    def _weight_of(self, category: str) -> str:
        if category in self.criteria.standalone_trigger_categories:
            return "standalone"
        if category in self.criteria.supporting_trigger_categories:
            return "supporting"
        return "unweighted"

    def assess_signal(self, signal: Signal) -> TriggerAssessment:
        """Apply the confirmation rules to one signal."""
        reasons: list[str] = []
        age = signal.age_days(self.as_of)

        if signal.category not in self.criteria.trigger_categories:
            reasons.append(
                f"Category '{signal.category}' is not one of the four monitored "
                f"categories."
            )
        if age > self.criteria.max_signal_age_days:
            reasons.append(
                f"Observed {age} days ago, beyond the "
                f"{self.criteria.max_signal_age_days}-day window."
            )
        if signal.confidence < self.criteria.min_trigger_confidence:
            reasons.append(
                f"Confidence {signal.confidence:.2f} is below the "
                f"{self.criteria.min_trigger_confidence:.2f} threshold."
            )
        if self.criteria.require_corroboration and not signal.corroborated:
            reasons.append(
                f"Single-source and uncorroborated "
                f"({len(signal.corroborating_sources)} corroborating source(s))."
            )

        return TriggerAssessment(
            signal=signal,
            confirmed=not reasons,
            weight=self._weight_of(signal.category),
            age_days=age,
            rejection_reasons=reasons,
        )

    # -- company level -------------------------------------------------------

    def scan(self, verdicts: list[ScreeningVerdict]) -> list[CompanyTriggerStatus]:
        """Assess every company's signals and decide what to propose."""
        statuses: list[CompanyTriggerStatus] = []
        for verdict in verdicts:
            signals = self.source.get_signals(company_id=verdict.company_id)
            if not signals and verdict.classification == CLASS_EXCLUDED:
                continue  # nothing to say about an excluded company with no signals
            status = CompanyTriggerStatus(
                company_id=verdict.company_id,
                company_name=verdict.name,
                classification=verdict.classification,
                structural_fit=verdict.structural_fit,
                assessments=[self.assess_signal(s) for s in signals],
            )
            self._decide(status, verdict)
            statuses.append(status)
        return statuses

    def _decide(self, status: CompanyTriggerStatus, verdict: ScreeningVerdict) -> None:
        """Work out whether a promotion proposal follows - and record why."""
        required = self.criteria.supporting_signals_required

        # A confirmed trigger on a company that fails the screen changes
        # nothing. This is the case worth showing in a demo: a strong,
        # well-corroborated succession signal on a PE-majority company must
        # not produce a proposal.
        if not status.structural_fit:
            status.outcome = OUTCOME_EXCLUDED
            reason = verdict.exclusion_reasons[0] if verdict.exclusion_reasons else ""
            confirmed = len(status.confirmed)
            status.outcome_detail = (
                f"No promotion: the company does not pass the screen. {reason}"
                + (
                    f" {confirmed} confirmed trigger(s) recorded for the file, "
                    f"but a trigger cannot override a structural exclusion."
                    if confirmed
                    else ""
                )
            )
            return

        if verdict.classification == CLASS_ACTIVE:
            status.outcome = OUTCOME_ALREADY_ACTIVE
            status.outcome_detail = "Already in the active pipeline; nothing to propose."
            return

        existing = self.state.record_for(status.company_id)
        if existing is not None and existing.status == STATUS_PENDING:
            status.outcome = OUTCOME_PROPOSAL_OPEN
            status.outcome_detail = (
                f"A promotion proposal from {existing.proposed_on} is already "
                f"awaiting a decision; not proposed again."
            )
            return
        if status.company_id in self.state.declined_ids:
            status.outcome = OUTCOME_PREVIOUSLY_DECLINED
            status.outcome_detail = (
                "A promotion was previously declined by a reviewer; not re-proposed "
                "automatically."
            )
            return

        standalone = status.standalone_confirmed
        supporting = status.supporting_confirmed

        if standalone:
            driver = max(standalone, key=lambda a: a.signal.confidence)
            status.outcome = OUTCOME_PROPOSED
            status.outcome_detail = (
                f"Promotion PROPOSED on a standalone trigger: "
                f"{driver.signal.category}."
            )
            status.proposal = self._build_proposal(status, [driver] + supporting, driver)
            return

        if len(supporting) >= required:
            driver = max(supporting, key=lambda a: a.signal.confidence)
            status.outcome = OUTCOME_PROPOSED
            status.outcome_detail = (
                f"Promotion PROPOSED on {len(supporting)} supporting triggers "
                f"({required} required)."
            )
            status.proposal = self._build_proposal(status, supporting, driver)
            return

        if supporting:
            status.outcome = OUTCOME_INSUFFICIENT
            categories = ", ".join(a.signal.category for a in supporting)
            status.outcome_detail = (
                f"No promotion: {len(supporting)} confirmed supporting trigger "
                f"({categories}) but {required} are required. A supporting signal "
                f"on its own is not a moment of opportunity. Stays on the watchlist."
            )
            return

        status.outcome = OUTCOME_NO_TRIGGER
        if status.assessments:
            reasons = {r for a in status.assessments for r in a.rejection_reasons}
            status.outcome_detail = (
                "No confirmed trigger. " + " ".join(sorted(reasons))
            )
        else:
            status.outcome_detail = "No signals on file. Stays on the watchlist."

    def _build_proposal(
        self,
        status: CompanyTriggerStatus,
        assessments: list[TriggerAssessment],
        driver: TriggerAssessment,
    ) -> PromotionRecord:
        return PromotionRecord(
            company_id=status.company_id,
            company_name=status.company_name,
            status=STATUS_PENDING,
            trigger_signal_ids=[a.signal.signal_id for a in assessments],
            trigger_category=driver.signal.category,
            trigger_headline=driver.signal.headline,
            proposed_on=self.as_of.isoformat(),
            proposed_by="sourcing_engine (monitoring run)",
            note=(
                f"{status.outcome_detail} Evidence: {driver.signal.detail} "
                f"Confidence {driver.signal.confidence:.2f}, corroborated by "
                f"{', '.join(driver.signal.corroborating_sources)}. "
                f"AWAITING HUMAN APPROVAL - the engine has not promoted this company."
            ),
        )


@dataclass
class MonitoringResult:
    """The whole monitoring run."""

    statuses: list[CompanyTriggerStatus]
    as_of: date
    run_id: str

    @property
    def proposals(self) -> list[PromotionRecord]:
        return [s.proposal for s in self.statuses if s.proposal is not None]

    @property
    def confirmed_count(self) -> int:
        return sum(len(s.confirmed) for s in self.statuses)

    @property
    def assessed_count(self) -> int:
        return sum(len(s.assessments) for s in self.statuses)

    def trigger_status_by_company(self) -> dict[str, str]:
        return {s.company_id: s.summary for s in self.statuses}

    def status_for(self, company_id: str) -> CompanyTriggerStatus | None:
        for status in self.statuses:
            if status.company_id == company_id:
                return status
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.isoformat(),
            "signals_assessed": self.assessed_count,
            "triggers_confirmed": self.confirmed_count,
            "promotions_proposed": len(self.proposals),
        }
