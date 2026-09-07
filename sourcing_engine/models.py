"""The data shapes the engine works with.

This file is the *contract* between the data layer and everything else. The
screening and monitoring code only ever sees these objects, never raw JSON
from a particular provider.

That is what makes the mock and the real sources interchangeable: when a Gain
or FactSet connector is written later, its only job is to map that provider's
fields into the classes below. Nothing downstream changes.

All monetary values are EUR millions. The mock data stores EUR directly; a
real connector would convert from local currency at that point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

DATA_LABEL_DEFAULT = "MOCK DATA"


class SchemaError(ValueError):
    """Raised when a data source returns a record missing required fields."""


def _require(record: dict[str, Any], key: str, context: str) -> Any:
    if key not in record or record[key] is None:
        raise SchemaError(f"Record for {context} is missing required field '{key}'.")
    return record[key]


def _parse_date(value: Any, context: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise SchemaError(
            f"Could not read date '{value}' for {context}; expected YYYY-MM-DD."
        ) from exc


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    """Core company record."""

    company_id: str
    name: str
    country: str
    subsector: str
    listing_status: str
    ebitda_eur_m: float | None = None
    revenue_eur_m: float | None = None
    employees: int | None = None
    founded_year: int | None = None
    city: str | None = None
    description: str = ""
    ticker: str | None = None
    financials_as_of: str | None = None
    data_label: str = DATA_LABEL_DEFAULT
    notes: str = ""

    @property
    def is_listed(self) -> bool:
        return self.listing_status != "private"

    @property
    def ebitda_margin_pct(self) -> float | None:
        if not self.revenue_eur_m or self.ebitda_eur_m is None:
            return None
        return round(100.0 * self.ebitda_eur_m / self.revenue_eur_m, 1)

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Company":
        context = record.get("company_id", "<unknown company>")
        return cls(
            company_id=str(_require(record, "company_id", context)),
            name=str(_require(record, "name", context)),
            country=str(_require(record, "country", context)).upper(),
            subsector=str(_require(record, "subsector", context)),
            listing_status=str(_require(record, "listing_status", context)),
            ebitda_eur_m=(None if record.get("ebitda_eur_m") is None
                          else float(record["ebitda_eur_m"])),
            revenue_eur_m=(None if record.get("revenue_eur_m") is None
                           else float(record["revenue_eur_m"])),
            employees=record.get("employees"),
            founded_year=record.get("founded_year"),
            city=record.get("city"),
            description=record.get("description", ""),
            ticker=record.get("ticker"),
            financials_as_of=record.get("financials_as_of"),
            data_label=record.get("data_label", DATA_LABEL_DEFAULT),
            notes=record.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holder:
    """One shareholder."""

    name: str
    holder_type: str  # family | founder | pe_fund | foundation | corporate | state | management | free_float
    stake_pct: float
    since_year: int | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, record: dict[str, Any], context: str) -> "Holder":
        return cls(
            name=str(_require(record, "name", context)),
            holder_type=str(_require(record, "holder_type", context)),
            stake_pct=float(_require(record, "stake_pct", context)),
            since_year=record.get("since_year"),
            notes=record.get("notes", ""),
        )


@dataclass(frozen=True)
class Ownership:
    """Ownership and control structure for one company."""

    company_id: str
    control_type: str
    family_founder_stake_pct: float = 0.0
    pe_stake_pct: float = 0.0
    free_float_pct: float = 0.0
    holders: tuple[Holder, ...] = ()
    governance_notes: str = ""
    ownership_as_of: str | None = None
    data_label: str = DATA_LABEL_DEFAULT
    #: Set by the data source when the share split does not settle who
    #: actually controls the company (investor veto rights, split boards).
    #: A structured field rather than keyword-matching on prose, so a real
    #: connector has one explicit thing to populate.
    control_contested: bool = False
    control_contested_note: str = ""

    @property
    def total_stake_pct(self) -> float:
        return round(sum(h.stake_pct for h in self.holders), 1)

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Ownership":
        context = record.get("company_id", "<unknown company>")
        holders = tuple(
            Holder.from_dict(h, context) for h in record.get("holders", [])
        )
        return cls(
            company_id=str(_require(record, "company_id", context)),
            control_type=str(_require(record, "control_type", context)),
            family_founder_stake_pct=float(record.get("family_founder_stake_pct", 0.0)),
            pe_stake_pct=float(record.get("pe_stake_pct", 0.0)),
            free_float_pct=float(record.get("free_float_pct", 0.0)),
            holders=holders,
            governance_notes=record.get("governance_notes", ""),
            ownership_as_of=record.get("ownership_as_of"),
            data_label=record.get("data_label", DATA_LABEL_DEFAULT),
            control_contested=bool(record.get("control_contested", False)),
            control_contested_note=record.get("control_contested_note", ""),
        )


# ---------------------------------------------------------------------------
# Executives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Executive:
    """One member of the leadership team or board."""

    company_id: str
    name: str
    title: str
    age: int | None = None
    in_role_since: int | None = None
    is_founder: bool = False
    is_family_member: bool = False
    background: tuple[str, ...] = ()
    notes: str = ""
    data_label: str = DATA_LABEL_DEFAULT

    def one_liner(self, company_name: str) -> str:
        """Bio in the house format: 'Name - Title, Company: Background'."""
        background = "; ".join(self.background) if self.background else "background not on file"
        return f"{self.name} - {self.title}, {company_name}: {background}"

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Executive":
        context = record.get("company_id", "<unknown company>")
        return cls(
            company_id=str(_require(record, "company_id", context)),
            name=str(_require(record, "name", context)),
            title=str(_require(record, "title", context)),
            age=record.get("age"),
            in_role_since=record.get("in_role_since"),
            is_founder=bool(record.get("is_founder", False)),
            is_family_member=bool(record.get("is_family_member", False)),
            background=tuple(record.get("background", [])),
            notes=record.get("notes", ""),
            data_label=record.get("data_label", DATA_LABEL_DEFAULT),
        )


# ---------------------------------------------------------------------------
# Signals (the raw material for triggers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """One observed event that may or may not amount to a confirmed trigger.

    A signal is evidence. Whether it counts as a *trigger* is decided by the
    monitoring engine against the thresholds in criteria.json - never here.
    """

    signal_id: str
    company_id: str
    category: str
    headline: str
    observed_on: date
    confidence: float = 0.0
    corroborated: bool = False
    detail: str = ""
    source_type: str = "mock_feed"
    corroborating_sources: tuple[str, ...] = ()
    data_label: str = DATA_LABEL_DEFAULT

    def age_days(self, as_of: date) -> int:
        return (as_of - self.observed_on).days

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Signal":
        context = record.get("signal_id", "<unknown signal>")
        return cls(
            signal_id=str(_require(record, "signal_id", context)),
            company_id=str(_require(record, "company_id", context)),
            category=str(_require(record, "category", context)),
            headline=str(_require(record, "headline", context)),
            observed_on=_parse_date(_require(record, "observed_on", context), context),
            confidence=float(record.get("confidence", 0.0)),
            corroborated=bool(record.get("corroborated", False)),
            detail=record.get("detail", ""),
            source_type=record.get("source_type", "mock_feed"),
            corroborating_sources=tuple(record.get("corroborating_sources", [])),
            data_label=record.get("data_label", DATA_LABEL_DEFAULT),
        )


# ---------------------------------------------------------------------------
# A convenience bundle used by the output generator
# ---------------------------------------------------------------------------


@dataclass
class CompanyDossier:
    """Everything known about one company, gathered from the data source."""

    company: Company
    ownership: Ownership | None = None
    executives: list[Executive] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
