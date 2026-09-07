"""Turning results into sheets and markdown.

Kept separate from the screening logic on purpose: the rules decide what is
true, this module decides how it is presented. Changing a column heading
should never risk changing a classification.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .audit import read_entries
from .config import Criteria, Guardrails, OutputConfig
from .screening import CLASS_ACTIVE, CLASS_EXCLUDED, CLASS_WATCHLIST, ScreeningResult
from .state import PipelineState
from .workbook import SheetSpec

YES_NO = {True: "yes", False: "no"}

SCREENING_HEADERS = [
    "Company ID",
    "Company",
    "Country",
    "Region",
    "Geography tier",
    "Subsector",
    "Revenue (EUR m)",
    "EBITDA (EUR m)",
    "Listed",
    "Control type",
    "Family/founder %",
    "PE %",
    "Classification",
    "Structural fit",
    "Needs human review",
    "Reason",
    "Review flags",
    "Screened on",
    "Data label",
]

SCREENING_WIDTHS = {
    "Company ID": 11,
    "Company": 31,
    "Country": 8,
    "Region": 11,
    "Geography tier": 13,
    "Subsector": 27,
    "Revenue (EUR m)": 14,
    "EBITDA (EUR m)": 13,
    "Listed": 8,
    "Control type": 30,
    "Family/founder %": 15,
    "PE %": 8,
    "Classification": 16,
    "Structural fit": 12,
    "Needs human review": 15,
    "Reason": 82,
    "Review flags": 60,
    "Screened on": 12,
    "Data label": 11,
}

WATCHLIST_HEADERS = [
    "Company ID",
    "Company",
    "Country",
    "EBITDA (EUR m)",
    "Control type",
    "Family/founder %",
    "PE %",
    "Needs human review",
    "Trigger status",
    "Reason",
]

ACTIVE_HEADERS = [
    "Company ID",
    "Company",
    "Country",
    "EBITDA (EUR m)",
    "Control type",
    "Approved on",
    "Approved by",
    "Trigger category",
    "Trigger",
    "Reason",
]

AUDIT_HEADERS = [
    "Timestamp (UTC)",
    "Run ID",
    "Seq",
    "Kind",
    "Tool",
    "Parameters",
    "Data source",
    "Result summary",
]


def _num(value: float | None) -> Any:
    return "" if value is None else round(float(value), 1)


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


def screening_sheet(result: ScreeningResult, decision_options: Sequence[str]) -> SheetSpec:
    rows = []
    for verdict in result.verdicts:
        rows.append([
            verdict.company_id,
            verdict.name,
            verdict.country,
            verdict.region or "-",
            verdict.tier or "out of scope",
            verdict.subsector,
            _num(verdict.revenue_eur_m),
            _num(verdict.ebitda_eur_m),
            YES_NO[verdict.listing_status != "private"],
            verdict.control_type,
            _num(verdict.family_founder_stake_pct),
            _num(verdict.pe_stake_pct),
            verdict.classification,
            YES_NO[verdict.structural_fit],
            YES_NO[verdict.needs_human_review],
            verdict.reason,
            " | ".join(verdict.review_flags),
            verdict.screened_on,
            verdict.data_label,
        ])
    return SheetSpec(
        name="01_Screening",
        headers=SCREENING_HEADERS,
        rows=rows,
        key_header="Company ID",
        tracked_headers=("Classification", "Needs human review", "EBITDA (EUR m)"),
        widths=SCREENING_WIDTHS,
        wrap_headers=("Reason", "Review flags"),
        dropdowns={"Entscheidung": decision_options},
        status_header="Classification",
    )


def watchlist_sheet(
    result: ScreeningResult,
    decision_options: Sequence[str],
    trigger_status: dict[str, str] | None = None,
) -> SheetSpec:
    trigger_status = trigger_status or {}
    rows = []
    for verdict in result.by_classification(CLASS_WATCHLIST):
        rows.append([
            verdict.company_id,
            verdict.name,
            verdict.country,
            _num(verdict.ebitda_eur_m),
            verdict.control_type,
            _num(verdict.family_founder_stake_pct),
            _num(verdict.pe_stake_pct),
            YES_NO[verdict.needs_human_review],
            trigger_status.get(
                verdict.company_id, "not scanned in this run - run monitoring"
            ),
            verdict.reason,
        ])
    return SheetSpec(
        name="02_Watchlist",
        headers=WATCHLIST_HEADERS,
        rows=rows,
        key_header="Company ID",
        tracked_headers=("Trigger status",),
        widths={**SCREENING_WIDTHS, "Trigger status": 44, "Reason": 82},
        wrap_headers=("Reason", "Trigger status"),
        dropdowns={"Entscheidung": decision_options},
    )


def active_pipeline_sheet(
    result: ScreeningResult,
    state: PipelineState,
    decision_options: Sequence[str],
) -> SheetSpec:
    rows = []
    for verdict in result.by_classification(CLASS_ACTIVE):
        record = state.record_for(verdict.company_id)
        rows.append([
            verdict.company_id,
            verdict.name,
            verdict.country,
            _num(verdict.ebitda_eur_m),
            verdict.control_type,
            record.decided_on if record else "",
            record.decided_by if record else "",
            record.trigger_category if record else "",
            record.trigger_headline if record else "",
            verdict.reason,
        ])
    return SheetSpec(
        name="03_Active_Pipeline",
        headers=ACTIVE_HEADERS,
        rows=rows,
        key_header="Company ID",
        tracked_headers=("Approved on",),
        widths={**SCREENING_WIDTHS, "Trigger": 60, "Approved by": 28, "Approved on": 12},
        wrap_headers=("Reason", "Trigger"),
        dropdowns={"Entscheidung": decision_options},
        status_header=None,
    )


def audit_sheet(guardrails: Guardrails, run_id: str) -> SheetSpec:
    """This run's data access, appended to whatever the file already holds."""
    rows = []
    for entry in read_entries(guardrails.audit_log_path, run_id=run_id):
        parameters = entry.get("parameters", entry.get("detail", {}))
        rows.append([
            entry.get("timestamp_utc", ""),
            entry.get("run_id", ""),
            entry.get("sequence", ""),
            entry.get("kind", ""),
            entry.get("tool", entry.get("event", "")),
            _compact(parameters),
            entry.get("data_source", ""),
            entry.get("result_summary", entry.get("reason", "")),
        ])
    return SheetSpec(
        name="07_Audit_Trail",
        headers=AUDIT_HEADERS,
        rows=rows,
        append_only=True,
        widths={
            "Timestamp (UTC)": 22,
            "Run ID": 26,
            "Seq": 6,
            "Kind": 11,
            "Tool": 20,
            "Parameters": 44,
            "Data source": 12,
            "Result summary": 52,
        },
        wrap_headers=("Parameters", "Result summary"),
    )


