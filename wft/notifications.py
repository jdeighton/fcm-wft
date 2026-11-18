"""What to tell a user about changes other people made, and how to show it on the desktop.

`derive` compares a request before and after new events arrive (pure). Notifiers show a notice
as a desktop toast: on Windows via PowerShell's built-in toast API (no extra packages), or not at
all, leaving it to the browser's own notifications.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Protocol

from wft import engine
from wft.engine import RequestState
from wft.events import Event

log = logging.getLogger(__name__)

STEP_READY = "step_ready"
STEP_ASSIGNED = "step_assigned"
OWNER_CHANGED = "owner_changed"
FLAG_RAISED = "flag_raised"
STATUS_CHANGED = "status_changed"

# Lifecycle changes worth telling the owner about.
_EVENT_WORDS = {engine.REQUEST_HELD: "put on hold", engine.REQUEST_RESUMED: "resumed",
                engine.REQUEST_CANCELLED: "cancelled"}
_SELF_MOVING = ("subworkflow", "decision")  # steps that move on by themselves: nobody needs telling


@dataclass(frozen=True)
class Notice:
    request_id: str
    kind: str
    title: str   # short, for a toast heading
    text: str
    ts: str      # when it happened (UTC)


def derive(before: RequestState | None, after: RequestState, new_events: Iterable[Event], me: str,
           step_titles: dict[str, str] | None = None, floor: str | None = None,
           step_types: dict[str, str] | None = None) -> list[Notice]:
    """Notices for `me` about changes others made. `floor`: ignore events at or before this UTC time."""
    others = [e for e in new_events if e.actor != me and (floor is None or e.ts > floor)]
    if not others:
        return []
    by = others[-1].actor
    ts = max(e.ts for e in others)
    titles = step_titles or {}
    types = step_types or {}
    name = after.reference or after.title
    out: list[Notice] = []

    def add(kind: str, text: str) -> None:
        out.append(Notice(after.request_id, kind, name, text, ts))

    # Hand-overs and lifecycle changes come from the events themselves, so none is lost when several
    # arrive together (e.g. held, resumed and cancelled between two polls).
    for e in others:
        if e.type == engine.OWNER_CHANGED and e.payload.get("owner") == me:
            out.append(Notice(after.request_id, OWNER_CHANGED, name, f"{e.actor} made you the owner of {after.title}", e.ts))
        elif e.type in _EVENT_WORDS and after.owner == me and not e.payload.get("auto"):  # not knock-ons from a parent
            out.append(Notice(after.request_id, STATUS_CHANGED, name, f"{e.actor} {_EVENT_WORDS[e.type]} the request", e.ts))
    was = {s.id: s for s in (before.steps if before else ())}
    for st in after.steps:
        prev = was.get(st.id)
        title = titles.get(st.id, st.id)
        if st.assignee == me and (prev is None or prev.assignee != me):
            add(STEP_ASSIGNED, f"{by} gave you “{title}”")
        elif (after.owner == me and st.status == engine.READY and (prev is None or prev.status != engine.READY)
              and st.assignee is None and types.get(st.id) not in _SELF_MOVING):
            add(STEP_READY, f"“{title}” is ready")
    if after.owner == me:
        old_flags = {f.id for f in (before.flags if before else ())}
        for f in after.flags:
            if f.id not in old_flags:
                add(FLAG_RAISED, f"Flag on “{titles.get(f.step, f.step or 'request')}”: {f.action}")
        if after.status in (engine.COMPLETED, engine.REJECTED) and (before is None or before.status != after.status):
            add(STATUS_CHANGED, f"{after.title} is {after.status.lower()}")
    return out


class Notifier(Protocol):
    def show(self, notice: Notice) -> None: ...


class NullNotifier:
    def show(self, notice: Notice) -> None:
        pass


class RecordingNotifier:
    """For tests."""

    def __init__(self) -> None:
        self.shown: list[Notice] = []

    def show(self, notice: Notice) -> None:
        self.shown.append(notice)


_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:WFT_TOAST_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:WFT_TOAST_TEXT)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


class WindowsToastNotifier:
    """Native Windows toasts through PowerShell's WinRT toast API.

    The title and text are passed in environment variables, never spliced into the script, so
    request titles can't inject commands. Failures (e.g. PowerShell blocked by policy) are logged
    once and then toasts are skipped.
    """

    def __init__(self, env_label: str = ""):
        self.prefix = f"WFT {env_label.upper()} · " if env_label and env_label != "prod" else "WFT · "
        self.broken = False

    def show(self, notice: Notice) -> None:
        if self.broken:
            return
        env = {**os.environ, "WFT_TOAST_TITLE": self.prefix + notice.title, "WFT_TOAST_TEXT": notice.text}
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                              "-Command", _TOAST_SCRIPT], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            self.broken = True
            log.warning("Desktop toasts unavailable (%s); notifications still show in WFT", e)


def make_notifier(setting: str, env: str) -> Notifier:
    """`windows` (default on Windows), `browser` (default elsewhere: the page shows them), or `off`."""
    setting = (setting or ("windows" if sys.platform == "win32" else "browser")).lower()
    if setting == "windows" and sys.platform == "win32":
        return WindowsToastNotifier(env)
    return NullNotifier()
