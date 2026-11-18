"""Excel exports of the request list and step timings.

Rows are built by pure functions (`request_rows`, `timing_rows`, `transition_rows`) so they're easy
to test; `workbook_bytes` turns them into an .xlsx. Times are real Excel date-times (UTC) and
durations are hours, so they sort, filter and sum in Excel.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from wft import engine, timings
from wft.definitions import WorkflowDef

HOUR = 3600.0


def _dt(ts: str | None) -> datetime | None:
    """UTC timestamp as a naive datetime (Excel has no time zones; the column headings say UTC)."""
    return timings.parse_ts(ts).replace(tzinfo=None) if ts else None


def _hours(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / HOUR, 3)


REQUEST_COLUMNS = ["Reference", "Title", "Workflow", "Version", "Status", "Owner", "Created by", "Created (UTC)",
                   "Submitted (UTC)", "Finished (UTC)", "Current step", "Step status", "Age (h)",
                   "In current step (h)", "On hold (h)", "Open flags", "Notes", "Attachments", "Follow-up of"]


def request_rows(rows, registry, people_name, now=None) -> list[list[Any]]:
    """One row per request from the request list (`services.Row`s), in the list's order."""
    now = now or timings.now_utc()
    out = []
    for row in rows:
        r = row.request
        defn = _defn(registry, r.workflow)
        finished = r.completed_at or r.cancelled_at or r.rejected_at
        out.append([
            r.reference, r.title, row.workflow_title, defn.version if defn else r.workflow.split("@")[-1],
            r.status, people_name(r.owner), people_name(r.created_by), _dt(r.created_at), _dt(r.submitted_at),
            _dt(finished), row.current_step_title, (row.current_step_status or "").replace("_", " ") or None,
            _hours(row.age), _hours(row.in_step), _hours(timings.time_on_hold(r, now)),
            sum(1 for f in r.flags if f.open), len(r.notes), len(r.attachments), r.follow_up_of,
        ])
    return out


def timing_columns() -> list[str]:
    return (["Reference", "Title", "Workflow", "Version", "Step", "Step title", "Step type", "Step status",
             "Ready (UTC)", "Started (UTC)", "Finished (UTC)", "Queued (h)", "Working (h)"]
            + [f"Waiting: {label} (h)" for label in engine.WAIT_REASONS.values()]
            + ["Waiting total (h)", "On hold (h)", "Elapsed (h)", "Completed by", "Assignee", "Transitions"])


def timing_rows(states: Iterable[engine.RequestState], registry, people_name, now=None) -> list[list[Any]]:
    """One row per step instance that has started its life (has any state change)."""
    now = now or timings.now_utc()
    out = []
    for r in states:
        defn = _defn(registry, r.workflow)
        for st in r.steps:
            if not st.transitions:
                continue
            t = timings.step_timing(st, now, r.holds)
            sd = defn.step(st.id) if defn else None
            out.append(
                [r.reference, r.title, defn.title if defn else r.workflow, defn.version if defn else None, st.id,
                 sd.title if sd else st.id, sd.type if sd else None, st.status.replace("_", " "),
                 _dt(t.ready_at), _dt(t.started_at), _dt(t.finished_at), _hours(t.queued), _hours(t.working)]
                + [_hours(t.waiting.get(reason, 0.0)) for reason in engine.WAIT_REASONS]
                + [_hours(t.waiting_total), _hours(t.held), _hours(t.elapsed), people_name(st.completed_by),
                   people_name(st.assignee), len(st.transitions)])
    return out


TRANSITION_COLUMNS = ["Reference", "Workflow", "Step", "State", "When (UTC)", "Who", "Reason", "Note"]


def transition_rows(states: Iterable[engine.RequestState], registry, people_name) -> list[list[Any]]:
    out = []
    for r in states:
        defn = _defn(registry, r.workflow)
        for st in r.steps:
            for tr in st.transitions:
                out.append([r.reference, defn.title if defn else r.workflow, st.id, tr.status.replace("_", " "),
                            _dt(tr.ts), people_name(tr.actor), tr.reason, tr.note])
    return out


def workbook_bytes(sheets: list[tuple[str, list[str], list[list[Any]]]]) -> bytes:
    """An .xlsx with one sheet per (title, header, rows). Text is always stored as text, never a formula."""
    wb = Workbook()
    wb.remove(wb.active)
    for title, header, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append([None if v is None else v for v in row])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"  # "=HYPERLINK(...)" in a title must stay text (formula injection)
                elif isinstance(cell.value, datetime):
                    cell.number_format = "yyyy-mm-dd hh:mm"
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{len(rows) + 1}"
        for i, name in enumerate(header, start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(40, len(name) + 2))
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _defn(registry, key: str) -> WorkflowDef | None:
    try:
        return registry.get(key)
    except KeyError:
        return None
