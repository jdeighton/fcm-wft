"""Off-process records: setups done entirely outside WFT (urgent changes, unusual structures...).

They aren't workflow requests, but they're stored the same way: a `off_process_recorded` event
(seq 1) in the recorder's journal, plus annotations for the readable reference, notes and
attachments. Pure functions; the services stamp and store events.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Iterable

from wft.engine import (ANNOTATION_SEQ, ATTACHMENT_ADDED, MAX_ATTACHMENT_BYTES, NOTE_ADDED, REFERENCE_ASSIGNED,
                        Attachment, CommandError, NewEvent, Note)
from wft.events import Event

DEFINITION = "off_process@1"  # recorded on every event, in place of a workflow definition
OFF_PROCESS_RECORDED = "off_process_recorded"
REFERENCE_PREFIX = "OFF"
MAX_TEXT = 4000


@dataclass(frozen=True)
class OffProcessRecord:
    record_id: str
    system: str
    what: str
    performed_on: str          # ISO date the change was made
    reason: str                # reason category id
    notes: str
    linked_request: str | None
    recorded_by: str
    recorded_at: str
    reference: str | None = None
    attachments: tuple[Attachment, ...] = ()
    extra_notes: tuple[Note, ...] = ()

    def attachment(self, sha256: str) -> Attachment:
        for a in self.attachments:
            if a.sha256 == sha256:
                return a
        raise KeyError(sha256)


def record(system: str, what: str, performed_on: str, reason: str, notes: str, linked_request: str | None,
           reason_ids: Iterable[str], today: date) -> NewEvent:
    system, what, notes = system.strip(), what.strip(), notes.strip()
    if not system:
        raise CommandError("Say which trading system")
    if not what:
        raise CommandError("Say what was set up or changed")
    if not notes:
        raise CommandError("Notes are required: what was done and why it was done outside WFT")
    if len(what) > MAX_TEXT or len(notes) > MAX_TEXT:
        raise CommandError(f"Keep each field under {MAX_TEXT} characters")
    if reason not in set(reason_ids):
        raise CommandError("Choose a reason")
    try:
        when = date.fromisoformat(performed_on)
    except (TypeError, ValueError):
        raise CommandError("Give the date the change was made") from None
    if when > today + timedelta(days=1):
        raise CommandError("The date can't be in the future")
    return NewEvent(OFF_PROCESS_RECORDED, seq=1, payload={
        "system": system, "what": what, "performed_on": when.isoformat(), "reason": reason, "notes": notes,
        "linked_request": linked_request or None})


def add_attachment(rec: OffProcessRecord, name: str, size: int, sha256: str, path: str) -> NewEvent:
    if size > MAX_ATTACHMENT_BYTES:
        raise CommandError(f"Files are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
    if any(a.sha256 == sha256 and a.name == name for a in rec.attachments):
        raise CommandError(f"{name} is already attached")
    return NewEvent(ATTACHMENT_ADDED, seq=ANNOTATION_SEQ,
                    payload={"name": name, "size": size, "sha256": sha256, "path": path, "step": None})


def add_note(rec: OffProcessRecord, text: str) -> NewEvent:
    text = text.strip()
    if not text:
        raise CommandError("A note can't be empty")
    return NewEvent(NOTE_ADDED, seq=ANNOTATION_SEQ, payload={"text": text[:MAX_TEXT]})


def assign_reference(rec: OffProcessRecord, reference: str) -> NewEvent:
    if rec.reference:
        raise CommandError(f"Record already has reference {rec.reference}")
    return NewEvent(REFERENCE_ASSIGNED, seq=ANNOTATION_SEQ, payload={"reference": reference})


def replay(events: Iterable[Event]) -> OffProcessRecord:
    events = sorted(events, key=lambda e: (e.seq != 1, e.ts, e.event_id))
    first = events[0]
    if first.type != OFF_PROCESS_RECORDED:
        raise ValueError(f"{first.request_id} is not an off-process record")
    p = first.payload
    rec = OffProcessRecord(first.request_id, p["system"], p["what"], p["performed_on"], p["reason"], p["notes"],
                           p.get("linked_request"), first.actor, first.ts)
    for e in events[1:]:
        if e.type == REFERENCE_ASSIGNED and not rec.reference:
            rec = replace(rec, reference=e.payload["reference"])
        elif e.type == ATTACHMENT_ADDED:
            q = e.payload
            rec = replace(rec, attachments=rec.attachments + (
                Attachment(e.ts, e.actor, q["name"], q["size"], q["sha256"], q["path"]),))
        elif e.type == NOTE_ADDED:
            rec = replace(rec, extra_notes=rec.extra_notes + (Note(e.ts, e.actor, e.payload["text"]),))
    return rec
