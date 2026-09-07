"""The data source interface.

Every data source - the mock one today, Gain/Inven/FactSet later - inherits
from `DataSource` and implements five read-only fetch methods.

The important detail: the *public* methods (`search_companies`, `get_company`,
and so on) are written once, here in the base class. They check the tool
allowlist and write the audit log entry, then hand off to the subclass's
private `_fetch_*` method. A subclass cannot skip either step, because it
never implements the public method at all.

So "log every tool call" is a property of the architecture rather than a rule
somebody has to remember when writing the next connector.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Callable, TypeVar

from ..audit import AuditLogger
from ..config import Guardrails
from ..models import Company, CompanyDossier, Executive, Ownership, Signal

T = TypeVar("T")

#: The five read-only tools any data source must offer. These names must match
#: guardrails.json -> allowlisted_tools.
TOOL_NAMES = (
    "search_companies",
    "get_company",
    "get_ownership",
    "get_executives",
    "get_signals",
)


class DataSourceError(RuntimeError):
    """Raised when a data source cannot answer (missing record, no credentials)."""


class DataSource(ABC):
    """Read-only access to company data, audited on every call."""

    #: short machine name, e.g. "mock" - must match guardrails.json
    source_name: str = "abstract"
    #: how this source is labelled in generated output
    source_label: str = "UNLABELLED SOURCE"
    #: whether the data is fictional; drives the MOCK DATA banners
    is_mock: bool = False

    def __init__(self, guardrails: Guardrails, logger: AuditLogger) -> None:
        self.guardrails = guardrails
        self.logger = logger

    # -- the audited call wrapper -------------------------------------------

    def _invoke(
        self,
        tool: str,
        parameters: dict[str, Any],
        fetch: Callable[[], T],
        summarise: Callable[[T], str],
    ) -> T:
        """Gate, execute, and log exactly one data access."""
        parameters = {k: v for k, v in parameters.items() if v is not None}
        try:
            self.guardrails.assert_tool_allowed(tool)
        except Exception as exc:
            self.logger.log_blocked(tool, reason=str(exc), parameters=parameters)
            raise
        try:
            result = fetch()
        except Exception as exc:
            self.logger.log_tool_call(
                tool,
                parameters=parameters,
                result_summary=f"error: {type(exc).__name__}: {exc}",
                data_source=self.source_name,
            )
            raise
        self.logger.log_tool_call(
            tool,
            parameters=parameters,
            result_summary=summarise(result),
            data_source=self.source_name,
        )
        return result

    # -- public interface (do not override) ---------------------------------

    def search_companies(
        self,
        country: str | None = None,
        subsector: str | None = None,
        min_ebitda_eur_m: float | None = None,
        listing_status: str | None = None,
        limit: int | None = None,
    ) -> list[Company]:
        """List companies in the universe, optionally filtered."""
        params = {
            "country": country,
            "subsector": subsector,
            "min_ebitda_eur_m": min_ebitda_eur_m,
            "listing_status": listing_status,
            "limit": limit,
        }
        return self._invoke(
            "search_companies",
            params,
            lambda: self._fetch_companies(
                country=country,
                subsector=subsector,
                min_ebitda_eur_m=min_ebitda_eur_m,
                listing_status=listing_status,
                limit=limit,
            ),
            lambda result: f"{len(result)} companies returned",
        )

    def get_company(self, company_id: str) -> Company:
        """Fetch one company record."""
        return self._invoke(
            "get_company",
            {"company_id": company_id},
            lambda: self._fetch_company(company_id),
            lambda result: f"company '{result.name}' ({result.country})",
        )

    def get_ownership(self, company_id: str) -> Ownership:
        """Fetch the ownership and control structure for one company."""
        return self._invoke(
            "get_ownership",
            {"company_id": company_id},
            lambda: self._fetch_ownership(company_id),
            lambda result: (
                f"control_type={result.control_type}, "
                f"family/founder={result.family_founder_stake_pct}%, "
                f"pe={result.pe_stake_pct}%, {len(result.holders)} holders"
            ),
        )

    def get_executives(self, company_id: str) -> list[Executive]:
        """Fetch the leadership team and board members for one company."""
        return self._invoke(
            "get_executives",
            {"company_id": company_id},
            lambda: self._fetch_executives(company_id),
            lambda result: f"{len(result)} executives returned",
        )

    def get_signals(
        self,
        company_id: str | None = None,
        category: str | None = None,
        since: date | None = None,
    ) -> list[Signal]:
        """Fetch observed signals, optionally filtered by company or category."""
        params = {
            "company_id": company_id,
            "category": category,
            "since": since.isoformat() if since else None,
        }
        return self._invoke(
            "get_signals",
            params,
            lambda: self._fetch_signals(
                company_id=company_id, category=category, since=since
            ),
            lambda result: f"{len(result)} signals returned",
        )

    # -- convenience built on the public interface --------------------------

    def get_dossier(self, company_id: str) -> CompanyDossier:
        """Gather everything about one company (four audited tool calls)."""
        return CompanyDossier(
            company=self.get_company(company_id),
            ownership=self.get_ownership(company_id),
            executives=self.get_executives(company_id),
            signals=self.get_signals(company_id=company_id),
        )

    # -- what a subclass must implement ------------------------------------

    @abstractmethod
    def _fetch_companies(
        self,
        country: str | None = None,
        subsector: str | None = None,
        min_ebitda_eur_m: float | None = None,
        listing_status: str | None = None,
        limit: int | None = None,
    ) -> list[Company]:
        ...

    @abstractmethod
    def _fetch_company(self, company_id: str) -> Company:
        ...

    @abstractmethod
    def _fetch_ownership(self, company_id: str) -> Ownership:
        ...

    @abstractmethod
    def _fetch_executives(self, company_id: str) -> list[Executive]:
        ...

    @abstractmethod
    def _fetch_signals(
        self,
        company_id: str | None = None,
        category: str | None = None,
        since: date | None = None,
    ) -> list[Signal]:
        ...
