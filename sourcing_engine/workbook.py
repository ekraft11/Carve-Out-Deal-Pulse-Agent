"""The maintained Excel workbook.

This is the deliverable: review_queue/pipeline.xlsx, a living document rather
than a repeated export.

Column ownership is the whole idea:

  * The engine owns the data columns (classification, reason, EBITDA, trigger
    status). It refreshes them on every run.
  * The reviewer owns the columns listed in config/output.json under
    human_owned_columns. The engine reads them out of the existing file before
    writing and puts them back unchanged, matched on Company ID.

Anything the engine changed between two runs is recorded on the Changelog
sheet, which is append-only - so "what moved since I last looked" is a
question the file itself answers.

The workbook is rebuilt from scratch on each run rather than edited in place.
That keeps formatting consistent and avoids openpyxl's in-place quirks; the
content that must survive (your columns, the audit trail, the changelog) is
read out first and carried across.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from .config import Guardrails, OutputConfig

# Canonical sheet order. Sheets a given run does not produce are carried over
# from the existing file untouched, so `screening` does not wipe the trigger
# sheets that `monitoring` wrote, or the other way round.
SHEET_ORDER = [
    "00_README",
    "01_Screening",
    "02_Watchlist",
    "03_Active_Pipeline",
    "04_Trigger_Scan",
    "05_Promotion_Proposals",
    "06_Profiles",
    "07_Audit_Trail",
    "08_Changelog",
]

CHANGELOG_SHEET = "08_Changelog"
CHANGELOG_HEADERS = [
    "Timestamp (UTC)",
    "Run ID",
    "Sheet",
    "Company ID",
    "Company",
    "Field",
    "Previous value",
    "New value",
]

# -- palette -----------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F3B4D")
HEADER_FONT_COLOR = "FFFFFF"
HUMAN_FILL = PatternFill("solid", fgColor="FFF2CC")      # your columns
HUMAN_HEADER_FILL = PatternFill("solid", fgColor="BF8F00")
MOCK_FILL = PatternFill("solid", fgColor="FCE4D6")
EXCLUDED_FILL = PatternFill("solid", fgColor="F2F2F2")
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
ACTIVE_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="D9D9D9")
CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


@dataclass
class SheetSpec:
    """One sheet the engine produces."""

    name: str
    headers: list[str]
    rows: list[list[Any]]
    key_header: str | None = None
    #: engine-owned columns to watch for the changelog
    tracked_headers: tuple[str, ...] = ()
    #: header -> allowed values, rendered as a dropdown
    dropdowns: dict[str, Sequence[str]] = field(default_factory=dict)
    #: header -> column width
    widths: dict[str, int] = field(default_factory=dict)
    #: headers whose cells wrap
    wrap_headers: tuple[str, ...] = ()
    #: append this run's rows to whatever is already there (audit trail)
    append_only: bool = False
    #: header whose value drives row shading
    status_header: str | None = None
    note: str = ""


@dataclass
class WriteReport:
    """What happened, so the CLI can tell the user."""

    path: Path
    sheets_written: list[str]
    sheets_carried: list[str]
    human_values_preserved: int
    changelog_entries: list[list[Any]]
    created: bool


class PipelineWorkbook:
    """Reads the existing workbook, merges, and writes it back."""

    def __init__(
        self,
        path: Path,
        output: OutputConfig,
        guardrails: Guardrails,
    ) -> None:
        self.path = path
        self.output = output
        self.guardrails = guardrails
        self.font = output.font_name
        self._prior: dict[str, dict[str, Any]] = {}

    # -- reading what is already there --------------------------------------

    def _read_prior(self) -> dict[str, dict[str, Any]]:
        """Read the existing workbook: headers, all rows, and rows by key.

        Opened with data_only=True so we get the values a reviewer typed
        rather than formula strings. This workbook object is never saved -
        we build a fresh one - so the usual "data_only destroys formulas on
        save" trap does not apply here.
        """
        if not self.path.exists():
            return {}
        try:
            workbook = load_workbook(self.path, data_only=True)
        except Exception as exc:  # a corrupt or open file should not lose data silently
            raise RuntimeError(
                f"Could not read the existing workbook at {self.path}: {exc}\n"
                f"If the file is open in Excel, close it and run again. Nothing "
                f"has been written."
            ) from exc

        prior: dict[str, dict[str, Any]] = {}
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                prior[sheet.title] = {"headers": [], "rows": [], "by_key": {}}
                continue
            headers = [("" if h is None else str(h)) for h in rows[0]]
            body = [list(r) for r in rows[1:]]
            prior[sheet.title] = {"headers": headers, "rows": body, "by_key": {}}
        workbook.close()
        return prior

    def _prior_by_key(self, sheet_name: str, key_header: str) -> dict[str, dict[str, Any]]:
        """Index a prior sheet's rows by their key column value."""
        info = self._prior.get(sheet_name)
        if not info or not info["headers"]:
            return {}
        headers = info["headers"]
        if key_header not in headers:
            return {}
        key_index = headers.index(key_header)
        indexed: dict[str, dict[str, Any]] = {}
        for row in info["rows"]:
            if key_index >= len(row) or row[key_index] in (None, ""):
                continue
            key = str(row[key_index])
            indexed[key] = {
                header: (row[i] if i < len(row) else None)
                for i, header in enumerate(headers)
            }
        return indexed

    # -- writing -------------------------------------------------------------

    def write(
        self,
        specs: Sequence[SheetSpec],
        readme_lines: Sequence[tuple[str, str]],
        run_id: str,
        summary_source_sheet: str | None = "01_Screening",
    ) -> WriteReport:
        self.guardrails.assert_write_allowed(self.path)
        created = not self.path.exists()
        self._prior = self._read_prior()

        human_columns = list(self.output.human_owned_columns)
        preserved = 0
        changelog: list[list[Any]] = []
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        workbook = Workbook()
        workbook.remove(workbook.active)

        spec_by_name = {spec.name: spec for spec in specs}
        written: list[str] = []
        carried: list[str] = []

        for sheet_name in SHEET_ORDER:
            if sheet_name == "00_README":
                self._write_readme(
                    workbook.create_sheet(sheet_name),
                    readme_lines,
                    human_columns,
                    summary_source_sheet,
                    spec_by_name.get(summary_source_sheet or ""),
                )
                written.append(sheet_name)
                continue

            if sheet_name == CHANGELOG_SHEET:
                continue  # written last, once every diff is known

            spec = spec_by_name.get(sheet_name)
            if spec is None:
                if sheet_name in self._prior and self._prior[sheet_name]["headers"]:
                    self._carry_sheet(workbook.create_sheet(sheet_name), sheet_name)
                    carried.append(sheet_name)
                continue

            count, entries = self._write_spec(
                workbook.create_sheet(sheet_name), spec, human_columns, run_id, timestamp
            )
            preserved += count
            # On the very first run there is nothing to compare against, so a
            # diff would just restate the whole file. Record one line saying
            # the workbook was created instead.
            if not created:
                changelog.extend(entries)
            written.append(sheet_name)

        if created:
            changelog.append([
                timestamp, run_id, "-", "-", "-", "Workbook created",
                "", f"{sum(len(s.rows) for s in specs if not s.append_only)} rows written",
            ])

        # -- changelog, append-only -----------------------------------------
        if self.output.changelog_enabled:
            sheet = workbook.create_sheet(CHANGELOG_SHEET)
            prior_rows = []
            info = self._prior.get(CHANGELOG_SHEET)
            if info and info["headers"]:
                prior_rows = [r for r in info["rows"] if any(c is not None for c in r)]
            self._write_table(
                sheet,
                CHANGELOG_HEADERS,
                [list(r) for r in prior_rows] + changelog,
                widths={
                    "Timestamp (UTC)": 22,
                    "Run ID": 26,
                    "Sheet": 18,
                    "Company ID": 12,
                    "Company": 32,
                    "Field": 22,
                    "Previous value": 30,
                    "New value": 30,
                },
                wrap_headers=("Previous value", "New value"),
            )
            written.append(CHANGELOG_SHEET)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(self.path)
        return WriteReport(
            path=self.path,
            sheets_written=written,
            sheets_carried=carried,
            human_values_preserved=preserved,
            changelog_entries=changelog,
            created=created,
        )

    # -- individual sheet writers -------------------------------------------

    def _write_spec(
        self,
        sheet: Worksheet,
        spec: SheetSpec,
        human_columns: list[str],
        run_id: str,
        timestamp: str,
    ) -> tuple[int, list[list[Any]]]:
        """Write one engine-produced sheet, restoring the reviewer's columns."""
        prior_indexed = (
            self._prior_by_key(spec.name, spec.key_header) if spec.key_header else {}
        )
        headers = list(spec.headers)
        applies_human = bool(spec.key_header) and not spec.append_only
        if applies_human:
            headers += [c for c in human_columns if c not in headers]

        rows: list[list[Any]] = []
        preserved = 0
        changelog: list[list[Any]] = []
        key_index = spec.headers.index(spec.key_header) if spec.key_header else None
        name_index = (
            spec.headers.index("Company") if "Company" in spec.headers else None
        )

        if spec.append_only:
            info = self._prior.get(spec.name)
            if info and info["headers"]:
                rows.extend(
                    [list(r) for r in info["rows"] if any(c is not None for c in r)]
                )

        for row in spec.rows:
            new_row = list(row)
            key = str(row[key_index]) if key_index is not None else None
            old = prior_indexed.get(key) if key else None

            if applies_human:
                for column in human_columns:
                    if column in spec.headers:
                        continue
                    value = old.get(column) if old else None
                    if value not in (None, ""):
                        preserved += 1
                    new_row.append("" if value is None else value)

            # -- changelog ------------------------------------------------
            if key and spec.tracked_headers:
                company = str(row[name_index]) if name_index is not None else ""
                if old is None:
                    changelog.append(
                        [timestamp, run_id, spec.name, key, company, "Added to sheet",
                         "", str(row[spec.headers.index(spec.tracked_headers[0])])
                         if spec.tracked_headers[0] in spec.headers else ""]
                    )
                else:
                    for tracked in spec.tracked_headers:
                        if tracked not in spec.headers:
                            continue
                        new_value = row[spec.headers.index(tracked)]
                        old_value = old.get(tracked)
                        if _normalise(old_value) != _normalise(new_value):
                            changelog.append(
                                [timestamp, run_id, spec.name, key, company, tracked,
                                 _display(old_value), _display(new_value)]
                            )
            rows.append(new_row)

        # companies that dropped off the sheet entirely
        if spec.key_header and spec.tracked_headers and not spec.append_only:
            current_keys = {
                str(r[key_index]) for r in spec.rows if key_index is not None
            }
            for gone in sorted(set(prior_indexed) - current_keys):
                changelog.append(
                    [timestamp, run_id, spec.name, gone,
                     _display(prior_indexed[gone].get("Company")),
                     "Removed from sheet",
                     _display(prior_indexed[gone].get(spec.tracked_headers[0])), ""]
                )

        self._write_table(
            sheet,
            headers,
            rows,
            widths=spec.widths,
            wrap_headers=spec.wrap_headers,
            human_columns=human_columns if applies_human else [],
            dropdowns=spec.dropdowns,
            status_header=spec.status_header,
        )
        return preserved, changelog

    def _carry_sheet(self, sheet: Worksheet, sheet_name: str) -> None:
        """Copy a sheet this run did not produce, so nothing is lost."""
        info = self._prior[sheet_name]
        self._write_table(
            sheet,
            info["headers"],
            [list(r) for r in info["rows"] if any(c is not None for c in r)],
            widths={},
            human_columns=list(self.output.human_owned_columns),
        )

    def _write_readme(
        self,
        sheet: Worksheet,
        lines: Sequence[tuple[str, str]],
        human_columns: list[str],
        summary_sheet: str | None,
        summary_spec: SheetSpec | None,
    ) -> None:
        sheet.column_dimensions["A"].width = 32
        sheet.column_dimensions["B"].width = 96
        row = 1

        sheet.cell(row=row, column=1, value="PRIVATE COMPANY SOURCING ENGINE").font = Font(
            name=self.font, bold=True, size=14
        )
        row += 1
        warning = sheet.cell(
            row=row,
            column=1,
            value="MOCK DATA - every company, person and figure in this workbook is FICTIONAL",
        )
        warning.font = Font(name=self.font, bold=True, color="9C0006")
        warning.fill = MOCK_FILL
        sheet.cell(row=row, column=2).fill = MOCK_FILL
        row += 2

        for label, value in lines:
            if not label and not value:
                row += 1
                continue
            if not value:
                heading = sheet.cell(row=row, column=1, value=label)
                heading.font = Font(name=self.font, bold=True, size=11)
                row += 1
                continue
            sheet.cell(row=row, column=1, value=label).font = Font(name=self.font, bold=True)
            cell = sheet.cell(row=row, column=2, value=value)
            cell.font = Font(name=self.font)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            row += 1

        # -- counts, as values counted from the rows this run wrote ----------
        # Deliberately literal rather than COUNTIF formulas: the engine
        # regenerates this workbook on every run, so a formula's ability to
        # self-update buys nothing, while a value displays correctly in every
        # viewer instead of staying blank until Excel first opens the file.
        if summary_sheet and summary_spec and "Classification" in summary_spec.headers:
            row += 1
            sheet.cell(row=row, column=1, value="COUNTS").font = Font(
                name=self.font, bold=True, size=11
            )
            row += 1
            class_index = summary_spec.headers.index("Classification")
            review_index = (
                summary_spec.headers.index("Needs human review")
                if "Needs human review" in summary_spec.headers
                else None
            )
            tallies: dict[str, int] = {}
            flagged = 0
            for data_row in summary_spec.rows:
                if class_index < len(data_row):
                    key = _normalise(data_row[class_index])
                    tallies[key] = tallies.get(key, 0) + 1
                if review_index is not None and review_index < len(data_row):
                    if _normalise(data_row[review_index]) == "yes":
                        flagged += 1

            for label, key in (
                ("Active pipeline", "active_pipeline"),
                ("Watchlist", "watchlist"),
                ("Excluded", "excluded"),
            ):
                sheet.cell(row=row, column=1, value=label).font = Font(name=self.font)
                cell = sheet.cell(row=row, column=2, value=tallies.get(key, 0))
                cell.font = Font(name=self.font, bold=True)
                row += 1
            if review_index is not None:
                sheet.cell(
                    row=row, column=1, value="Flagged for human review"
                ).font = Font(name=self.font)
                cell = sheet.cell(row=row, column=2, value=flagged)
                cell.font = Font(name=self.font, bold=True, color="BF8F00")
                row += 1
            sheet.cell(row=row, column=1, value="Counted").font = Font(
                name=self.font, italic=True
            )
            sheet.cell(
                row=row,
                column=2,
                value=f"As written by this run from the {len(summary_spec.rows)} rows on "
                f"{summary_sheet}. Re-run the command to refresh.",
            ).font = Font(name=self.font, italic=True)
            row += 1

        # -- legend -----------------------------------------------------------
        row += 1
        sheet.cell(row=row, column=1, value="YOUR COLUMNS").font = Font(
            name=self.font, bold=True, size=11
        )
        row += 1
        legend = sheet.cell(
            row=row,
            column=1,
            value=", ".join(human_columns),
        )
        legend.font = Font(name=self.font, bold=True)
        legend.fill = HUMAN_FILL
        explanation = sheet.cell(
            row=row,
            column=2,
            value=(
                "Shaded amber on every sheet. Type in them freely: the engine reads "
                "them before each run and writes them back unchanged, matched on "
                "Company ID. Every other column is engine-owned and will be "
                "overwritten on the next run, so do not keep notes there."
            ),
        )
        explanation.font = Font(name=self.font)
        explanation.alignment = Alignment(wrap_text=True, vertical="top")
        row += 2

        sheet.cell(row=row, column=1, value="Example row").font = Font(
            name=self.font, bold=True
        )
        sheet.cell(
            row=row,
            column=2,
            value='Entscheidung: "Verfolgen"  |  Verantwortlich: "E. Kraft"  |  '
            'Notiz: "Erstkontakt ueber Beiratsmitglied moeglich"  |  '
            'Naechster Schritt: "Warm intro pruefen bis KW 38"',
        ).font = Font(name=self.font, italic=True)

    # -- the shared table renderer ------------------------------------------

    def _write_table(
        self,
        sheet: Worksheet,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        widths: dict[str, int] | None = None,
        wrap_headers: Iterable[str] = (),
        human_columns: Sequence[str] = (),
        dropdowns: dict[str, Sequence[str]] | None = None,
        status_header: str | None = None,
    ) -> None:
        widths = widths or {}
        dropdowns = dropdowns or {}
        wrap_set = set(wrap_headers)
        human_set = set(human_columns)

        for index, header in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=index, value=header)
            cell.font = Font(name=self.font, bold=True, color=HEADER_FONT_COLOR)
            cell.fill = HUMAN_HEADER_FILL if header in human_set else HEADER_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            cell.border = CELL_BORDER
            letter = get_column_letter(index)
            sheet.column_dimensions[letter].width = widths.get(
                header, 60 if header in wrap_set else max(12, min(34, len(header) + 6))
            )
        sheet.row_dimensions[1].height = 30

        status_index = (
            list(headers).index(status_header) if status_header in headers else None
        )
        review_index = (
            list(headers).index("Needs human review")
            if "Needs human review" in headers
            else None
        )

        for row_number, row in enumerate(rows, start=2):
            row_fill = None
            if status_index is not None and status_index < len(row):
                status = _normalise(row[status_index])
                if status == "excluded":
                    row_fill = EXCLUDED_FILL
                elif status == "active_pipeline":
                    row_fill = ACTIVE_FILL
            if row_fill is None and review_index is not None and review_index < len(row):
                if _normalise(row[review_index]) == "yes":
                    row_fill = REVIEW_FILL

            for index, header in enumerate(headers, start=1):
                value = row[index - 1] if index - 1 < len(row) else ""
                cell = sheet.cell(row=row_number, column=index, value=value)
                cell.font = Font(name=self.font)
                cell.border = CELL_BORDER
                cell.alignment = Alignment(
                    wrap_text=header in wrap_set,
                    vertical="top",
                )
                if header in human_set:
                    cell.fill = HUMAN_FILL
                elif row_fill is not None:
                    cell.fill = row_fill

        if headers:
            sheet.freeze_panes = "A2"
            if rows:
                sheet.auto_filter.ref = (
                    f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
                )

        # dropdowns for the reviewer's decision columns
        for header, options in dropdowns.items():
            if header not in headers or not options:
                continue
            letter = get_column_letter(list(headers).index(header) + 1)
            validation = DataValidation(
                type="list",
                formula1='"' + ",".join(options) + '"',
                allow_blank=True,
                showDropDown=False,
            )
            validation.error = "Please choose one of the listed values."
            validation.errorTitle = "Value not allowed"
            sheet.add_data_validation(validation)
            last = max(len(rows) + 1, 200)  # room to grow
            validation.add(f"{letter}2:{letter}{last}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normalise(value: Any) -> str:
    """Compare cell values without tripping over 24.7 vs '24.7'."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value).strip()


def _display(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def read_sheet_by_key(
    path: Path, sheet_name: str, key_header: str
) -> dict[str, dict[str, Any]]:
    """Read one sheet of an existing workbook, indexed by its key column.

    Used to pick up decisions a reviewer typed into the workbook, which is the
    only way a human instruction travels back into the engine. Opened
    read-only with data_only=True and never saved.
    """
    if not Path(path).exists():
        return {}
    workbook = load_workbook(path, data_only=True)
    if sheet_name not in workbook.sheetnames:
        workbook.close()
        return {}
    sheet = workbook[sheet_name]
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows:
        return {}
    headers = [("" if h is None else str(h)) for h in rows[0]]
    if key_header not in headers:
        return {}
    key_index = headers.index(key_header)
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        if key_index >= len(row) or row[key_index] in (None, ""):
            continue
        indexed[str(row[key_index])] = {
            header: (row[i] if i < len(row) else None)
            for i, header in enumerate(headers)
        }
    return indexed
