"""The mock data source.

Serves fictional company data from data/mock_universe.json. This is the only
data source that works today; it exists so the whole pipeline can be built and
demonstrated before any real provider is connected.

Everything it returns is invented and carries the label "MOCK DATA".
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..audit import AuditLogger
from ..config import DATA_DIR, Guardrails
from ..models import Company, Executive, Ownership, Signal
from .base import DataSource, DataSourceError

DEFAULT_UNIVERSE_PATH = DATA_DIR / "mock_universe.json"


class MockDataSource(DataSource):
    """Read-only access to the fictional universe."""

    source_name = "mock"
    source_label = "MOCK DATA (fictional universe)"
    is_mock = True

    def __init__(
        self,
        guardrails: Guardrails,
        logger: AuditLogger,
        universe_path: Path = DEFAULT_UNIVERSE_PATH,
    ) -> None:
        super().__init__(guardrails, logger)
        self.universe_path = universe_path
        self._companies: dict[str, Company] = {}
        self._ownership: dict[str, Ownership] = {}
        self._executives: dict[str, list[Executive]] = {}
        self._signals: list[Signal] = []
        self.as_of: date | None = None
        self._load()

    # -- loading -------------------------------------------------------------

    def _load(self) -> None:
        if not self.universe_path.exists():
            raise DataSourceError(
                f"Mock universe file not found at {self.universe_path}. "
                f"The prototype cannot run without it."
            )
        try:
            with self.universe_path.open(encoding="utf-8") as handle:
                raw: dict[str, Any] = json.load(handle)
        except json.JSONDecodeError as exc:
            raise DataSourceError(
                f"{self.universe_path.name} is not valid JSON "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ) from exc

        as_of = raw.get("as_of")
        if as_of:
            self.as_of = date.fromisoformat(as_of)

        for record in raw.get("companies", []):
            company = Company.from_dict(record)
            if company.company_id in self._companies:
                raise DataSourceError(
                    f"Duplicate company_id '{company.company_id}' in "
                    f"{self.universe_path.name}."
                )
            self._companies[company.company_id] = company

            ownership_record = dict(record.get("ownership", {}))
            if ownership_record:
                ownership_record.setdefault("company_id", company.company_id)
                self._ownership[company.company_id] = Ownership.from_dict(ownership_record)

            executives = []
            for exec_record in record.get("executives", []):
                exec_record = dict(exec_record)
                exec_record.setdefault("company_id", company.company_id)
                executives.append(Executive.from_dict(exec_record))
            self._executives[company.company_id] = executives

        known_ids = set(self._companies)
        for record in raw.get("signals", []):
            signal = Signal.from_dict(record)
            if signal.company_id not in known_ids:
                raise DataSourceError(
                    f"Signal '{signal.signal_id}' refers to unknown company "
                    f"'{signal.company_id}'."
                )
            self._signals.append(signal)

        if not self._companies:
            raise DataSourceError(f"{self.universe_path.name} contains no companies.")

    # -- the five fetch methods ---------------------------------------------

    def _fetch_companies(
        self,
        country: str | None = None,
        subsector: str | None = None,
        min_ebitda_eur_m: float | None = None,
        listing_status: str | None = None,
        limit: int | None = None,
    ) -> list[Company]:
        results = list(self._companies.values())
        if country:
            results = [c for c in results if c.country == country.upper()]
        if subsector:
            needle = subsector.lower()
            results = [c for c in results if needle in c.subsector.lower()]
        if min_ebitda_eur_m is not None:
            results = [
                c for c in results
                if c.ebitda_eur_m is not None and c.ebitda_eur_m >= min_ebitda_eur_m
            ]
        if listing_status:
            results = [c for c in results if c.listing_status == listing_status]
        results.sort(key=lambda c: c.company_id)
        if limit is not None:
            results = results[:limit]
        return results

    def _fetch_company(self, company_id: str) -> Company:
        try:
            return self._companies[company_id]
        except KeyError:
            raise DataSourceError(
                f"No company with id '{company_id}' in the mock universe."
            ) from None

    def _fetch_ownership(self, company_id: str) -> Ownership:
        if company_id not in self._companies:
            raise DataSourceError(
                f"No company with id '{company_id}' in the mock universe."
            )
        try:
            return self._ownership[company_id]
        except KeyError:
            raise DataSourceError(
                f"No ownership record on file for '{company_id}'."
            ) from None

    def _fetch_executives(self, company_id: str) -> list[Executive]:
        if company_id not in self._companies:
            raise DataSourceError(
                f"No company with id '{company_id}' in the mock universe."
            )
        return list(self._executives.get(company_id, []))

    def _fetch_signals(
        self,
        company_id: str | None = None,
        category: str | None = None,
        since: date | None = None,
    ) -> list[Signal]:
        results = list(self._signals)
        if company_id:
            results = [s for s in results if s.company_id == company_id]
        if category:
            results = [s for s in results if s.category == category]
        if since:
            results = [s for s in results if s.observed_on >= since]
        results.sort(key=lambda s: s.observed_on, reverse=True)
        return results
