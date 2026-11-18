"""FastAPI web UI, served on localhost to the user's own browser."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wft import __version__, engine, timings
from wft.engine import CommandError
from wft.events import Event
from wft.services import STATUS_FILTERS, FileTooLarge, NotFound, ReadOnly, Services
from wft.web import security
from wft.web.forms import build_fields, parse_form, picklist_names
from wft.adapters import AdapterUnavailable, NotFoundInSystem
from wft.artefacts import ArtefactInvalid

HERE = Path(__file__).parent


def create_app(services: Services, env: str, token: str, port: int, browser_notifications: bool = True) -> FastAPI:
    app = FastAPI(title="WFT", docs_url=None, redoc_url=None, openapi_url=None)
    security.install(app, token, port)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(env=env, user=services.user)
    templates.env.filters["person"] = lambda u: services.team.name(u) if u else "—"
    templates.env.filters["duration"] = timings.format_duration
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["reason_label"] = services.reason_label
    templates.env.filters["utc"] = lambda ts: (ts or "")[:16].replace("T", " ")
    templates.env.globals.update(wait_reasons=engine.WAIT_REASONS, step_timing=timings.step_timing,
                                 resolutions=engine.RESOLUTIONS, time_on_hold=timings.time_on_hold,
                                 status_labels=STATUS_LABELS)

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.update(status=services.status(), conflicts=[_describe(services, e) for e in services.my_conflicts()],
                   is_admin=services.is_admin, my_task_count=len(services.tasks().mine),
                   open_flag_count=sum(1 for o in services.open_flags() if o.flag.blocking),
                   has_adapters=bool(services.adapters), unread=services.unread_count(),
                   away=services.away, browser_notifications=browser_notifications,
                   last_notice_id=(services.notices()[:1] or [{"id": 0}])[0]["id"])
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back_to(request_id: str) -> RedirectResponse:
        return RedirectResponse(f"/requests/{request_id}", status_code=303)

    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound):
        return render(request, "error.html", 404, message=f"Request {exc} not found")

    @app.exception_handler(CommandError)
    def command_error(request: Request, exc: CommandError):
        return render(request, "error.html", 413 if isinstance(exc, FileTooLarge) else 409, message=str(exc))

    @app.exception_handler(ReadOnly)
    def read_only(request: Request, exc: ReadOnly):
        return render(request, "error.html", 503, message=str(exc))

    @app.get("/status")
    def status(after: int = 0):
        """Polled by the page to notice other users' changes, read-only mode and new notifications."""
        s = services.status()
        new = services.notices(after_id=after) if after else []
        return {"version": s.version, "read_only": s.read_only, "conflicts": len(services.my_conflicts()),
                "unread": services.unread_count(),
                "notices": [{"id": n["id"], "title": n["title"], "text": n["text"], "url": f"/requests/{n['request_id']}"}
                            for n in reversed(new)]}

    @app.get("/notifications")
    def notifications_page(request: Request):
        return render(request, "notifications.html", items=services.notices())

    @app.post("/notifications/read")
    def mark_all_read(request: Request):
        services.mark_read()
        services.dismiss_away()
        return RedirectResponse(_back(request, "/notifications"), status_code=303)

    @app.post("/notifications/dismiss-away")
    def dismiss_away(request: Request):
        services.dismiss_away()
        return RedirectResponse(_back(request, "/"), status_code=303)

    @app.post("/cache/rebuild")
    def rebuild_cache():
        services.rebuild_cache()
        return RedirectResponse("/", status_code=303)

    @app.post("/conflicts/{event_id}/dismiss")
    def dismiss_conflict(request: Request, event_id: str):
        services.acknowledge_conflict(event_id)
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    @app.get("/tasks")
    def tasks(request: Request):
        return render(request, "tasks.html", tasks=services.tasks())

    @app.post("/requests/{request_id}/owner")
    def change_owner(request_id: str, owner: Annotated[str, Form()]):
        services.change_owner(request_id, owner)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/claim")
    def claim(request: Request, request_id: str, step_id: str):
        services.claim_step(request_id, step_id)
        return RedirectResponse(_back(request, f"/requests/{request_id}"), status_code=303)

    @app.post("/requests/{request_id}/steps/{step_id}/assign")
    def assign(request_id: str, step_id: str, user: Annotated[str, Form()]):
        services.assign_step(request_id, step_id, user)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/release")
    def release(request_id: str, step_id: str):
        services.release_step(request_id, step_id)
        return back_to(request_id)

    @app.get("/")
    def index(request: Request, status: str = "open", workflow: str = "", owner: str = "", q: str = "",
              sort: str = "age", dir: str = "desc"):
        workflows = [(w, w.runnable_by(__version__)) for w in services.registry.latest()]
        rows = services.open_requests(status, workflow, owner, q, sort, dir != "asc")
        filters = {"status": status, "workflow": workflow, "owner": owner, "q": q, "sort": sort, "dir": dir}
        off_process = services.off_process_records(q) if q.strip() else []
        return render(request, "index.html", rows=rows, workflows=workflows, filters=filters, off_process=off_process,
                      people=services.people(), status_filters=list(STATUS_FILTERS))

    @app.get("/flags")
    def flags(request: Request):
        return render(request, "flags.html", items=services.open_flags())

    def amend_page(request: Request, request_id: str, status_code: int = 200, values=None, errors=None,
                   impact=None) -> HTMLResponse:
        state, defn = services.get(request_id)
        capture = engine.initial_forms(defn)
        values, errors = values or {}, errors or {}
        forms = {}
        for sd in capture:
            step_errors = {f"{sd.id}.{k}": v for k, v in (errors.get(sd.id) or {}).items()}
            lists = {n: services.picklist(n) for n in picklist_names(sd.form_schema)}
            forms[sd.id] = build_fields(sd.form_schema or {}, values.get(sd.id, state.step(sd.id).data or {}),
                                        step_errors, f"{sd.id}.", lists)
        titles = {s.id: s.title for s in defn.steps}
        return render(request, "amend.html", status_code, req=state, defn=defn, capture=capture, forms=forms,
                      impact=impact, step_titles=titles, form_errors=errors)

    @app.get("/requests/{request_id}/amend")
    def amend_form(request: Request, request_id: str):
        return amend_page(request, request_id)

    @app.post("/requests/{request_id}/amend")
    async def amend(request: Request, request_id: str):
        _, defn = services.get(request_id)
        posted = await request.form()
        form = {k: posted.getlist(k) for k in posted.keys()}
        action = (form.pop("_action", ["preview"]) or ["preview"])[0]
        data = {sd.id: parse_form(sd.form_schema or {}, form, f"{sd.id}.") for sd in engine.initial_forms(defn)}
        try:
            if action == "apply":
                services.amend(request_id, data)
                return back_to(request_id)
            impact = services.amend_preview(request_id, data)
        except engine.FormInvalid as e:
            return amend_page(request, request_id, 422, values=data, errors={e.step_id: e.errors})
        return amend_page(request, request_id, values=data, impact=impact)

    @app.post("/requests/{request_id}/steps/{step_id}/verify/check")
    def verify_check(request_id: str, step_id: str):
        services.verify_check(request_id, step_id)
        return RedirectResponse(f"/requests/{request_id}#step-{step_id}", status_code=303)

    @app.post("/requests/{request_id}/steps/{step_id}/verify/accept")
    def verify_accept(request_id: str, step_id: str, notes: Annotated[str, Form()] = ""):
        services.accept_verification(request_id, step_id, notes)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/verify/manual")
    def verify_manual(request_id: str, step_id: str, notes: Annotated[str, Form()] = ""):
        services.complete_verification_manually(request_id, step_id, notes)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/outside")
    def done_outside(request_id: str, step_id: str, notes: Annotated[str, Form()] = ""):
        services.complete_outside_tool(request_id, step_id, notes)
        return back_to(request_id)

    @app.get("/off-process")
    def off_process_list(request: Request, q: str = "", reason: str = "", system: str = "", link: str = ""):
        records = services.off_process_records(q, reason, system)
        linked = services._find(link) if link else None
        return render(request, "off_process.html", records=records, reasons=services.reasons,
                      filters={"q": q, "reason": reason, "system": system}, systems=_systems(services),
                      today=timings.now_utc().date().isoformat(), link=(linked.reference or linked.request_id) if linked else "")

    @app.post("/off-process")
    def off_process_create(system: Annotated[str, Form()] = "", what: Annotated[str, Form()] = "",
                           performed_on: Annotated[str, Form()] = "", reason: Annotated[str, Form()] = "",
                           notes: Annotated[str, Form()] = "", linked_request: Annotated[str, Form()] = ""):
        record_id = services.record_off_process(system, what, performed_on, reason, notes, linked_request)
        return RedirectResponse(f"/off-process/{record_id}", status_code=303)

    @app.get("/off-process/{record_id}")
    def off_process_detail(request: Request, record_id: str):
        rec = services.get_off_process(record_id)
        linked = services.rm.get(rec.linked_request) if rec.linked_request else None
        return render(request, "off_process_record.html", rec=rec, linked=linked)

    @app.post("/off-process/{record_id}/notes")
    def off_process_note(record_id: str, text: Annotated[str, Form()] = ""):
        services.add_off_process_note(record_id, text)
        return RedirectResponse(f"/off-process/{record_id}", status_code=303)

    @app.post("/off-process/{record_id}/attachments")
    async def off_process_upload(request: Request, record_id: str):
        declared = int(request.headers.get("content-length") or 0)
        if declared > engine.MAX_ATTACHMENT_BYTES + 1024 * 1024:
            return render(request, "error.html", 413,
                          message=f"That file is too large: the limit is {engine.MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise CommandError("Choose a file to attach")
        try:
            services.add_off_process_attachment(record_id, upload.filename, upload.file)
        finally:
            await upload.close()
        return RedirectResponse(f"/off-process/{record_id}", status_code=303)

    @app.get("/off-process/{record_id}/attachments/{sha256}")
    def off_process_download(record_id: str, sha256: str):
        att, path = services.off_process_attachment_file(record_id, sha256)
        return FileResponse(path, filename=att.name, media_type="application/octet-stream")

    @app.post("/requests/{request_id}/steps/{step_id}/subworkflow/start")
    def start_children(request_id: str, step_id: str):
        services.follow_up(request_id)  # starts any sub-requests that should have been started
        return RedirectResponse(f"/requests/{request_id}#step-{step_id}", status_code=303)

    @app.post("/requests/{request_id}/steps/{step_id}/decision")
    def decide(request_id: str, step_id: str, chosen: Annotated[str, Form()] = ""):
        services.choose_branch(request_id, step_id, chosen)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/approve")
    def approve(request_id: str, step_id: str, notes: Annotated[str, Form()] = ""):
        services.approve(request_id, step_id, notes)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/reject")
    def reject(request_id: str, step_id: str, notes: Annotated[str, Form()] = ""):
        services.reject_approval(request_id, step_id, notes)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/generate")
    def generate(request: Request, request_id: str, step_id: str):
        try:
            services.generate_files(request_id, step_id)
        except ArtefactInvalid as e:
            return show(request, request_id, 422, message="The file couldn't be generated:", problems=e.problems)
        return RedirectResponse(f"/requests/{request_id}#step-{step_id}", status_code=303)

    @app.post("/requests/{request_id}/steps/{step_id}/raise")
    def raise_external(request_id: str, step_id: str, reference: Annotated[str, Form()] = "",
                       link: Annotated[str, Form()] = ""):
        services.raise_external(request_id, step_id, reference, link)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/outcome")
    async def record_outcome(request: Request, request_id: str, step_id: str):
        state, defn = services.get(request_id)
        posted = await request.form()
        form = {k: posted.getlist(k) for k in posted.keys()}
        outcome = (form.get("outcome") or [""])[0]
        sd = defn.step(step_id)
        spec = engine.outcome_spec(sd, outcome)
        captured = None
        cap = engine.capture_spec(spec)
        if cap:
            schema = _capture_schema(defn, *cap)
            captured = parse_form(schema, form, "capture.").get(cap[0])
        services.record_outcome(request_id, step_id, outcome, (form.get("notes") or [""])[0], captured,
                                (form.get("choice") or [None])[0])
        return back_to(request_id)

    @app.post("/picklists/refresh")
    def refresh_picklists(request: Request):
        names = [f"{system}.{op}" for system, a in services.adapters.items() for op in sorted(a.list_operations)]
        services.refresh_picklists(names)
        return RedirectResponse(_back(request, "/"), status_code=303)

    @app.post("/requests/{request_id}/hold")
    def hold(request_id: str, reason: Annotated[str, Form()] = ""):
        services.hold(request_id, reason)
        return back_to(request_id)

    @app.post("/requests/{request_id}/resume")
    def resume_request(request_id: str):
        services.resume(request_id)
        return back_to(request_id)

    @app.post("/requests/{request_id}/cancel")
    def cancel(request_id: str, reason: Annotated[str, Form()] = ""):
        services.cancel(request_id, reason)
        return back_to(request_id)

    @app.post("/requests/{request_id}/flags/resolve")
    def resolve_flag(request: Request, request_id: str, flag: Annotated[str, Form()],
                     resolution: Annotated[str, Form()], notes: Annotated[str, Form()] = "",
                     linked_request: Annotated[str, Form()] = ""):
        services.resolve_flag(request_id, flag, resolution, notes, linked_request or None)
        return RedirectResponse(_back(request, f"/requests/{request_id}#flags"), status_code=303)

    @app.post("/requests/{request_id}/follow-up")
    def follow_up(request_id: str, title: Annotated[str, Form()] = ""):
        return back_to(services.create_follow_up(request_id, title))

    @app.get("/waiting")
    def waiting(request: Request):
        return render(request, "waiting.html", board=services.waiting_board())

    @app.post("/requests/{request_id}/steps/{step_id}/start")
    def start(request_id: str, step_id: str):
        services.start_step(request_id, step_id)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/wait")
    def wait(request_id: str, step_id: str, reason: Annotated[str, Form()], note: Annotated[str, Form()] = ""):
        services.wait_step(request_id, step_id, reason, note)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/resume")
    def resume(request: Request, request_id: str, step_id: str):
        services.resume_step(request_id, step_id)
        return RedirectResponse(_back(request, f"/requests/{request_id}"), status_code=303)

    @app.post("/requests")
    def create(workflow: Annotated[str, Form()], title: Annotated[str, Form()]):
        return back_to(services.create_request(workflow, title))

    def show(request: Request, request_id: str, status_code: int = 200,
             values: dict[str, dict] | None = None, errors: dict[str, dict] | None = None,
             message: str | None = None, problems: list[str] | None = None) -> HTMLResponse:
        """The request page. `values`/`errors` (per form step) re-display a form that failed validation."""
        state, defn = services.get(request_id)
        values, errors = values or {}, errors or {}
        editable = {sd.id for sd in editable_forms(state, defn)}
        forms = {}
        for sd in defn.steps:
            if sd.type != "form":
                continue
            st = state.step(sd.id)
            data = values.get(sd.id, st.draft if st.data is None else st.data)
            step_errors = {f"{sd.id}.{k}": v for k, v in (errors.get(sd.id) or {}).items()}
            lists = {n: services.picklist(n) for n in picklist_names(sd.form_schema)} if sd.id in editable else {}
            forms[sd.id] = build_fields(sd.form_schema or {}, data or {}, step_errors, f"{sd.id}.", lists)
        verify = {sd.id: _verify_view(state, defn, sd.id) for sd in defn.steps if sd.type == "verify"}
        outcome_forms = {sd.id: _capture_fields(state, defn, sd) for sd in defn.steps if sd.type == "outcome"}
        files = {sd.id: engine.step_files(state, sd.id) for sd in defn.steps if sd.artefacts}
        follow_ups = services.follow_ups(request_id)
        linked_off_process = services.off_process_for_request(request_id)
        parent = services.rm.get(state.parent["request"]) if state.parent else None
        children = {c.request_id: c for c in services.children(request_id)}
        original = services.rm.get(state.follow_up_of) if state.follow_up_of else None
        return render(request, "request.html", status_code, req=state, defn=defn, forms=forms, people=services.people(),
                      verify=verify, message=message, problems=problems or [], linked_off_process=linked_off_process,
                      outcome_forms=outcome_forms, step_files=files,
                      parent=parent, children=children,
                      follow_ups=follow_ups, original=original,
                      editable=editable_forms(state, defn), form_errors=errors,
                      history=services.rm.history(request_id))

    @app.get("/requests/{request_id}")
    def detail(request: Request, request_id: str):
        services.mark_read(request_id)  # looking at the request counts as seeing its notifications
        return show(request, request_id)

    @app.post("/requests/{request_id}/forms")
    async def post_forms(request: Request, request_id: str):
        """Save or submit the editable form(s): the initial forms of a Draft, or one ready form step."""
        state, defn = services.get(request_id)
        posted = await request.form()
        form = {k: posted.getlist(k) for k in posted.keys()}
        action = (form.pop("_action", ["save"]) or ["save"])[0]
        only = (form.pop("_step", [None]) or [None])[0]
        steps = [sd for sd in editable_forms(state, defn) if only in (None, sd.id)]
        if not steps:
            raise CommandError("There is no form to fill in on this request right now")
        data = {sd.id: parse_form(sd.form_schema or {}, form, f"{sd.id}.") for sd in steps}
        if action == "prefill":
            step_id = next((sd.id for sd in steps if sd.raw.get("prefill")), None)
            if step_id is None:
                raise CommandError("This form has nothing to fill from a trading system")
            try:
                services.prefill(request_id, step_id, data[step_id])
            except (AdapterUnavailable, NotFoundInSystem) as e:
                services.save_form(request_id, step_id, data[step_id])
                return show(request, request_id, values=data, message=f"Couldn't fill from the trading system: {e}")
            return back_to(request_id)
        try:
            if action == "submit" and state.status == engine.DRAFT:
                services.submit(request_id, data)
            elif action == "submit":
                services.complete_form(request_id, steps[0].id, data[steps[0].id])
            else:
                for step_id, values in data.items():
                    services.save_form(request_id, step_id, values)
        except engine.FormInvalid as e:
            for step_id, values in data.items():  # keep what was typed
                services.save_form(request_id, step_id, values)
            return show(request, request_id, 422, values=data, errors={e.step_id: e.errors})
        return back_to(request_id)

    @app.post("/requests/{request_id}/submit")
    def submit(request_id: str):
        """Submit a Draft whose workflow has no initial form."""
        services.submit(request_id, {})
        return back_to(request_id)

    @app.post("/requests/{request_id}/attachments")
    async def upload(request: Request, request_id: str):
        declared = int(request.headers.get("content-length") or 0)
        if declared > engine.MAX_ATTACHMENT_BYTES + 1024 * 1024:  # refuse before reading the body
            return render(request, "error.html", 413,
                          message=f"That file is too large: the limit is {engine.MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise CommandError("Choose a file to attach")
        step = (form.get("step") or "").strip() or None
        try:
            services.add_attachment(request_id, upload.filename, upload.file, step)
        finally:
            await upload.close()
        return RedirectResponse(f"/requests/{request_id}#attachments", status_code=303)

    @app.get("/requests/{request_id}/attachments/{sha256}")
    def download(request_id: str, sha256: str):
        att, path = services.attachment_file(request_id, sha256)
        return FileResponse(path, filename=att.name, media_type="application/octet-stream")

    @app.post("/requests/{request_id}/notes")
    def add_note(request_id: str, text: Annotated[str, Form()]):
        services.add_note(request_id, text)
        return RedirectResponse(f"/requests/{request_id}#notes", status_code=303)

    @app.post("/requests/{request_id}/steps/{step_id}/checklist/{index}")
    def toggle(request_id: str, step_id: str, index: int, checked: Annotated[bool, Form()]):
        services.set_checklist_item(request_id, step_id, index, checked)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/complete")
    def complete(request_id: str, step_id: str):
        services.complete_step(request_id, step_id)
        return back_to(request_id)

    return app


STATUS_LABELS = {"open": "Open", "draft": "Draft", "active": "Active", "on_hold": "On hold",
                 "cancelling": "Cancelling", "completed": "Completed", "cancelled": "Cancelled", "rejected": "Rejected", "all": "All"}


def _systems(services: Services) -> list[str]:
    """Trading systems to suggest: those WFT knows about, plus any already used in off-process records."""
    from wft.adapters import KNOWN_OPERATIONS
    known = {s.upper() for s in KNOWN_OPERATIONS} | {r.system for r in services.rm.list_off_process()}
    return sorted(known, key=str.lower)


def _filesize(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


def _back(request: Request, default: str) -> str:
    """Return to the page the action came from (e.g. My tasks after a claim); only this app's own pages."""
    path = urlsplit(request.headers.get("referer") or "").path
    return path if path in ("/", "/tasks", "/waiting", "/flags", "/off-process", "/notifications") or path.startswith("/requests/") else default


def _capture_schema(defn, name: str, source: str) -> dict:
    """A one-field form schema for an outcome's captured value, taken from the inputs field it starts from."""
    prop: dict = {"type": "string"}
    for sd in engine.initial_forms(defn):
        node = sd.form_schema or {}
        for part in source.split("."):
            node = (node.get("properties") or {}).get(part, {}) if isinstance(node, dict) else {}
        if node:
            prop = {**node, "title": name.replace("_", " ").capitalize()}
            break
    return {"type": "object", "properties": {name: prop}}


def _capture_fields(state, defn, sd) -> dict:
    """Per outcome that captures values: form fields pre-filled with what was requested."""
    out = {}
    for name, spec in sd.raw.get("outcomes", {}).items():
        cap = engine.capture_spec(spec)
        if cap:
            out[name] = build_fields(_capture_schema(defn, *cap), {cap[0]: state.inputs.get(cap[1])}, None, "capture.")
    return out


def _verify_view(state, defn, step_id: str) -> dict:
    """What a verify step will check, and its latest result, for the page."""
    st = state.step(step_id)
    try:
        operation, key, expected = engine.verify_target(state, defn, step_id)
    except Exception as e:  # a bad expression shouldn't break the page
        operation, key, expected = defn.step(step_id).raw.get("adapter"), None, f"(can't work out: {e})"
    data = st.data or {}
    last = data.get("last_check")
    differing = {d["field"] for d in (last or {}).get("differences") or ()}
    rows = []
    if last and isinstance(last.get("expected"), dict) and isinstance(last.get("actual"), dict):
        rows = [(f, e, a, f in differing) for f, e, a in _flatten_pairs(last["expected"], last["actual"])]
    elif last and last.get("actual") is not None:
        rows = [("(value)", last.get("expected"), last.get("actual"), bool(differing))]
    return {"operation": operation, "key": key, "expected": expected, "last": last, "rows": rows,
            "result": data.get("result"), "notes": data.get("notes")}


def _flatten_pairs(expected: dict, actual: dict, prefix: str = ""):
    for k, v in expected.items():
        a = actual.get(k) if isinstance(actual, dict) else None
        if isinstance(v, dict):
            yield from _flatten_pairs(v, a if isinstance(a, dict) else {}, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v, a


def editable_forms(state, defn) -> list:
    """Form steps the user can fill in now."""
    if state.status == engine.DRAFT:
        return engine.initial_forms(defn)
    if state.status == engine.ACTIVE:
        return [defn.step(s.id) for s in state.steps if s.status in engine.WORKABLE and defn.step(s.id).type == "form"]
    return []


def _describe(services: Services, e: Event) -> dict:
    """A user-facing description of an action that lost to a concurrent one."""
    state = services.rm.get(e.request_id)
    if e.type == engine.CHECKLIST_ITEM_SET:
        what = f"{'ticking' if e.payload['checked'] else 'unticking'} item {e.payload['index'] + 1} of step {e.payload['step']!r}"
    elif e.type == engine.STEP_COMPLETED:
        what = f"completing step {e.payload['step']!r}"
    elif e.type == engine.REQUEST_AMENDED:
        what = "amending the request"
    elif e.type in (engine.REQUEST_HELD, engine.REQUEST_RESUMED, engine.REQUEST_CANCELLED):
        what = {engine.REQUEST_HELD: "putting the request on hold", engine.REQUEST_RESUMED: "resuming the request",
                engine.REQUEST_CANCELLED: "cancelling the request"}[e.type]
    elif e.type == engine.FLAG_RESOLVED:
        what = f"resolving a flag as {engine.RESOLUTIONS[e.payload['resolution']].lower()}"
    elif e.type in (engine.STEP_WAITING, engine.STEP_RESUMED, engine.STEP_STARTED):
        what = f"{e.type.replace('_', ' ').replace('step ', '')} step {e.payload['step']!r}"
    elif e.type == engine.STEP_ASSIGNMENT:
        what = f"claiming step {e.payload['step']!r}"
    elif e.type == engine.FORM_SAVED:
        what = f"saving the {e.payload['step']!r} form"
    elif e.type == engine.REQUEST_SUBMITTED:
        what = "submitting the request"
    else:
        what = e.type.replace("_", " ")
    return {"event_id": e.event_id, "request_id": e.request_id,
            "title": state.title if state else e.request_id, "what": what, "ts": e.ts}
