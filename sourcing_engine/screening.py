"""The screening engine.

Applies the criteria in config/criteria.json to every company in the universe
and produces a verdict with a written reason.

Three rules shape the output, and all three are deliberate:

1. Hard gates first. A listed company is excluded before ownership is even
   considered, because no degree of family control makes a listed company
   eligible.
2. Structural fit alone never reaches the active pipeline. The best a company
   can achieve here is `watchlist`. Only a confirmed behavioural trigger plus
   human approval moves it further, and that happens in monitoring.py.
3. Anything near a threshold, or with an ownership form the criteria do not
   describe, is flagged for human review rather than sorted quietly. The
   engine says "I am not sure" instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import Criteria
from .data_sources.base import DataSource
from .models import Company, Ownership

CLASS_ACTIVE = "active_pipeline"
CLASS_WATCHLIST = "watchlist"
CLASS_EXCLUDED = "excluded"


@dataclass
class Check:
    """One criterion applied to one company."""

    name: str
    outcome: str  # pass | fail | review
    detail: str

    @property
    def failed(self) -> bool:
        return self.outcome == "fail"

    @property
    def needs_review(self) -> bool:
        return self.outcome == "review"


@dataclass
class ScreeningVerdict:
    """The result for one company, with the reasoning kept alongside it."""

    company_id: str
    name: str
    country: str
    region: str | None
    tier: str | None
    subsector: str
    ebitda_eur_m: float | None
    revenue_eur_m: float | None
    listing_status: str
    control_type: str
    family_founder_stake_pct: float
    pe_stake_pct: float
    classification: str
    structural_fit: bool
    needs_human_review: bool
    reason: str
    review_flags: list[str] = field(default_factory=list)
    exclusion_reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    screened_on: str = ""
    data_label: str = "MOCK DATA"

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "name": self.name,
            "classification": self.classification,
            "structural_fit": self.structural_fit,
            "needs_human_review": self.needs_human_review,
            "reason": self.reason,
            "review_flags": list(self.review_flags),
            "exclusion_reasons": list(self.exclusion_reasons),
            "screened_on": self.screened_on,
        }


class Screener:
    """Applies the criteria. Holds no state beyond the configuration."""

    def __init__(self, criteria: Criteria, source: DataSource, as_of: date) -> None:
        self.criteria = criteria
        self.source = source
        self.as_of = as_of

    # -- entry point ---------------------------------------------------------

    def screen_all(self) -> list[ScreeningVerdict]:
        verdicts = []
        for company in self.source.search_companies():
            ownership = self.source.get_ownership(company.company_id)
            verdicts.append(self.screen(company, ownership))
        return verdicts

    def screen(self, company: Company, ownership: Ownership) -> ScreeningVerdict:
        checks: list[Check] = []
        checks.append(self._check_listing(company, ownership))
        checks.append(self._check_geography(company))
        checks.extend(self._check_ownership(ownership))
        checks.extend(self._check_financials(company))

        exclusions = [c.detail for c in checks if c.failed]
        review_flags = [c.detail for c in checks if c.needs_review]
        structural_fit = not exclusions

        classification = CLASS_EXCLUDED if exclusions else CLASS_WATCHLIST

        # A company excluded by a hard gate does not go on the reviewer's desk
        # for a near-threshold EBITDA: the exclusion settles it. The written
        # reason still records that the other criteria were not fully assessed.
        needs_review = bool(review_flags) and not exclusions

        return ScreeningVerdict(
            company_id=company.company_id,
            name=company.name,
            country=company.country,
            region=self.criteria.region_for_country(company.country),
            tier=self.criteria.tier_for_country(company.country),
            subsector=company.subsector,
            ebitda_eur_m=company.ebitda_eur_m,
            revenue_eur_m=company.revenue_eur_m,
            listing_status=company.listing_status,
            control_type=ownership.control_type,
            family_founder_stake_pct=ownership.family_founder_stake_pct,
            pe_stake_pct=ownership.pe_stake_pct,
            classification=classification,
            structural_fit=structural_fit,
            needs_human_review=needs_review,
            reason=self._write_reason(company, ownership, exclusions, review_flags, structural_fit),
            review_flags=review_flags,
            exclusion_reasons=exclusions,
            checks=checks,
            screened_on=self.as_of.isoformat(),
            data_label=company.data_label,
        )

    # -- the individual gates ------------------------------------------------

    def _check_listing(self, company: Company, ownership: Ownership) -> Check:
        """Hard gate: any listed company is out, however concentrated its ownership."""
        listing = self.criteria.listing
        excluded_statuses = listing["excluded_listing_statuses"]
        accepted_statuses = listing["accepted_listing_statuses"]

        if listing.get("exclude_if_listed", True) and company.listing_status in excluded_statuses:
            concentration = ""
            if ownership.family_founder_stake_pct >= 25.0:
                concentration = (
                    f" Family/founder concentration of "
                    f"{ownership.family_founder_stake_pct:.0f}% does not change this."
                )
            return Check(
                "Private company",
                "fail",
                f"Publicly listed ({company.listing_status.replace('_', ' ')}"
                f"{', ticker ' + company.ticker if company.ticker else ''}); the criteria "
                f"exclude any listed company.{concentration}",
            )
        if company.listing_status not in accepted_statuses:
            return Check(
                "Private company",
                "review",
                f"Listing status '{company.listing_status}' is neither on the accepted "
                f"nor the excluded list in the criteria; a human must confirm whether "
                f"this company is private.",
            )
        return Check("Private company", "pass", "Privately held.")

    def _check_geography(self, company: Company) -> Check:
        tier = self.criteria.tier_for_country(company.country)
        region = self.criteria.region_for_country(company.country)
        if tier is None:
            return Check(
                "Geography",
                "fail",
                f"Domiciled in {company.country}, outside the target geographies "
                f"(DACH and Nordics primary, UK and Benelux secondary).",
            )
        return Check(
            "Geography",
            "pass",
            f"{company.country} sits in {region}, a {tier} target geography.",
        )

    def _check_ownership(self, ownership: Ownership) -> list[Check]:
        rules = self.criteria.ownership
        checks: list[Check] = []
        control_type = ownership.control_type
        readable = control_type.replace("_", " ")

        # -- control type -----------------------------------------------------
        if control_type in rules["excluded_control_types"]:
            if control_type == "pe_majority":
                detail = (
                    f"Private equity holds {ownership.pe_stake_pct:.0f}%, a controlling "
                    f"majority. PE minority stakes are acceptable, PE majority is not."
                )
            elif control_type == "corporate_subsidiary":
                holder = next(
                    (h.name for h in ownership.holders if h.holder_type == "corporate"),
                    "a corporate parent",
                )
                detail = f"Subsidiary of {holder}, not family or founder owned."
            else:
                detail = f"Control type '{readable}' is on the excluded list."
            checks.append(Check("Ownership - control type", "fail", detail))
        elif control_type in rules["eligible_control_types"]:
            checks.append(
                Check("Ownership - control type", "pass", f"Control type: {readable}.")
            )
        else:
            # An ownership form the criteria simply do not describe. Do not guess.
            checks.append(
                Check(
                    "Ownership - control type",
                    "review",
                    f"Control type '{readable}' appears on neither the eligible nor the "
                    f"excluded list, so the criteria do not settle it. "
                    f"{ownership.governance_notes} A human must decide whether this "
                    f"ownership form is in scope.",
                )
            )

        # -- PE stake ---------------------------------------------------------
        max_pe = self.criteria.max_pe_stake
        if ownership.pe_stake_pct > max_pe:
            if control_type != "pe_majority":
                checks.append(
                    Check(
                        "Ownership - PE stake",
                        "fail",
                        f"Private equity holds {ownership.pe_stake_pct:.0f}%, above the "
                        f"{max_pe:.1f}% ceiling for an acceptable minority stake.",
                    )
                )
        elif ownership.pe_stake_pct > 0:
            low, high = rules["borderline_pe_stake_band_pct"]
            if float(low) <= ownership.pe_stake_pct <= float(high):
                checks.append(
                    Check(
                        "Ownership - PE stake",
                        "review",
                        f"Private equity holds {ownership.pe_stake_pct:.0f}%, a minority "
                        f"but within {max_pe - ownership.pe_stake_pct:.1f} points of the "
                        f"{max_pe:.1f}% ceiling.",
                    )
                )
            else:
                checks.append(
                    Check(
                        "Ownership - PE stake",
                        "pass",
                        f"Private equity holds {ownership.pe_stake_pct:.0f}%, a clear "
                        f"minority.",
                    )
                )

        # -- family/founder stake --------------------------------------------
        # Only meaningful once the control type is recognised. If the control
        # type is already unclassifiable, a second flag saying the register
        # disagrees with it adds noise, not information.
        min_family = self.criteria.min_family_stake
        if control_type in rules["eligible_control_types"]:
            if ownership.family_founder_stake_pct < min_family:
                checks.append(
                    Check(
                        "Ownership - family/founder stake",
                        "review",
                        f"Family or founder holds {ownership.family_founder_stake_pct:.0f}%, "
                        f"below the {min_family:.0f}% threshold, yet the control type is not "
                        f"on the excluded list. The register and the control type disagree; "
                        f"a human should resolve which is right.",
                    )
                )
            else:
                low, high = rules["borderline_family_founder_stake_band_pct"]
                if float(low) <= ownership.family_founder_stake_pct <= float(high):
                    checks.append(
                        Check(
                            "Ownership - family/founder stake",
                            "review",
                            f"Family or founder holds "
                            f"{ownership.family_founder_stake_pct:.0f}%, a bare majority "
                            f"close to the {min_family:.0f}% threshold.",
                        )
                    )
                else:
                    checks.append(
                        Check(
                            "Ownership - family/founder stake",
                            "pass",
                            f"Family or founder holds "
                            f"{ownership.family_founder_stake_pct:.0f}%.",
                        )
                    )

        # -- contested control ------------------------------------------------
        if ownership.control_contested:
            checks.append(
                Check(
                    "Ownership - effective control",
                    "review",
                    ownership.control_contested_note
                    or "The share split does not settle who controls the company.",
                )
            )
        return checks

    def _check_financials(self, company: Company) -> list[Check]:
        minimum = self.criteria.min_ebitda
        floor = self.criteria.ebitda_hard_floor
        low, high = self.criteria.ebitda_borderline_band
        ebitda = company.ebitda_eur_m

        if ebitda is None:
            return [
                Check(
                    "EBITDA",
                    "review",
                    f"No EBITDA on file, so the EUR {minimum:.0f}m minimum cannot be "
                    f"tested. The company is neither passed nor excluded on size.",
                )
            ]
        if ebitda < floor:
            return [
                Check(
                    "EBITDA",
                    "fail",
                    f"EBITDA of EUR {ebitda:.1f}m is below the EUR {floor:.0f}m hard floor "
                    f"(minimum EUR {minimum:.0f}m).",
                )
            ]
        if ebitda < minimum:
            return [
                Check(
                    "EBITDA",
                    "review",
                    f"EBITDA of EUR {ebitda:.1f}m is below the EUR {minimum:.0f}m minimum "
                    f"but above the EUR {floor:.0f}m tolerance floor, so it is flagged "
                    f"rather than excluded.",
                )
            ]
        if ebitda <= high:
            return [
                Check(
                    "EBITDA",
                    "review",
                    f"EBITDA of EUR {ebitda:.1f}m clears the EUR {minimum:.0f}m minimum but "
                    f"sits inside the EUR {low:.0f}m to EUR {high:.0f}m watch band, close "
                    f"enough to the line to be worth a look.",
                )
            ]
        return [
            Check(
                "EBITDA",
                "pass",
                f"EBITDA of EUR {ebitda:.1f}m clears the EUR {minimum:.0f}m minimum.",
            )
        ]

    # -- the written reason --------------------------------------------------

    def _write_reason(
        self,
        company: Company,
        ownership: Ownership,
        exclusions: list[str],
        review_flags: list[str],
        structural_fit: bool,
    ) -> str:
        """One paragraph a human can read without opening the rule set."""
        if exclusions:
            head = f"EXCLUDED. {exclusions[0]}"
            if len(exclusions) > 1:
                head += " Also: " + " ".join(exclusions[1:])
            if review_flags:
                head += (
                    " (Other criteria were not fully assessed once the company was "
                    "excluded on the above.)"
                )
            return head

        tier = self.criteria.tier_for_country(company.country)
        region = self.criteria.region_for_country(company.country)
        ebitda = (
            "EBITDA not on file"
            if company.ebitda_eur_m is None
            else f"EUR {company.ebitda_eur_m:.1f}m EBITDA"
        )
        parts = [
            "STRUCTURAL FIT.",
            f"{ownership.control_type.replace('_', ' ').capitalize()}"
            f" ({ownership.family_founder_stake_pct:.0f}% family/founder"
            + (f", {ownership.pe_stake_pct:.0f}% PE minority" if ownership.pe_stake_pct else "")
            + f"), {ebitda}, private, {company.country} in {region}"
            f" ({tier} geography).",
            "Placed on the WATCHLIST because no behavioural trigger has been confirmed;"
            " structural fit on its own does not enter the active pipeline.",
        ]
        if review_flags:
            parts.append(
                "FLAGGED FOR HUMAN REVIEW: " + " ".join(review_flags)
            )
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Aggregate view used by the workbook writer and the console summary
# ---------------------------------------------------------------------------


@dataclass
class ScreeningResult:
    """The whole run."""

    verdicts: list[ScreeningVerdict]
    as_of: date
    run_id: str

    def by_classification(self, classification: str) -> list[ScreeningVerdict]:
        return [v for v in self.verdicts if v.classification == classification]

    @property
    def flagged(self) -> list[ScreeningVerdict]:
        return [v for v in self.verdicts if v.needs_human_review]

    @property
    def counts(self) -> dict[str, int]:
        counts = {CLASS_ACTIVE: 0, CLASS_WATCHLIST: 0, CLASS_EXCLUDED: 0}
        for verdict in self.verdicts:
            counts[verdict.classification] = counts.get(verdict.classification, 0) + 1
        return counts

    def apply_approved_promotions(self, approved_ids: set[str]) -> None:
        """Move human-approved companies into the active pipeline.

        Screening itself can only produce watchlist or excluded. A company
        reaches the active pipeline only because a human approved a promotion
        in an earlier monitoring run - and only if it still passes the screen.
        """
        for verdict in self.verdicts:
            if verdict.company_id in approved_ids and verdict.classification == CLASS_WATCHLIST:
                verdict.classification = CLASS_ACTIVE
                verdict.reason = (
                    "ACTIVE PIPELINE (human-approved promotion). " + verdict.reason
                )
