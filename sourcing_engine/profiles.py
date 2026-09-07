"""Output packs for active pipeline companies.

Three things per company:

  profile           a standardised company one-pager
  executive bios    in the house format: "Name - Title, Company: Background"
  outreach note     warm-intro and live-process checks

The outreach note is deliberately full of explicit placeholders. Warm-intro
and live-process checks need the CRM and the live deal list, and this
prototype has neither. Writing "cannot be assessed, here is what it would
need" is the honest output; inventing a plausible-looking answer would be the
dangerous one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import Criteria, Guardrails
from .data_sources.base import DataSource
from .models import CompanyDossier, Executive
from .monitoring import CompanyTriggerStatus
from .screening import ScreeningVerdict
from .state import PromotionRecord

PROFILE_DIRNAME = "profiles"

#: Checks the prototype cannot perform, and what each one would need.
OUTREACH_PLACEHOLDERS = [
    (
        "Warm introduction paths",
        "PLACEHOLDER - not assessed",
        "Requires the firm CRM (relationship history, shared board seats, adviser "
        "and banker contacts, portfolio company overlaps). No CRM connector exists "
        "in this prototype.",
    ),
    (
        "Live process check",
        "PLACEHOLDER - not assessed",
        "Requires the live deal list and the sell-side mandate feed, to establish "
        "whether the company is already in a process and whether the firm has been "
        "approached. Not connected.",
    ),
    (
        "Prior contact history",
        "PLACEHOLDER - not assessed",
        "Requires deal history: previous approaches, NDAs signed, and any reason "
        "the company was passed on before.",
    ),
    (
        "Conflict and restriction check",
        "PLACEHOLDER - not assessed",
        "Requires the compliance restricted list and the portfolio overlap check "
        "before any approach is made.",
    ),
]


@dataclass
class OutputPack:
    """One company's profile, bios and outreach note."""

    company_id: str
    company_name: str
    profile_lines: list[tuple[str, str]] = field(default_factory=list)
    bios: list[str] = field(default_factory=list)
    outreach_lines: list[tuple[str, str, str]] = field(default_factory=list)
    trigger_summary: str = ""
    approval_note: str = ""
    markdown_path: Path | None = None

    @property
    def profile_text(self) -> str:
        return " | ".join(f"{label}: {value}" for label, value in self.profile_lines)

    @property
    def bios_text(self) -> str:
        return "\n".join(self.bios)

    @property
    def outreach_text(self) -> str:
        return "\n".join(f"{label}: {status}" for label, status, _ in self.outreach_lines)


def _fmt_money(value: float | None) -> str:
    return "not on file" if value is None else f"EUR {value:.1f}m"


