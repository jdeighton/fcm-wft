"""FastAPI web UI, served on localhost to the user's own browser."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wft import __version__, engine
from wft.engine import CommandError
from wft.events import Event
from wft.services import NotFound, ReadOnly, Services
from wft.web import security
from wft.web.forms import build_fields, parse_form

HERE = Path(__file__).parent


def create_app(services: Services, env: str, token: str, port: int) -> FastAPI:
    app = FastAPI(title="WFT", docs_url=None, redoc_url=None, openapi_url=None)
    security.install(app, token, port)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(env=env, user=services.user)
    templates.env.filters["person"] = lambda u: services.team.name(u) if u else "—"

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.update(status=services.status(), conflicts=[_describe(services, e) for e in services.my_conflicts()],
                   is_admin=services.is_admin, my_task_count=len(services.tasks().mine))
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back_to(request_id: str) -> RedirectResponse:
        return RedirectResponse(f"/requests/{request_id}", status_code=303)

    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound):
        return render(request, "error.html", 404, message=f"Request {exc} not found")

    @app.exception_handler(CommandError)
    def command_error(request: Request, exc: CommandError):
        return render(request, "error.html", 409, message=str(exc))

    @app.exception_handler(ReadOnly)
    def read_only(request: Request, exc: ReadOnly):
        return render(request, "error.html", 503, message=str(exc))

    @app.get("/status")
    def status():
        """Polled by the page to notice other users' changes and read-only mode."""
        s = services.status()
        return {"version": s.version, "read_only": s.read_only, "conflicts": len(services.my_conflicts())}

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
    def index(request: Request):
        workflows = [(w, w.runnable_by(__version__)) for w in services.registry.latest()]
        return render(request, "index.html", requests=services.rm.list_requests(), workflows=workflows)

    @app.post("/requests")
    def create(workflow: Annotated[str, Form()], title: Annotated[str, Form()]):
        return back_to(services.create_request(workflow, title))

    def show(request: Request, request_id: str, status_code: int = 200,
             values: dict[str, dict] | None = None, errors: dict[str, dict] | None = None) -> HTMLResponse:
        """The request page. `values`/`errors` (per form step) re-display a form that failed validation."""
        state, defn = services.get(request_id)
        values, errors = values or {}, errors or {}
        forms = {}
        for sd in defn.steps:
            if sd.type != "form":
                continue
            st = state.step(sd.id)
            data = values.get(sd.id, st.draft if st.data is None else st.data)
            step_errors = {f"{sd.id}.{k}": v for k, v in (errors.get(sd.id) or {}).items()}
            forms[sd.id] = build_fields(sd.form_schema or {}, data or {}, step_errors, f"{sd.id}.")
        return render(request, "request.html", status_code, req=state, defn=defn, forms=forms, people=services.people(),
                      editable=editable_forms(state, defn), form_errors=errors,
                      history=services.rm.history(request_id))

    @app.get("/requests/{request_id}")
    def detail(request: Request, request_id: str):
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


def _back(request: Request, default: str) -> str:
    """Return to the page the action came from (e.g. My tasks after a claim); only this app's own pages."""
    path = urlsplit(request.headers.get("referer") or "").path
    return path if path == "/" or path == "/tasks" or path.startswith("/requests/") else default


def editable_forms(state, defn) -> list:
    """Form steps the user can fill in now."""
    if state.status == engine.DRAFT:
        return engine.initial_forms(defn)
    if state.status == engine.ACTIVE:
        return [defn.step(s.id) for s in state.steps if s.status == engine.READY and defn.step(s.id).type == "form"]
    return []


def _describe(services: Services, e: Event) -> dict:
    """A user-facing description of an action that lost to a concurrent one."""
    state = services.rm.get(e.request_id)
    if e.type == engine.CHECKLIST_ITEM_SET:
        what = f"{'ticking' if e.payload['checked'] else 'unticking'} item {e.payload['index'] + 1} of step {e.payload['step']!r}"
    elif e.type == engine.STEP_COMPLETED:
        what = f"completing step {e.payload['step']!r}"
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
