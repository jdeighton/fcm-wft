"""FastAPI web UI, served on localhost to the user's own browser."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wft.engine import CommandError
from wft.services import NotFound, Services

HERE = Path(__file__).parent


def create_app(services: Services, env: str) -> FastAPI:
    app = FastAPI(title="WFT", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(env=env, user=services.user)

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back_to(request_id: str) -> RedirectResponse:
        return RedirectResponse(f"/requests/{request_id}", status_code=303)

    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound):
        return render(request, "error.html", 404, message=f"Request {exc} not found")

    @app.exception_handler(CommandError)
    def command_error(request: Request, exc: CommandError):
        return render(request, "error.html", 409, message=str(exc))

    @app.get("/")
    def index(request: Request):
        services.sync()
        return render(request, "index.html",
                      requests=services.rm.list_requests(), workflows=services.registry.latest())

    @app.post("/requests")
    def create(workflow: Annotated[str, Form()], title: Annotated[str, Form()]):
        return back_to(services.create_request(workflow, title))

    @app.get("/requests/{request_id}")
    def detail(request: Request, request_id: str):
        services.sync()
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