def _compact(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def readme_lines(
    criteria: Criteria,
    guardrails: Guardrails,
    result: ScreeningResult | None,
    state: PipelineState,
    source_label: str,
    command: str,
) -> list[tuple[str, str]]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[tuple[str, str]] = [
        ("THIS RUN", ""),
        ("Generated", now),
        ("Command", f"python run.py {command}"),
        ("Data source", source_label),
        ("Criteria version", criteria.version),
        ("Audit log", f"./{guardrails.audit_log_path.name} (every data access, append-only)"),
        ("", ""),
        ("SCREENING CRITERIA APPLIED", ""),
        ("Minimum EBITDA", f"EUR {criteria.min_ebitda:.0f}m "
                           f"(flagged for review between EUR "
                           f"{criteria.ebitda_borderline_band[0]:.0f}m and EUR "
                           f"{criteria.ebitda_borderline_band[1]:.0f}m; "
                           f"excluded below EUR {criteria.ebitda_hard_floor:.0f}m)"),
        ("Ownership", f"Family or founder owned, at least "
                      f"{criteria.min_family_stake:.0f}%. PE minority acceptable up to "
                      f"{criteria.max_pe_stake:.1f}%; PE majority excluded"),
        ("Geography", "DACH and Nordics primary, UK and Benelux secondary; "
                      "anything else out of scope"),
        ("Listing", "Any listed company is excluded regardless of how concentrated "
                    "its ownership is"),
        ("Promotion rule", "Structural fit alone stays on the watchlist. The active "
                           "pipeline requires a confirmed behavioural trigger AND "
                           "human approval"),
        ("", ""),
        ("HOW TO READ THIS FILE", ""),
        ("01_Screening", "Every company in the universe with its classification and a "
                         "written reason"),
        ("02_Watchlist", "Structural fit, no confirmed trigger yet"),
        ("03_Active_Pipeline", "Human-approved promotions only"),
        ("04_Trigger_Scan", "Every signal assessed, with why it did or did not count "
                            "as a confirmed trigger (written by `run monitoring`)"),
        ("05_Promotion_Proposals", "Proposals waiting for your decision "
                                   "(written by `run monitoring`)"),
        ("06_Profiles", "Profile, executive bios and outreach readiness per active "
                        "company (written by `run monitoring`)"),
        ("07_Audit_Trail", "Every data access, appended run after run"),
        ("08_Changelog", "What the engine changed between runs, appended"),
    ]
    if result is not None:
        counts = result.counts
        lines.extend([
            ("", ""),
            ("THIS RUN IN NUMBERS", ""),
            ("Companies screened", str(len(result.verdicts))),
            ("Flagged for human review", str(len(result.flagged))),
            ("Pending promotion proposals", str(len(state.pending))),
        ])
        del counts
    return lines


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------


def write_screening_markdown(
    result: ScreeningResult,
    criteria: Criteria,
    guardrails: Guardrails,
    output: OutputConfig,
    source_label: str,
) -> Path:
    """A short readable companion to the workbook."""
    path = guardrails.review_queue_dir / "screening_summary.md"
    guardrails.assert_write_allowed(path)
    counts = result.counts
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Screening summary",
        "",
        "> **MOCK DATA** - every company, person and figure below is fictional.",
        "> Verify anything before relying on it or sharing it.",
        "",
        f"- Generated: {now}",
        f"- Data source: {source_label}",
        f"- Criteria version: {criteria.version}",
        f"- Companies screened: {len(result.verdicts)}",
        "",
        "## Result",
        "",
        "| Bucket | Companies |",
        "|---|---|",
        f"| Active pipeline (human-approved) | {counts.get(CLASS_ACTIVE, 0)} |",
        f"| Watchlist | {counts.get(CLASS_WATCHLIST, 0)} |",
        f"| Excluded | {counts.get(CLASS_EXCLUDED, 0)} |",
        f"| **Flagged for human review** | **{len(result.flagged)}** |",
        "",
        "Structural fit alone never enters the active pipeline. A company reaches it",
        "only through a confirmed behavioural trigger plus human approval.",
        "",
        "## Flagged for human review",
        "",
    ]
    if result.flagged:
        for verdict in result.flagged:
            lines.append(f"### {verdict.name} ({verdict.company_id}) - {verdict.classification}")
            lines.append("")
            for flag in verdict.review_flags:
                lines.append(f"- {flag}")
            lines.append("")
    else:
        lines.append("_None._")
        lines.append("")

    lines.extend(["## Excluded, with reasons", ""])
    for verdict in result.by_classification(CLASS_EXCLUDED):
        lines.append(f"- **{verdict.name}** ({verdict.company_id}): {verdict.reason}")
    lines.append("")
    lines.extend([
        "## Watchlist",
        "",
    ])
    for verdict in result.by_classification(CLASS_WATCHLIST):
        flag = " *(flagged)*" if verdict.needs_human_review else ""
        ebitda = "n/a" if verdict.ebitda_eur_m is None else f"EUR {verdict.ebitda_eur_m:.1f}m"
        lines.append(
            f"- **{verdict.name}** ({verdict.company_id}), {verdict.country}, "
            f"{ebitda}, {verdict.control_type}{flag}"
        )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Monitoring sheets
