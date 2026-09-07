#!/usr/bin/env python3
"""Private Company Sourcing Engine - command line entry point.

Usage:
    python run.py check        Verify configuration and guardrails
    python run.py screening    Screen the universe against the criteria
    python run.py monitoring   Scan the universe for behavioural triggers
    python run.py audit        Show what data the engine read

All data in this prototype is FICTIONAL and labelled "MOCK DATA".
"""

from __future__ import annotations

import argparse
import sys

from sourcing_engine import DATA_LABEL, __version__
from sourcing_engine.audit import AuditLogger, format_run_table, summarise_run
from sourcing_engine.config import (
    ConfigError,
    GuardrailViolation,
    REPO_ROOT,
    load_settings,
)

BANNER = f"Private Company Sourcing Engine v{__version__}  [{DATA_LABEL}]"
RULE = "=" * 78


def _tick(ok: bool) -> str:
    return "  OK  " if ok else " FAIL "


def cmd_check(_args: argparse.Namespace) -> int:
    """Load the configuration, enforce the guardrails, prove the log works."""
    print(RULE)
    print(BANNER)
    print(RULE)

    settings = load_settings()
    criteria = settings.criteria
    guardrails = settings.guardrails

    print("\nCONFIGURATION")
    print(f"  criteria version        {criteria.version}")
    print(f"  data label              {criteria.data_label}")
    print(f"  active data source      {guardrails.active_data_source}")
    print(f"  repository root         {REPO_ROOT}")

    print("\nSCREENING CRITERIA")
    print(f"  minimum EBITDA          EUR {criteria.min_ebitda:.0f}m")
    low, high = criteria.ebitda_borderline_band
    print(f"  borderline EBITDA band  EUR {low:.0f}m - {high:.0f}m  (flagged for human review)")
    print(f"  max PE stake            {criteria.max_pe_stake:.1f}%")
    print(f"  min family/founder      {criteria.min_family_stake:.1f}%")
    for region, spec in criteria.geography["regions"].items():
        countries = ", ".join(spec["countries"])
        print(f"  geography {region:<10}    {spec['tier']:<10} ({countries})")
    print(f"  listed companies        excluded: {criteria.listing['exclude_if_listed']}")
    print(f"  trigger categories      {', '.join(criteria.trigger_categories)}")

    print("\nGUARDRAILS (enforced in code, not requested in a prompt)")
    checks = [
        ("read-only data access", guardrails.read_only),
        ("no open web access", not guardrails.web_access_enabled),
        ("agent cannot send anything externally", not guardrails.agent_may_send_external_messages),
        ("promotions require human approval", guardrails.promotions_require_human_approval),
        ("borderline cases require human review", guardrails.borderline_cases_require_human_review),
        ("structural fit alone cannot reach active pipeline",
         criteria.classification["structural_fit_without_trigger"] == "watchlist"),
    ]
    for label, ok in checks:
        print(f"  [{_tick(ok)}] {label}")
    print(f"\n  allowlisted tools ({len(guardrails.allowlisted_tools)}): "
          f"{', '.join(guardrails.allowlisted_tools)}")

    print("\nOUTPUT LOCATIONS (the only places the engine may write)")
    settings.ensure_output_dirs()
    for label, path in (
        ("review queue", guardrails.review_queue_dir),
        ("state", guardrails.state_dir),
        ("audit log", guardrails.audit_log_path),
    ):
        print(f"  {label:<14} ./{path.relative_to(REPO_ROOT)}")

    # Prove the allowlist actually blocks something, and that the audit log
    # records both the permitted probe and the refusal.
    print("\nSELF-TEST")
    logger = AuditLogger.for_run(guardrails, command="check")

    probe_tool = guardrails.allowlisted_tools[0]
    guardrails.assert_tool_allowed(probe_tool)
    logger.log_tool_call(
        probe_tool,
        parameters={"self_test": True},
        result_summary="allowlist probe - no data requested",
    )
    print(f"  [{_tick(True)}] allowlisted tool '{probe_tool}' permitted and logged")

    blocked_tool = "send_email"
    try:
        guardrails.assert_tool_allowed(blocked_tool)
        blocked_ok = False
    except GuardrailViolation as exc:
        blocked_ok = True
        logger.log_blocked(blocked_tool, reason=str(exc), parameters={"self_test": True})
    print(f"  [{_tick(blocked_ok)}] non-allowlisted tool '{blocked_tool}' refused and logged")

    write_blocked = False
    try:
        guardrails.assert_write_allowed(REPO_ROOT / "sourcing_engine" / "config.py")
    except GuardrailViolation:
        write_blocked = True
    print(f"  [{_tick(write_blocked)}] write outside the output directories refused")

    logger.log_event("run_finished", {"command": "check", "tool_calls": logger.call_count})

    summary = summarise_run(guardrails.audit_log_path, logger.run_id)
    print("\nAUDIT LOG")
    print(f"  run id     {summary['run_id']}")
    print(f"  entries    {summary['entries']} written to ./{guardrails.audit_log_path.relative_to(REPO_ROOT)}")
    print()
    print(format_run_table(guardrails.audit_log_path, logger.run_id))

    all_ok = all(ok for _, ok in checks) and blocked_ok and write_blocked
    print(f"\n{RULE}")
    print("RESULT: configuration valid, guardrails enforced." if all_ok
          else "RESULT: one or more checks FAILED - see above.")
    print(RULE)
    return 0 if all_ok else 1


def cmd_not_built(step: int, name: str):
    def _handler(_args: argparse.Namespace) -> int:
        print(f"'{name}' is not built yet - it arrives in step {step} of the build.")
        return 2
    return _handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=f"{BANNER} - all data is fictional.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="verify configuration and guardrails")
    check.set_defaults(handler=cmd_check)

    screening = subparsers.add_parser("screening", help="screen the universe (step 4)")
    screening.set_defaults(handler=cmd_not_built(4, "screening"))

    monitoring = subparsers.add_parser("monitoring", help="scan for triggers (step 5)")
    monitoring.set_defaults(handler=cmd_not_built(5, "monitoring"))

    audit = subparsers.add_parser("audit", help="show recorded data access (step 7)")
    audit.set_defaults(handler=cmd_not_built(7, "audit"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, GuardrailViolation) as exc:
        print(f"\nSTOPPED: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
