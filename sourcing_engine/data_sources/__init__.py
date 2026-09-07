"""Data sources: one interface, several possible backends.

The rest of the engine calls `open_data_source(...)` and never names a
provider. Which backend it gets is decided by data_sources.active in
config/guardrails.json - "mock" today, "gain" or "factset" once those
connectors exist.
"""

from __future__ import annotations

from ..audit import AuditLogger
from ..config import Guardrails
from .base import TOOL_NAMES, DataSource, DataSourceError
from .mock import MockDataSource
from .stubs import FactSetDataSource, GainDataSource, InvenDataSource

#: machine name (as used in guardrails.json) -> implementation
REGISTRY: dict[str, type[DataSource]] = {
    "mock": MockDataSource,
    "gain": GainDataSource,
    "inven": InvenDataSource,
    "factset": FactSetDataSource,
}


def open_data_source(guardrails: Guardrails, logger: AuditLogger) -> DataSource:
    """Build the configured data source."""
    name = guardrails.active_data_source
    try:
        source_class = REGISTRY[name]
    except KeyError:
        raise DataSourceError(
            f"Unknown data source '{name}'. Available: "
            f"{', '.join(sorted(REGISTRY))}."
        ) from None
    return source_class(guardrails, logger)


__all__ = [
    "DataSource",
    "DataSourceError",
    "FactSetDataSource",
    "GainDataSource",
    "InvenDataSource",
    "MockDataSource",
    "REGISTRY",
    "TOOL_NAMES",
    "open_data_source",
]