# ---------------------------------------------------------------------------

TRIGGER_HEADERS = [
    "Signal ID",
    "Company ID",
    "Company",
    "Classification",
    "Category",
    "Weight",
    "Headline",
    "Observed on",
    "Age (days)",
    "Confidence",
    "Corroborated",
    "Sources",
    "Confirmed trigger",
    "Assessment",
    "Consequence",
    "Detail",
]

PROPOSAL_HEADERS = [
    "Company ID",
    "Company",
    "Proposed on",
    "Proposed by",
    "Status",
    "Trigger category",
    "Trigger",
    "Signals",
    "Why proposed",
    "Freigabe",
    "Freigabe durch",
    "Freigabe am",
]

PROFILE_HEADERS = [
    "Company ID",
    "Company",
    "Profile",
    "Executive bios",
    "Outreach readiness",
    "Trigger status",
    "Promotion record",
    "Markdown file",
]


def trigger_scan_sheet(statuses) -> SheetSpec:
    """Every signal assessed, with why it did or did not count."""
    rows = []
    for status in statuses:
        for assessment in status.assessments:
            signal = assessment.signal
            rows.append([
                signal.signal_id,
                status.company_id,
                status.company_name,
                status.classification,
                signal.category,
                assessment.weight,
                signal.headline,
                signal.observed_on.isoformat(),
                assessment.age_days,
                round(signal.confidence, 2),
                YES_NO[signal.corroborated],
                "; ".join(signal.corroborating_sources) or "none",
                YES_NO[assessment.confirmed],
                assessment.verdict_text,
                status.outcome_detail,
                signal.detail,
            ])
    rows.sort(key=lambda r: (r[1], r[7]), reverse=False)
    return SheetSpec(
        name="04_Trigger_Scan",
        headers=TRIGGER_HEADERS,
        rows=rows,
        key_header="Signal ID",
        tracked_headers=("Confirmed trigger",),
        widths={
            "Signal ID": 14, "Company ID": 11, "Company": 30, "Classification": 15,
            "Category": 26, "Weight": 12, "Headline": 56, "Observed on": 12,
            "Age (days)": 10, "Confidence": 11, "Corroborated": 12, "Sources": 44,
            "Confirmed trigger": 12, "Assessment": 58, "Consequence": 62, "Detail": 76,
        },
        wrap_headers=("Headline", "Assessment", "Consequence", "Detail", "Sources"),
    )


