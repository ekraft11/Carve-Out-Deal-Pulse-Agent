#!/usr/bin/env python3
"""Read-only MCP server over the sourcing engine's data layer.

MCP (Model Context Protocol) is the standard way to hand an assistant a fixed
set of tools it may call. This server exposes exactly five read-only tools -
the same five in config/guardrails.json - and nothing else. There is no tool
to write anything, send anything, or fetch anything from the web.

It is a thin wrapper: every tool call goes through the same DataSource used by
`run.py screening` and `run.py monitoring`, so the allowlist check and the
audit log apply here too. Nothing can reach the data except through those
five audited methods.

Three properties worth knowing:

  * The tool list is checked against the config at startup. If the registered
    tools and guardrails.json disagree, the server refuses to start rather
    than quietly serving a tool nobody approved.
  * Each tool is declared read-only in the protocol itself (read_only_hint,
    destructive_hint=False, open_world_hint=False), so a client can see the
    constraint without reading this file.
  * Nothing is printed to stdout. Under the stdio transport stdout carries the
    protocol; a stray print would corrupt the stream. Diagnostics go to stderr.

Run it directly for a manual check:

    python mcp_server.py

Or attach it to Claude Code via the .mcp.json in this repository.

All data served is FICTIONAL and labelled "MOCK DATA".
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any

from sourcing_engine import DATA_LABEL, __version__
from sourcing_engine.audit import AuditLogger
from sourcing_engine.config import ConfigError, GuardrailViolation, load_settings
from sourcing_engine.data_sources import DataSourceError, open_data_source

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
    raise SystemExit(
        "The 'mcp' package is required for the MCP server (but not for "
        "`run.py screening` or `run.py monitoring`).\n"
        "Install it with:  pip install -r requirements-mcp.txt\n"
        f"Underlying import error: {exc}"
    ) from exc


def log(message: str) -> None:
    """Diagnostics to stderr - stdout belongs to the protocol."""
    print(f"[sourcing-engine-mcp] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Startup: configuration, guardrails, data source
# ---------------------------------------------------------------------------

try:
    SETTINGS = load_settings()
except (ConfigError, GuardrailViolation) as exc:
    raise SystemExit(f"Refusing to start: {exc}") from exc

GUARDRAILS = SETTINGS.guardrails
AUDIT = AuditLogger.for_run(GUARDRAILS, command="mcp_server")

try:
    SOURCE = open_data_source(GUARDRAILS, AUDIT)
except DataSourceError as exc:
    raise SystemExit(f"Refusing to start: {exc}") from exc

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    # This server never reaches outside the local data layer. Declaring it
    # here makes the "no open web access" guardrail visible to the client.
    open_world_hint=False,
)

server = MCPServer(
    name="sourcing-engine-mock",
    title="Private Company Sourcing Engine (mock data)",
    version=__version__,
    instructions=(
        "Read-only access to a FICTIONAL universe of private healthcare companies "
        "for a deal-sourcing prototype. Every company, person, financial figure and "
        "event served by this server is invented and labelled MOCK DATA; none of it "
        "describes any real business or person, and it must not be presented as "
        "information about one.\n\n"
        "Five tools are available: search_companies, get_company, get_ownership, "
        "get_executives and get_signals. There are no tools to write, send or "
        "fetch anything else - this server is read-only by design, and every call "
        "you make is written to a local audit log.\n\n"
        "Screening criteria for context: family- or founder-owned (a private equity "
        "minority is acceptable, a PE majority is not), at least EUR 20m EBITDA, "
        "DACH and Nordics primary with UK and Benelux secondary, and any listed "
        "company is out of scope regardless of how concentrated its ownership is."
    ),
)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Make a dataclass or date JSON-safe."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _envelope(payload: Any, count: int | None = None) -> dict[str, Any]:
    """Every response carries the mock-data warning with it."""
    body: dict[str, Any] = {
        "data_label": DATA_LABEL,
        "warning": (
            "MOCK DATA - fictional companies and people, invented for a prototype. "
            "Not information about any real business or person."
        ),
        "source": SOURCE.source_name,
    }
    if count is not None:
        body["count"] = count
    body["data"] = _plain(payload)
    return body


# ---------------------------------------------------------------------------
# The five tools. Names must match guardrails.json exactly.
# ---------------------------------------------------------------------------


@server.tool(
    name="search_companies",
    description=(
        "List companies in the fictional universe, optionally filtered by country "
        "(ISO code, e.g. DE), subsector substring, minimum EBITDA in EUR millions, "
        "or listing status ('private', 'listed_main_market', "
        "'listed_secondary_market'). Returns MOCK DATA."
    ),
    annotations=READ_ONLY,
)
def search_companies(
    country: str | None = None,
    subsector: str | None = None,
    min_ebitda_eur_m: float | None = None,
    listing_status: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    companies = SOURCE.search_companies(
        country=country,
        subsector=subsector,
        min_ebitda_eur_m=min_ebitda_eur_m,
        listing_status=listing_status,
        limit=limit,
    )
    return _envelope(companies, count=len(companies))


@server.tool(
    name="get_company",
    description=(
        "Fetch one fictional company record by its id (e.g. MOCK-001): country, "
        "subsector, description, headcount, revenue and EBITDA in EUR millions, "
        "and listing status. Returns MOCK DATA."
    ),
    annotations=READ_ONLY,
)
def get_company(company_id: str) -> dict[str, Any]:
    return _envelope(SOURCE.get_company(company_id))


@server.tool(
    name="get_ownership",
    description=(
        "Fetch the ownership and control structure of one fictional company: "
        "control type, family/founder and private equity percentages, the "
        "shareholder register, governance notes, and whether the share split "
        "leaves effective control unclear. Returns MOCK DATA."
    ),
    annotations=READ_ONLY,
)
def get_ownership(company_id: str) -> dict[str, Any]:
    return _envelope(SOURCE.get_ownership(company_id))


@server.tool(
    name="get_executives",
    description=(
        "Fetch the leadership team and board of one fictional company: names, "
        "titles, ages, tenure, whether they are a founder or family member, and "
        "career background. Every person is invented. Returns MOCK DATA."
    ),
    annotations=READ_ONLY,
)
def get_executives(company_id: str) -> dict[str, Any]:
    executives = SOURCE.get_executives(company_id)
    return _envelope(executives, count=len(executives))


@server.tool(
    name="get_signals",
    description=(
        "Fetch observed behavioural signals, optionally for one company, one "
        "category ('succession', 'c_suite_change', "
        "'ownership_governance_change', 'capital_event'), or observed on or after "
        "a date (YYYY-MM-DD). A signal is evidence, not a confirmed trigger: "
        "whether it counts is decided by the engine's thresholds. Returns MOCK DATA."
    ),
    annotations=READ_ONLY,
)
def get_signals(
    company_id: str | None = None,
    category: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    since_date = date.fromisoformat(since) if since else None
    signals = SOURCE.get_signals(
        company_id=company_id, category=category, since=since_date
    )
    return _envelope(signals, count=len(signals))


# ---------------------------------------------------------------------------
# Startup guardrail: the served tools must be exactly the approved ones
# ---------------------------------------------------------------------------


def verify_tool_surface() -> list[str]:
    """Refuse to start if the tool list and guardrails.json disagree.

    This is the guardrail that matters most for a server: not "we intended to
    expose five read-only tools" but "the process will not run if it exposes
    anything else".
    """
    tools = asyncio.run(server.list_tools())
    served = {tool.name for tool in tools}
    approved = set(GUARDRAILS.allowlisted_tools)

    extra = served - approved
    missing = approved - served
    if extra:
        raise SystemExit(
            f"Refusing to start: this server would expose tools that are not on "
            f"the allowlist in config/guardrails.json: {', '.join(sorted(extra))}."
        )
    if missing:
        raise SystemExit(
            f"Refusing to start: config/guardrails.json approves tools this "
            f"server does not implement: {', '.join(sorted(missing))}."
        )
    for tool in tools:
        annotations = tool.annotations
        if annotations is None or not annotations.read_only_hint:
            raise SystemExit(
                f"Refusing to start: tool '{tool.name}' is not declared read-only."
            )
    return sorted(served)


def main() -> None:
    served = verify_tool_surface()
    AUDIT.log_event(
        "mcp_server_started",
        {
            "tools": served,
            "data_source": SOURCE.source_name,
            "read_only": GUARDRAILS.read_only,
            "web_access_enabled": GUARDRAILS.web_access_enabled,
        },
    )
    log(f"serving {len(served)} read-only tools: {', '.join(served)}")
    log(f"data source: {SOURCE.source_label}")
    log(f"audit log: {GUARDRAILS.audit_log_path} (run {AUDIT.run_id})")
    log("all data served is FICTIONAL and labelled MOCK DATA")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
