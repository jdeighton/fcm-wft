"""TT (Trading Technologies) adapter over the TT REST API 2.0. Read-only.

Authentication follows TT's published pattern: every call carries the application key in the
`x-api-key` header and a `requestId` query parameter; most calls also need a bearer token, obtained
from the TT ID service with the key and secret.

>>> CHECK BEFORE GO-LIVE <<<
TT's documentation could not be fetched while writing this, so the token request details and the
endpoint paths and response shapes in `ENDPOINTS` / `_list_key` / `_name` are best understanding,
not confirmed. Compare them with the firm's existing TT API code, adjust here (everything
TT-specific is in this file), and run `python scripts/tt_smoke.py --env uat` against TT UAT.

Secrets: the key and secret come only from the user's env file. They are never logged, never put in
exception messages, never written to the journal or the share.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request

from wft.adapters import KNOWN_OPERATIONS, AdapterUnavailable, Entity, Fetched, NotFoundInSystem

log = logging.getLogger(__name__)

# operation -> (method, path). `{env}` is the TT environment (e.g. ext_uat_cert), `{key}` the item id.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "token": ("POST", "/ttid/{env}/token"),
    "accounts": ("GET", "/ttaccount/{env}/accounts"),
    "account": ("GET", "/ttaccount/{env}/account/{key}"),
    "account_limits": ("GET", "/ttaccount/{env}/account/{key}/riskSettings"),  # unconfirmed path
    "users": ("GET", "/ttuser/{env}/users"),
    "user": ("GET", "/ttuser/{env}/user/{key}"),
}
TOKEN_SAFETY_SECONDS = 60
MAX_PAGES = 50


@dataclass(frozen=True)
class TTSettings:
    base_url: str          # e.g. https://ttrestapi.trade.tt
    environment: str       # e.g. ext_uat_cert / ext_prod_live
    app_key: str
    app_secret: str = field(repr=False)
    app_name: str = "WFT"
    company: str = "FIRM"
    timeout: float = 15.0

    @property
    def label(self) -> str:
        return f"TT {self.environment}"


class TTAdapter:
    system = "tt"
    list_operations = KNOWN_OPERATIONS["tt"]["list"]
    fetch_operations = KNOWN_OPERATIONS["tt"]["fetch"]

    def __init__(self, settings: TTSettings, opener=None):
        self.s = settings
        self._open = opener or request.urlopen
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    # --- operations ---------------------------------------------------------

    def list(self, operation: str) -> list[Entity]:
        if operation not in self.list_operations:
            raise ValueError(f"Unknown TT list operation {operation!r}")
        items: list[dict] = []
        page_key = None
        for _ in range(MAX_PAGES):
            data = self._get(operation, query={"nextPageKey": page_key} if page_key else None)
            items += data.get(_list_key(operation)) or []
            page_key = data.get("nextPageKey")
            if not page_key or data.get("lastPage") is True:
                break
        return sorted((Entity(str(i.get("id")), _name(i), _extra(i)) for i in items if i.get("id") is not None),
                      key=lambda e: e.name.lower())

    def fetch(self, operation: str, key: Any) -> Fetched:
        if operation not in self.fetch_operations:
            raise ValueError(f"Unknown TT fetch operation {operation!r}")
        data = self._get(operation, key=str(key))
        singular = {"account": "account", "user": "user"}.get(operation)
        if singular and isinstance(data.get(singular), list):
            data = data[singular][0] if data[singular] else {}
        return Fetched(data, source=f"{self.s.label} REST API")

    # --- HTTP ---------------------------------------------------------------

    def _get(self, operation: str, key: str | None = None, query: dict | None = None) -> dict:
        method, path = ENDPOINTS[operation]
        url_path = path.format(env=self.s.environment, key=parse.quote(key or "", safe=""))
        return self._call(method, url_path, query, auth=True, not_found_ok=key is not None)

    def _call(self, method: str, path: str, query: dict | None, *, auth: bool, body: bytes | None = None,
              content_type: str | None = None, not_found_ok: bool = False) -> dict:
        params = {"requestId": f"{self.s.app_name}-{self.s.company}--{uuid.uuid4()}", **(query or {})}
        url = f"{self.s.base_url.rstrip('/')}{path}?{parse.urlencode(params)}"
        headers = {"x-api-key": self.s.app_key, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if auth:
            headers["Authorization"] = f"Bearer {self._bearer()}"
        req = request.Request(url, data=body, headers=headers, method=method)
        what = f"{method} {path}"  # never include headers, query secrets or bodies in messages
        try:
            with self._open(req, timeout=self.s.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except error.HTTPError as e:
            if e.code == 404 and not_found_ok:
                raise NotFoundInSystem(f"TT has no item for {what}") from None
            if e.code in (401, 403):
                self._token = None  # try a fresh token next time
                raise AdapterUnavailable(f"TT refused {what} (HTTP {e.code}): check your TT app key and secret") from None
            raise AdapterUnavailable(f"TT error for {what} (HTTP {e.code})") from None
        except (error.URLError, TimeoutError, OSError) as e:
            raise AdapterUnavailable(f"Can't reach TT for {what}: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise AdapterUnavailable(f"TT sent an unreadable response for {what}") from None

    def _bearer(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires:
                return self._token
            method, path = ENDPOINTS["token"]
            body = parse.urlencode({"grant_type": "user_app",
                                    "app_key": f"{self.s.app_key}:{self.s.app_secret}"}).encode()
            data = self._call(method, path.format(env=self.s.environment), None, auth=False, body=body,
                              content_type="application/x-www-form-urlencoded")
            token = data.get("access_token")
            if not token:
                raise AdapterUnavailable("TT didn't return a token: check your TT app key and secret")
            self._token = token
            self._token_expires = time.time() + float(data.get("seconds_until_expiry", 1800)) - TOKEN_SAFETY_SECONDS
            log.info("Obtained a TT token for %s", self.s.environment)
            return token


def _list_key(operation: str) -> str:
    return {"accounts": "accounts", "users": "users"}[operation]


def _name(item: dict) -> str:
    if item.get("name"):
        return str(item["name"])
    person = " ".join(str(item[k]) for k in ("firstName", "lastName") if item.get(k))
    return person or str(item.get("alias") or item.get("email") or item.get("id"))


def _extra(item: dict) -> dict:
    return {k: v for k, v in item.items() if k in ("email", "alias", "parentId", "companyId") and v is not None}
