#!/usr/bin/env python3
"""Test the MCP server without needing an assistant.

Starts mcp_server.py as a subprocess, speaks the protocol to it, and checks
the things that matter:

  * exactly the five approved tools are offered, and nothing else
  * every tool is declared read-only in the protocol
  * a tool call returns data, carrying its MOCK DATA label
  * a tool that is not on the allowlist does not exist and cannot be called
  * every call landed in the audit log

Run it with:

    python scripts/mcp_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from sourcing_engine.audit import read_entries  # noqa: E402
from sourcing_engine.config import load_settings  # noqa: E402

EXPECTED_TOOLS = {
    "search_companies",
    "get_company",
    "get_ownership",
    "get_executives",
    "get_signals",
}

PASS, FAIL = "  OK  ", " FAIL "
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"  [{PASS if ok else FAIL}] {label}")
    if detail:
        print(f"           {detail}")
    return ok


def _payload(result) -> dict:
    """Pull the JSON body out of a tool result."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return {}


async def main() -> int:
    settings = load_settings()
    print("=" * 78)
    print("MCP SERVER SMOKE TEST")
    print("=" * 78)

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "mcp_server.py")],
        cwd=str(REPO_ROOT),
    )

    # The server's stderr is captured rather than shown live: one check below
    # deliberately calls a tool with a bad id, and the framework logs that as a
    # traceback which would otherwise bury the results.
    print("\nCONNECTING")
    errlog = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            info = await session.initialize()
            server_name = getattr(info.server_info, "name", "?")
            check(bool(server_name), f"server started and initialised ({server_name})")

            # -- the tool surface ---------------------------------------
            print("\nTOOL SURFACE")
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            check(
                names == EXPECTED_TOOLS,
                f"exactly the {len(EXPECTED_TOOLS)} approved tools are offered",
                f"offered: {', '.join(sorted(names))}",
            )
            check(
                names == set(settings.guardrails.allowlisted_tools),
                "the offered tools match config/guardrails.json exactly",
            )
            read_only = [
                tool.name
                for tool in listed.tools
                if tool.annotations and tool.annotations.read_only_hint
            ]
            check(
                len(read_only) == len(listed.tools),
                "every tool is declared read-only in the protocol",
            )
            closed_world = [
                tool.name
                for tool in listed.tools
                if tool.annotations and tool.annotations.open_world_hint is False
            ]
            check(
                len(closed_world) == len(listed.tools),
                "every tool declares it does not reach the open world (no web access)",
            )
            write_ish = [
                name
                for name in names
                if any(
                    word in name
                    for word in ("write", "send", "create", "update", "delete", "post",
                                 "fetch", "http", "web", "search_web", "email")
                )
            ]
            check(not write_ish, "no tool that writes, sends or fetches anything exists")

            # -- calling them -------------------------------------------
            print("\nTOOL CALLS")
            result = await session.call_tool("search_companies", {"country": "DE"})
            body = _payload(result)
            german = body.get("data", [])
            check(
                len(german) == 5,
                f"search_companies(country=DE) returned {len(german)} companies",
                ", ".join(c["company_id"] for c in german),
            )
            check(
                body.get("data_label") == "MOCK DATA",
                "the response carries its MOCK DATA label",
                str(body.get("warning", ""))[:96],
            )

            result = await session.call_tool("get_ownership", {"company_id": "MOCK-008"})
            ownership = _payload(result).get("data", {})
            check(
                ownership.get("control_type") == "listed"
                and ownership.get("family_founder_stake_pct") == 58.0,
                "get_ownership(MOCK-008) shows the listed company with 58% family control",
            )

            result = await session.call_tool(
                "get_signals", {"category": "succession"}
            )
            signals = _payload(result).get("data", [])
            check(
                len(signals) == 3,
                f"get_signals(category=succession) returned {len(signals)} signals",
            )

            result = await session.call_tool("get_executives", {"company_id": "MOCK-001"})
            executives = _payload(result).get("data", [])
            check(
                len(executives) == 3,
                f"get_executives(MOCK-001) returned {len(executives)} people",
            )

            # -- what must not be possible ------------------------------
            print("\nWHAT THE SERVER REFUSES")
            try:
                refused = await session.call_tool("send_email", {"to": "x@example.com"})
                is_error = bool(getattr(refused, "is_error", False))
                check(is_error, "calling a non-existent 'send_email' tool is refused")
            except Exception as exc:
                check(True, "calling a non-existent 'send_email' tool is refused",
                      f"{type(exc).__name__}")

            try:
                bad = await session.call_tool("get_company", {"company_id": "NOT-REAL"})
                is_error = bool(getattr(bad, "is_error", False))
                check(is_error, "an unknown company id returns an error, not empty data")
            except Exception as exc:
                check(True, "an unknown company id returns an error, not empty data",
                      f"{type(exc).__name__}")

    errlog.flush()
    errlog.seek(0)
    server_log = errlog.read()
    print("\nSERVER STARTUP (from its stderr)")
    for line in server_log.splitlines():
        if line.startswith("[sourcing-engine-mcp]"):
            print(f"  {line}")
    if "Traceback" in server_log:
        print(f"  (the deliberate bad-id call logged a traceback; full server log: "
              f"{errlog.name})")

    # -- the audit trail --------------------------------------------------
    print("\nAUDIT TRAIL")
    entries = read_entries(settings.guardrails.audit_log_path)
    server_runs = sorted(
        {
            e["run_id"]
            for e in entries
            if e.get("event") == "mcp_server_started"
            or (e.get("kind") == "event" and e.get("detail", {}).get("command") == "mcp_server")
        }
    )
    check(bool(server_runs), f"the server session was logged ({len(server_runs)} run(s))")
    if server_runs:
        latest = server_runs[-1]
        calls = [
            e for e in entries
            if e.get("run_id") == latest and e.get("kind") == "tool_call"
        ]
        per_tool: dict[str, int] = {}
        for entry in calls:
            per_tool[entry["tool"]] = per_tool.get(entry["tool"], 0) + 1
        check(
            len(calls) >= 5,
            f"{len(calls)} tool calls recorded for this session",
            ", ".join(f"{k}={v}" for k, v in sorted(per_tool.items())),
        )
        print("\n  Recorded calls:")
        for entry in calls:
            print(f"    {entry['timestamp_utc']}  {entry['tool']:<18} "
                  f"{json.dumps(entry.get('parameters', {}))}")

    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 78)
    print(f"RESULT: {passed}/{len(results)} checks passed")
    print("=" * 78)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
