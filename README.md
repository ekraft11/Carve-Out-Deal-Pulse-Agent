# Private Company Sourcing Engine (prototype)

Turns static, export-based private company screening into a living pipeline.

> **All company data in this prototype is FICTIONAL and labelled `MOCK DATA`.**
> Real connectors (Gain.pro, Inven, FactSet) are stubbed, not implemented.
> Nothing here should be relied on without human verification.

## What it does

1. **Screens** a universe of private healthcare companies against the investment criteria
   (family/founder ownership, minimum EBITDA, geography tiers, listed-company exclusion).
2. **Classifies** each company as `active_pipeline`, `watchlist` or `excluded`, with a
   written reason. Structural fit *without* a behavioural trigger goes to the watchlist —
   never straight to active pipeline. Borderline cases are flagged for human review.
3. **Monitors** for four behavioural trigger categories: succession, C-suite change,
   ownership/governance change, capital events. A confirmed trigger on a watchlist company
   *proposes* promotion to active pipeline. A human approves it.
4. **Generates** a company profile, executive bios and an outreach-readiness note per
   active company.

## Design principles

- **Mock-first.** A local MCP server serves realistic fake data for ~30 fictional
  healthcare companies. Mock and real sources sit behind the same interface, so
  swapping in a real connector later does not touch the screening logic.
- **Guardrails as configuration.** Read-only tools only, no write access, no open web
  access, an explicit tool allowlist. `config/guardrails.json` holds these settings and
  the engine *refuses to start* if any of them is weakened.
- **Human-in-the-loop.** The engine never sends anything anywhere. Every output lands
  as markdown in `./review_queue/`.
- **Full audit trail.** Every data access is appended to `./audit_log.jsonl` with
  timestamp, tool name and parameters.

## Requirements

Python 3.11+. No third-party packages needed for the screening and monitoring demo.
(The optional MCP server needs the `mcp` package.)

## Usage

```bash
python run.py check        # verify configuration and guardrails
python run.py universe     # show the company universe as the data layer sees it
python run.py screening    # screen the universe against the criteria
python run.py monitoring   # scan the universe for behavioural triggers
python run.py audit        # show what data the engine read
```

## Layout

```
config/criteria.json          the investment screen - thresholds are knobs, not code
config/guardrails.json        the safety envelope, enforced at startup
data/mock_universe.json       30 fictional companies, their owners, executives and signals
sourcing_engine/models.py     the data contract every source must satisfy
sourcing_engine/data_sources/ base interface, mock backend, real-connector stubs
review_queue/                 generated output for human review (git-ignored)
state/                        JSON state files (git-ignored)
audit_log.jsonl               append-only record of every data access (git-ignored)
```

## Swapping in a real data source

`sourcing_engine/models.py` defines the contract. A real connector subclasses
`DataSource` and implements five private fetch methods; the audited public methods are
inherited, so a new connector cannot bypass the allowlist or the audit log. Point
`data_sources.active` in `config/guardrails.json` at it and nothing else changes.

## Build status

| Step | Component | Status |
|------|-----------|--------|
| 1 | Skeleton, criteria config, audit logger | done |
| 2 | Mock universe + swappable data-source interface | done |
| 3 | Read-only MCP server | to do |
| 4 | Screening engine | to do |
| 5 | Monitoring engine | to do |
| 6 | Output pack generator | to do |
| 7 | Demo wiring + audit view | to do |