#: state status -> the word shown in the reviewer's approval column
STATUS_TO_WORD = {
    "approved": "Freigegeben",
    "declined": "Abgelehnt",
    "pending_human_approval": "",
}


def promotion_proposals_sheet(state, approval_options) -> SheetSpec:
    """Proposals and decided promotions. The engine proposes; you decide.

    The `Freigabe` column is the one place a human instruction travels back
    into the engine: type a decision there, re-run, and the engine records it.
    The cell is then rewritten from the recorded state, so the sheet and the
    state file cannot drift apart.
    """
    rows = []
    for record in state.promotions:
        rows.append([
            record.company_id,
            record.company_name,
            record.proposed_on,
            record.proposed_by,
            record.status,
            record.trigger_category,
            record.trigger_headline,
            ", ".join(record.trigger_signal_ids),
            record.note,
            STATUS_TO_WORD.get(record.status, ""),
            record.decided_by,
            record.decided_on,
        ])
    return SheetSpec(
        name="05_Promotion_Proposals",
        headers=PROPOSAL_HEADERS,
        rows=rows,
        key_header="Company ID",
        tracked_headers=("Status",),
        widths={
            "Company ID": 11, "Company": 30, "Proposed on": 12, "Proposed by": 30,
            "Status": 22, "Trigger category": 20, "Trigger": 56, "Signals": 26,
            "Why proposed": 92, "Freigabe": 16, "Freigabe durch": 20, "Freigabe am": 13,
        },
        wrap_headers=("Trigger", "Why proposed"),
        dropdowns={"Freigabe": approval_options},
        status_header="Status",
    )


def profiles_sheet(packs) -> SheetSpec:
    """One row per active company; the full text lives in the markdown files."""
    rows = []
    for pack in packs:
        rows.append([
            pack.company_id,
            pack.company_name,
            pack.profile_text,
            pack.bios_text,
            pack.outreach_text,
            pack.trigger_summary,
            pack.approval_note,
            f"review_queue/profiles/{pack.markdown_path.name}" if pack.markdown_path else "",
        ])
    return SheetSpec(
        name="06_Profiles",
        headers=PROFILE_HEADERS,
        rows=rows,
        key_header="Company ID",
        widths={
            "Company ID": 11, "Company": 30, "Profile": 110, "Executive bios": 96,
            "Outreach readiness": 60, "Trigger status": 50, "Promotion record": 60,
            "Markdown file": 48,
        },
        wrap_headers=("Profile", "Executive bios", "Outreach readiness",
                      "Trigger status", "Promotion record"),
    )


def write_monitoring_markdown(
    result,
    state: PipelineState,
    guardrails: Guardrails,
    source_label: str,
) -> Path:
    """The pending promotion proposals, as a readable review-queue note."""
    path = guardrails.review_queue_dir / "promotion_proposals.md"
    guardrails.assert_write_allowed(path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pending = state.pending

    lines = [
        "# Promotion proposals awaiting your decision",
        "",
        "> **MOCK DATA** - every company, person and event below is fictional.",
        "> Verify anything before relying on it or sharing it.",
        "",
        f"- Generated: {now}",
        f"- Data source: {source_label}",
        f"- Signals assessed: {result.assessed_count}",
        f"- Confirmed triggers: {result.confirmed_count}",
        f"- **Proposals awaiting approval: {len(pending)}**",
        "",
        "The engine has **not** promoted anything. Each proposal below needs a human",
        "decision. Record it on the `05_Promotion_Proposals` sheet of",
        "`pipeline.xlsx` in the `Freigabe` column.",
        "",
    ]
    if not pending:
        lines.extend(["_No proposals awaiting a decision._", ""])
    for record in pending:
        lines.extend([
            f"## {record.company_name} ({record.company_id})",
            "",
            f"- **Proposed on:** {record.proposed_on} by {record.proposed_by}",
            f"- **Trigger category:** {record.trigger_category}",
            f"- **Trigger:** {record.trigger_headline}",
            f"- **Supporting signals:** {', '.join(record.trigger_signal_ids)}",
            "",
            f"{record.note}",
            "",
            "**Decision required:** approve, decline, or defer. Until then the company",
            "stays on the watchlist.",
            "",
        ])

    lines.extend(["## Companies with signals but no proposal", ""])
    for status in result.statuses:
        if status.proposal is not None:
            continue
        lines.append(
            f"- **{status.company_name}** ({status.company_id}, "
            f"{status.classification}): {status.outcome_detail}"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
