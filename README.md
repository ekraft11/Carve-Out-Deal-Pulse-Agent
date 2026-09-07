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

Everything lands in **`review_queue/pipeline.xlsx`**, a workbook the engine maintains
rather than overwrites - see below.

## Design principles

- **Mock-first.** A local MCP server serves realistic fake data for ~30 fictional
  healthcare companies. Mock and real sources sit behind the same interface, so
  swapping in a real connector later does not touch the screening logic.
- **Guardrails as configuration.** Read-only tools only, no write access, no open web
  access, an explicit tool allowlist. `config/guardrails.json` holds these settings and
  the engine *refuses to start* if any of them is weakened.
- **Human-in-the-loop.** The engine never sends anything anywhere. Every output lands
  in `./review_queue/` as a maintained Excel workbook plus markdown companions.
- **Full audit trail.** Every data access is appended to `./audit_log.jsonl` with
  timestamp, tool name and parameters.

## Requirements

Python 3.11+ and one package for the Excel output:

```bash
pip install -r requirements.txt   # openpyxl
```

The screening and monitoring logic itself uses only the standard library.
(The optional MCP server additionally needs the `mcp` package.)

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
config/output.json            workbook filename and which columns belong to the reviewer
data/mock_universe.json       30 fictional companies, their owners, executives and signals
sourcing_engine/models.py     the data contract every source must satisfy
sourcing_engine/data_sources/ base interface, mock backend, real-connector stubs
review_queue/pipeline.xlsx    the maintained workbook (git-ignored, regenerated on each run)
state/pipeline_state.json     human decisions: approved and pending promotions (git-ignored)
audit_log.jsonl               append-only record of every data access (git-ignored)
```

## The maintained workbook

`review_queue/pipeline.xlsx` is a living document, not a repeated export. Column
ownership is explicit:

- **The engine owns the data columns** - classification, reason, EBITDA, trigger status -
  and refreshes them on every run.
- **You own the columns listed in `config/output.json`** (`Entscheidung`,
  `Verantwortlich`, `Notiz`, `Naechster Schritt`). They are shaded amber, carry a
  dropdown where useful, and the engine reads them before each run and writes them back
  unchanged, matched on Company ID.

Anything the engine changed between two runs is appended to the `08_Changelog` sheet,
so "what moved since I last looked" is a question the file answers itself. The audit
trail on `07_Audit_Trail` is appended the same way. Sheets a given command does not
produce are carried over untouched, so `screening` never wipes what `monitoring` wrote.

| Sheet | Written by |
|---|---|
| `00_README` | every run - criteria applied, counts, legend |
| `01_Screening` | `screening` - all companies, classification, written reason |
| `02_Watchlist` | `screening` |
| `03_Active_Pipeline` | `screening` - human-approved promotions only |
| `04_Trigger_Scan` | `monitoring` - every signal, confirmed or not, with the reason |
| `05_Promotion_Proposals` | `monitoring` - your `Freigabe` column decides |
| `06_Profiles` | `monitoring` - profile, bios and outreach note per active company |
| `07_Audit_Trail` | every run, appended |
| `08_Changelog` | every run, appended |

## Approving a promotion

The engine proposes; you decide. The decision travels back through the workbook,
which is the only path by which a human instruction enters the engine:

1. `python run.py monitoring` writes a proposal to `05_Promotion_Proposals` with
   status `pending_human_approval`.
2. You type `Freigegeben`, `Abgelehnt` or `Zurueckgestellt` in the `Freigabe`
   column (there is a dropdown) and optionally your name in `Freigabe durch`.
3. The next `python run.py monitoring` reads that cell, records the decision in
   `state/pipeline_state.json`, and only then does the company appear in
   `03_Active_Pipeline` with a full profile pack.

A blank approval cell means "not decided yet" and changes nothing. A word the
engine cannot interpret is reported on the console and left alone rather than
guessed at. A declined promotion is not re-proposed automatically.

## Trigger weighting

Not every confirmed trigger justifies a promotion, so
`triggers.promotion_policy` in `criteria.json` weights the four categories:

| Category | Weight |
|---|---|
| `succession` | standalone - one is enough |
| `ownership_governance_change` | standalone - one is enough |
| `c_suite_change` | supporting - two supporting signals required |
| `capital_event` | supporting - two supporting signals required |

A signal only counts as a *confirmed trigger* if it clears the confidence
threshold, is corroborated, falls inside the age window, and belongs to a known
category. Every rejected signal keeps its rejection reason on `04_Trigger_Scan`,
so "why did we not act on that?" stays answerable. The engine refuses to start
if a category has no weighting, so a confirmed trigger can never silently count
for nothing.

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
| 4 | Screening engine + maintained Excel workbook | done |
| 5 | Monitoring engine, trigger weighting, promotion proposals | done |
| 6 | Output packs: profile, bios, outreach note | done |
| 3 | Read-only MCP server | to do |
| 7 | `run.py audit` view + demo walkthrough | to do |