def build_pack(
    verdict: ScreeningVerdict,
    dossier: CompanyDossier,
    criteria: Criteria,
    trigger_status: CompanyTriggerStatus | None,
    promotion: PromotionRecord | None,
) -> OutputPack:
    """Assemble one company's pack from data already fetched."""
    company = dossier.company
    ownership = dossier.ownership

    holders = (
        "; ".join(
            f"{h.name} {h.stake_pct:.0f}%"
            + (f" (since {h.since_year})" if h.since_year else "")
            for h in ownership.holders
        )
        if ownership and ownership.holders
        else "not on file"
    )
    margin = company.ebitda_margin_pct
    profile_lines = [
        ("Company", company.name),
        ("Company ID", company.company_id),
        ("Headquarters", f"{company.city}, {company.country}" if company.city else company.country),
        ("Geography tier", f"{verdict.region} ({verdict.tier})"),
        ("Subsector", company.subsector),
        ("Business", company.description or "not on file"),
        ("Founded", str(company.founded_year) if company.founded_year else "not on file"),
        ("Employees", f"{company.employees:,}" if company.employees else "not on file"),
        ("Revenue", f"{_fmt_money(company.revenue_eur_m)} ({company.financials_as_of or 'period not stated'})"),
        ("EBITDA", f"{_fmt_money(company.ebitda_eur_m)}"
                   + (f", {margin:.1f}% margin" if margin is not None else "")),
        ("Listing status", company.listing_status.replace("_", " ")),
        ("Control type", ownership.control_type.replace("_", " ") if ownership else "not on file"),
        ("Family/founder stake", f"{ownership.family_founder_stake_pct:.0f}%" if ownership else "not on file"),
        ("PE stake", f"{ownership.pe_stake_pct:.0f}%" if ownership else "not on file"),
        ("Shareholders", holders),
        ("Governance", ownership.governance_notes if ownership and ownership.governance_notes else "not on file"),
        ("Screening verdict", verdict.reason),
        ("Data label", company.data_label),
    ]

    bios = [exec_.one_liner(company.name) for exec_ in dossier.executives]
    if not bios:
        bios = ["No executives on file for this company."]

    return OutputPack(
        company_id=company.company_id,
        company_name=company.name,
        profile_lines=profile_lines,
        bios=bios,
        outreach_lines=list(OUTREACH_PLACEHOLDERS),
        trigger_summary=(
            trigger_status.summary if trigger_status else "no trigger scan on file"
        ),
        approval_note=(
            f"Promoted to the active pipeline on {promotion.decided_on} by "
            f"{promotion.decided_by}, on the {promotion.trigger_category} trigger: "
            f"{promotion.trigger_headline}"
            if promotion
            else "No promotion record on file."
        ),
    )


def build_packs(
    verdicts: Sequence[ScreeningVerdict],
    source: DataSource,
    criteria: Criteria,
    trigger_lookup: dict[str, CompanyTriggerStatus],
    state_lookup: dict[str, PromotionRecord],
) -> list[OutputPack]:
    """Build a pack per company, fetching each dossier through the audited tools."""
    packs = []
    for verdict in verdicts:
        dossier = source.get_dossier(verdict.company_id)
        packs.append(
            build_pack(
                verdict,
                dossier,
                criteria,
                trigger_lookup.get(verdict.company_id),
                state_lookup.get(verdict.company_id),
            )
        )
    return packs


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def write_pack_markdown(pack: OutputPack, guardrails: Guardrails) -> Path:
    """One markdown file per active company, in review_queue/profiles/."""
    directory = guardrails.review_queue_dir / PROFILE_DIRNAME
    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in pack.company_name
    ).strip("_")
    path = directory / f"{pack.company_id}_{safe_name}.md"
    guardrails.assert_write_allowed(path)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {pack.company_name} ({pack.company_id})",
        "",
        "> **MOCK DATA** - this company, its people and every figure below are",
        "> FICTIONAL, invented for a prototype demonstration. Nothing here describes",
        "> any real business or person. Verify everything before relying on it.",
        "",
        f"- Generated: {now}",
        "- Status: **active pipeline**",
        f"- {pack.approval_note}",
        f"- Trigger status: {pack.trigger_summary}",
        "",
        "## Company profile",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for label, value in pack.profile_lines:
        cleaned = str(value).replace("|", "\\|")
        lines.append(f"| {label} | {cleaned} |")

    lines.extend([
        "",
        "## Executive biographies",
        "",
        "_Format: Name - Title, Company: Background_",
        "",
    ])
    for bio in pack.bios:
        lines.append(f"- {bio}")

    lines.extend([
        "",
        "## Outreach readiness",
        "",
        "This section is **not an assessment**. Every check below needs a data",
        "source this prototype does not have. Each one states what would be",
        "required, so the gap is visible rather than glossed over.",
        "",
        "| Check | Status | What it would need |",
        "|---|---|---|",
    ])
    for label, status, requirement in pack.outreach_lines:
        lines.append(f"| {label} | {status} | {requirement} |")

    lines.extend([
        "",
        "**Outreach readiness verdict: NOT READY.** No approach should be made on",
        "the basis of this pack. The structural and trigger work is done; the",
        "relationship, process and compliance checks are outstanding.",
        "",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
