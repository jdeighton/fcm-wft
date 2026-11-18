"""FastAPI web UI, served on localhost to the user's own browser."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wft import engine
from wft.engine import CommandError
from wft.events import Event
from wft.services import NotFound, ReadOnly, Services
from wft.web import security

HERE = Path(__file__).parent


def create_app(services: Services, env: str, token: str, port: int) -> FastAPI:
    app = FastAPI(title="WFT", docs_url=None, redoc_url=None, openapi_url=None)
    security.install(app, token, port)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(env=env, user=services.user)

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.update(status=services.status(), conflicts=[_describe(services, e) for e in services.my_conflicts()])
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

    @app.get("/")
    def index(request: Request):
        return render(request, "index.html",
                      requests=services.rm.list_requests(), workflows=services.registry.latest())

    @app.post("/requests")
    def create(workflow: Annotated[str, Form()], title: Annotated[str, Form()]):
        return back_to(services.create_request(workflow, title))

    @app.get("/requests/{request_id}")
    def detail(request: Request, request_id: str):
        state, defn = services.get(request_id)
        return render(request, "request.html", req=state, defn=defn,
                      history=services.rm.history(request_id))

    @app.post("/requests/{request_id}/steps/{step_id}/checklist/{index}")
    def toggle(request_id: str, step_id: str, index: int, checked: Annotated[bool, Form()]):
        services.set_checklist_item(request_id, step_id, index, checked)
        return back_to(request_id)

    @app.post("/requests/{request_id}/steps/{step_id}/complete")
    def complete(request_id: str, step_id: str):
        services.complete_step(request_id, step_id)
        return back_to(request_id)

    return app


def _describe(services: Services, e: Event) -> dict:
    """A user-facing description of an action that lost to a concurrent one."""
    state = services.rm.get(e.request_id)
    if e.type == engine.CHECKLIST_ITEM_SET:
        what = f"{'ticking' if e.payload['checked'] else 'unticking'} item {e.payload['index'] + 1} of step {e.payload['step']!r}"
    elif e.type == engine.STEP_COMPLETED:
        what = f"completing step {e.payload['step']!r}"
    else:
        what = e.type.replace("_", " ")
    return {"event_id": e.event_id, "request_id": e.request_id,
            "title": state.title if state else e.request_id, "what": what, "ts": e.ts}
