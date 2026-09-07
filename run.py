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
from datetime import date

from sourcing_engine import DATA_LABEL, __version__
from sourcing_engine.audit import AuditLogger, format_run_table, summarise_run
from sourcing_engine.config import (
    ConfigError,
    GuardrailViolation,
    REPO_ROOT,
    load_settings,
)
from sourcing_engine.data_sources import REGISTRY, DataSourceError, open_data_source
from sourcing_engine.models import SchemaError
from sourcing_engine.reporting import (
    active_pipeline_sheet,
    audit_sheet,
    readme_lines,
    screening_sheet,
    watchlist_sheet,
    write_screening_markdown,
)
from sourcing_engine.screening import (
    CLASS_ACTIVE,
    CLASS_EXCLUDED,
    CLASS_WATCHLIST,
    Screener,
    ScreeningResult,
)
from sourcing_engine.state import PipelineState
from sourcing_engine.workbook import PipelineWorkbook

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


def cmd_universe(_args: argparse.Namespace) -> int:
    """Read the whole universe through the data layer and show what came back."""
    settings = load_settings()
    guardrails = settings.guardrails
    criteria = settings.criteria

    logger = AuditLogger.for_run(guardrails, command="universe")
    source = open_data_source(guardrails, logger)

    print(RULE)
    print(BANNER)
    print(f"data source: {source.source_label}")
    print(RULE)

    companies = source.search_companies()

    header = (
        f"{'ID':<9} {'NAME':<31} {'CTRY':<5} {'TIER':<12} "
        f"{'EBITDA':>7}  {'LISTED':<7} {'CONTROL TYPE':<32} {'FAM%':>5} {'PE%':>5}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    for company in companies:
        ownership = source.get_ownership(company.company_id)
        tier = criteria.tier_for_country(company.country) or "out of scope"
        ebitda = "n/a" if company.ebitda_eur_m is None else f"{company.ebitda_eur_m:.1f}"
        print(
            f"{company.company_id:<9} {company.name[:31]:<31} {company.country:<5} "
            f"{tier:<12} {ebitda:>7}  {'yes' if company.is_listed else 'no':<7} "
            f"{ownership.control_type:<32} "
            f"{ownership.family_founder_stake_pct:>5.0f} {ownership.pe_stake_pct:>5.0f}"
        )

    # A rough shape-of-the-universe count. This is NOT the classification -
    # that arrives in step 4 and applies the full rule set.
    listed = [c for c in companies if c.is_listed]
    out_of_scope = [c for c in companies if criteria.tier_for_country(c.country) is None]
    print(f"\nUNIVERSE SHAPE ({len(companies)} companies)")
    print(f"  listed (must be excluded)          {len(listed)}")
    print(f"  outside target geographies         {len(out_of_scope)}")
    print(f"  private and in scope               "
          f"{len(companies) - len(listed) - len(out_of_scope)}")

    signals = source.get_signals()
    per_category: dict[str, int] = {}
    for signal in signals:
        per_category[signal.category] = per_category.get(signal.category, 0) + 1
    print(f"\nSIGNALS ON FILE ({len(signals)})")
    for category in criteria.trigger_categories:
        print(f"  {category:<30} {per_category.get(category, 0)}")

    print("\nCONNECTORS")
    for name, source_class in sorted(REGISTRY.items()):
        active = " (active)" if name == guardrails.active_data_source else ""
        status = "implemented" if name == "mock" else "stub - not configured, no credentials"
        print(f"  {name:<10} {status}{active}")

    logger.log_event("run_finished", {"command": "universe", "tool_calls": logger.call_count})
    summary = summarise_run(guardrails.audit_log_path, logger.run_id)
    print("\nAUDIT LOG")
    print(f"  run id      {summary['run_id']}")
    print(f"  tool calls  {summary['tool_calls']} recorded this run")
    for tool, count in sorted(summary["per_tool"].items()):
        print(f"    {tool:<20} {count}")
    print(f"\n{RULE}")
    print(f"All company data above is FICTIONAL ({criteria.data_label}).")
    print(RULE)
    return 0


def cmd_screening(_args: argparse.Namespace) -> int:
    """Screen the universe, classify with reasons, and maintain the workbook."""
    settings = load_settings()
    guardrails, criteria, output = settings.guardrails, settings.criteria, settings.output
    settings.ensure_output_dirs()

    logger = AuditLogger.for_run(guardrails, command="screening")
    source = open_data_source(guardrails, logger)
    state = PipelineState.load(guardrails)

    print(RULE)
    print(BANNER)
    print(f"data source: {source.source_label}")
    print(RULE)

    as_of = getattr(source, "as_of", None) or date.today()
    screener = Screener(criteria, source, as_of)
    result = ScreeningResult(
        verdicts=screener.screen_all(), as_of=as_of, run_id=logger.run_id
    )
    # Screening produces watchlist or excluded only. Companies reach the active
    # pipeline solely because a human approved a promotion in an earlier run.
    result.apply_approved_promotions(state.approved_ids)

    counts = result.counts
    print(f"\nCLASSIFIED {len(result.verdicts)} COMPANIES")
    print(f"  active pipeline (human-approved)   {counts[CLASS_ACTIVE]}")
    print(f"  watchlist                          {counts[CLASS_WATCHLIST]}")
    print(f"  excluded                           {counts[CLASS_EXCLUDED]}")
    print(f"  flagged for human review           {len(result.flagged)}")

    print("\nFLAGGED FOR HUMAN REVIEW (not silently sorted)")
    for verdict in result.flagged:
        print(f"  {verdict.company_id}  {verdict.name[:34]:<34} -> {verdict.classification}")
        for flag in verdict.review_flags:
            print(f"            - {flag}")

    print("\nEXCLUDED")
    for verdict in result.by_classification(CLASS_EXCLUDED):
        first_reason = verdict.exclusion_reasons[0] if verdict.exclusion_reasons else ""
        print(f"  {verdict.company_id}  {verdict.name[:34]:<34} {first_reason[:78]}")

    # -- the maintained workbook -------------------------------------------
    logger.log_event("run_finished", {"command": "screening", "tool_calls": logger.call_count})
    specs = [
        screening_sheet(result, output.decision_options),
        watchlist_sheet(result, output.decision_options),
        active_pipeline_sheet(result, state, output.decision_options),
        audit_sheet(guardrails, logger.run_id),
    ]
    workbook = PipelineWorkbook(settings.workbook_path, output, guardrails)
    report = workbook.write(
        specs,
        readme_lines(criteria, guardrails, result, state, source.source_label, "screening"),
        run_id=logger.run_id,
    )

    state.last_screening_run = logger.run_id
    state_path = state.save(guardrails)

    markdown_path = None
    if output.write_markdown_packs:
        markdown_path = write_screening_markdown(
            result, criteria, guardrails, output, source.source_label
        )

    print("\nOUTPUT")
    verb = "created" if report.created else "updated"
    print(f"  workbook {verb}   ./{report.path.relative_to(REPO_ROOT)}")
    print(f"    sheets written    {', '.join(report.sheets_written)}")
    if report.sheets_carried:
        print(f"    sheets preserved  {', '.join(report.sheets_carried)}")
    print(f"    your entries kept {report.human_values_preserved} "
          f"(columns: {', '.join(output.human_owned_columns)})")
    print(f"    changelog entries {len(report.changelog_entries)} added this run")
    if markdown_path:
        print(f"  markdown summary  ./{markdown_path.relative_to(REPO_ROOT)}")
    print(f"  state             ./{state_path.relative_to(REPO_ROOT)}")
    print(f"  audit log         ./{guardrails.audit_log_path.relative_to(REPO_ROOT)} "
          f"({logger.call_count} entries this run)")

    print(f"\n{RULE}")
    print(f"All company data is FICTIONAL ({criteria.data_label}). "
          f"Verify before relying on or sharing any of it.")
    print(RULE)
    return 0


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

    universe = subparsers.add_parser("universe", help="show the company universe")
    universe.set_defaults(handler=cmd_universe)

    screening = subparsers.add_parser(
        "screening", help="screen the universe and update the workbook"
    )
    screening.set_defaults(handler=cmd_screening)

    monitoring = subparsers.add_parser("monitoring", help="scan for triggers (step 5)")
    monitoring.set_defaults(handler=cmd_not_built(5, "monitoring"))

    audit = subparsers.add_parser("audit", help="show recorded data access (step 7)")
    audit.set_defaults(handler=cmd_not_built(7, "audit"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, GuardrailViolation, DataSourceError, SchemaError) as exc:
        print(f"\nSTOPPED: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
