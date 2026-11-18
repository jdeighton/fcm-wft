"""Protects the localhost UI from other users on the same host and from other websites.

- Session token: generated per launch; the launcher opens `/auth?token=...`, which sets
  it as a cookie and redirects. Every other request must carry the cookie.
- Host header: must be this instance's own `127.0.0.1:<port>` or `localhost:<port>`
  (blocks DNS rebinding).
- CSRF: the cookie is SameSite=Strict, and state-changing requests must carry an
  Origin (or Referer) from this instance's own origin.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
AUTH_PATH = "/auth"

NOT_AUTHORISED = """<!doctype html><meta charset="utf-8"><title>WFT</title>
<body style="font-family:system-ui,sans-serif;max-width:40em;margin:3em auto">
<h1>This browser window isn't signed in to WFT</h1>
<p>Open WFT using the link printed in the WFT console window, or restart WFT.</p></body>"""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_name(port: int) -> str:
    # Cookies are shared across ports on the same host, so keep each instance's separate.
    return f"wft_session_{port}"


def install(app: FastAPI, token: str, port: int) -> None:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    name = cookie_name(port)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return PlainTextResponse("Invalid Host header", status_code=400)

        if request.url.path == AUTH_PATH:
            if not secrets.compare_digest(request.query_params.get("token", ""), token):
                return HTMLResponse(NOT_AUTHORISED, status_code=403)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(name, token, httponly=True, samesite="strict", path="/")
            return response

        if not secrets.compare_digest(request.cookies.get(name, ""), token):
            return HTMLResponse(NOT_AUTHORISED, status_code=403)

        if request.method not in SAFE_METHODS and _origin(request) not in allowed_origins:
            return PlainTextResponse("Cross-site request blocked", status_code=403)

        return await call_next(request)


def _origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin.lower()
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}".lower()
    return None
