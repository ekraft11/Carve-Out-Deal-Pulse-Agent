"""Stubs for the real data providers.

None of these are implemented, and none of them have credentials. They exist
to mark the seam: when a real connector is built, it subclasses `DataSource`
exactly like `MockDataSource` does and maps the provider's fields into the
classes in models.py. Nothing in the screening or monitoring code changes.

Each stub raises a clear message rather than silently returning nothing, so
misconfiguring the active data source fails loudly instead of producing an
empty screen that looks like a valid result.
"""

from __future__ import annotations

from datetime import date

from ..models import Company, Executive, Ownership, Signal
from .base import DataSource, DataSourceError


class NotConfiguredDataSource(DataSource):
    """Shared behaviour for providers that are not wired up yet."""

    #: what a real implementation would still need
    integration_notes: str = ""

    def _not_configured(self, tool: str) -> DataSourceError:
        return DataSourceError(
            f"The {self.source_label} connector is not implemented and has no "
            f"credentials configured, so '{tool}' cannot be served.\n"
            f"To enable it: implement the five _fetch_* methods on "
            f"{type(self).__name__}, then set data_sources.active to "
            f"'{self.source_name}' in config/guardrails.json.\n"
            f"{self.integration_notes}"
        )

    def _fetch_companies(self, **_kwargs) -> list[Company]:  # type: ignore[override]
        raise self._not_configured("search_companies")

    def _fetch_company(self, company_id: str) -> Company:
        raise self._not_configured("get_company")

    def _fetch_ownership(self, company_id: str) -> Ownership:
        raise self._not_configured("get_ownership")

    def _fetch_executives(self, company_id: str) -> list[Executive]:
        raise self._not_configured("get_executives")

    def _fetch_signals(
        self,
        company_id: str | None = None,
        category: str | None = None,
        since: date | None = None,
    ) -> list[Signal]:
        raise self._not_configured("get_signals")


class GainDataSource(NotConfiguredDataSource):
    """Placeholder for a Gain.pro connector."""

    source_name = "gain"
    source_label = "Gain.pro"
    is_mock = False
    integration_notes = (
        "Expected to cover: company search and financials, ownership and "
        "shareholder history, management teams. Signals would need a separate "
        "news or event feed."
    )


class InvenDataSource(NotConfiguredDataSource):
    """Placeholder for an Inven connector."""

    source_name = "inven"
    source_label = "Inven"
    is_mock = False
    integration_notes = (
        "Expected to cover: private company discovery and sub-sector "
        "classification for universe expansion."
    )


class FactSetDataSource(NotConfiguredDataSource):
    """Placeholder for a FactSet connector."""

    source_name = "factset"
    source_label = "FactSet"
    is_mock = False
    integration_notes = (
        "Expected to cover: listing status verification, people and "
        "biographies, and corporate transaction events. Note that listing "
        "status from FactSet should be treated as authoritative for the "
        "listed-company exclusion."
    )
