"""Trading-system adapters: comparison, the TT REST client, picklists, pre-fill and verify steps."""
import io
import json
import time
from urllib import error

import pytest

from wft import engine
from wft.adapters import AdapterUnavailable, NotFoundInSystem, compare
from wft.adapters.cache import PicklistCache
from wft.adapters.fake import FakeTT
from wft.adapters.tt import TTAdapter, TTSettings
from wft.engine import CommandError

SETTINGS = TTSettings(base_url="https://tt.example", environment="ext_uat_cert", app_key="KEY123",
                      app_secret="SECRET456", app_name="WFT", company="ACME")


# --- comparison ------------------------------------------------------------

def test_compare_only_checks_expected_fields():
    actual = {"max": 1000.0, "currency": " usd ", "other": 1}
    assert compare({"max": 1000, "currency": "USD"}, actual) == []
    assert compare({"max": 2500, "nested": {"x": 1}}, actual) == [
        {"field": "max", "expected": 2500, "actual": 1000.0},
        {"field": "nested", "expected": {"x": 1}, "actual": None}]
    assert compare({"max": 10}, {"max": "10"}) == []
    assert compare("ACC1", "acc1") == [] and compare(True, 1) == [{"field": "(value)", "expected": True, "actual": 1}]


# --- TT REST client with a scripted HTTP opener -----------------------------

class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode())


def http_error(code):
    return error.HTTPError("https://tt.example/x", code, "err", {}, None)


TOKEN = {"status": "Ok", "access_token": "TOKEN789", "seconds_until_expiry": 3600}


def test_token_then_bearer_and_request_ids():
    opener = Opener(TOKEN, {"accounts": [{"id": 2, "name": "Zeta"}, {"id": 1, "name": "alpha"}]})
    tt = TTAdapter(SETTINGS, opener)
    assert [(e.id, e.name) for e in tt.list("accounts")] == [("1", "alpha"), ("2", "Zeta")]
    token_req, list_req = opener.requests
    assert token_req.get_method() == "POST" and "/ttid/ext_uat_cert/token?" in token_req.full_url
    assert token_req.get_header("X-api-key") == "KEY123"
    assert b"grant_type=user_app" in token_req.data and b"KEY123%3ASECRET456" in token_req.data
    assert list_req.get_header("Authorization") == "Bearer TOKEN789"
    assert "/ttaccount/ext_uat_cert/accounts?requestId=WFT-ACME--" in list_req.full_url
    # The token is reused until it nearly expires.
    opener.responses.append({"users": [{"id": "u1", "firstName": "Jane", "lastName": "Doe"}]})
    assert [e.name for e in tt.list("users")] == ["Jane Doe"]
    assert len(opener.requests) == 3


def test_paging():
    opener = Opener(TOKEN, {"accounts": [{"id": 1, "name": "a"}], "nextPageKey": "p2"},
                    {"accounts": [{"id": 2, "name": "b"}], "lastPage": True})
    assert [e.id for e in TTAdapter(SETTINGS, opener).list("accounts")] == ["1", "2"]
    assert "nextPageKey=p2" in opener.requests[2].full_url


@pytest.mark.parametrize("failure, kind, text", [
    (http_error(401), AdapterUnavailable, "check your TT app key and secret"),
    (http_error(500), AdapterUnavailable, "HTTP 500"),
    (error.URLError("timed out"), AdapterUnavailable, "Can't reach TT"),
    (http_error(404), NotFoundInSystem, "TT has no item"),
])
def test_failures_never_leak_secrets(failure, kind, text):
    tt = TTAdapter(SETTINGS, Opener(TOKEN, failure))
    with pytest.raises(kind, match=text) as exc:
        tt.fetch("account_limits", "ACC1")
    message = str(exc.value)
    assert "SECRET456" not in message and "KEY123" not in message and "TOKEN789" not in message
    assert "SECRET456" not in repr(SETTINGS)


def test_refused_token_is_dropped():
    opener = Opener(TOKEN, http_error(401), TOKEN, {"limits": 1})
    tt = TTAdapter(SETTINGS, opener)
    with pytest.raises(AdapterUnavailable):
        tt.fetch("account_limits", "ACC1")
    assert tt.fetch("account_limits", "ACC1").values == {"limits": 1}
    assert sum("/token" in r.full_url for r in opener.requests) == 2


# --- picklist cache ---------------------------------------------------------

def test_picklist_cache_ttl_refresh_and_stale_fallback(tmp_path):
    fake = FakeTT()
    cache = PicklistCache(tmp_path / "p.json", ttl_seconds=60)
    first = cache.get("tt.accounts", lambda: fake.list("accounts"))
    assert [e.id for e in first.items] == ["ACC1", "ACC7", "ACC9"] and not first.stale
    cache.get("tt.accounts", lambda: fake.list("accounts"))
    assert len(fake.calls) == 1  # served from cache
    cache.get("tt.accounts", lambda: fake.list("accounts"), force=True)
    assert len(fake.calls) == 2
    fake.available = False
    stale = cache.get("tt.accounts", lambda: fake.list("accounts"), force=True)
    assert stale.stale and stale.error and [e.id for e in stale.items] == ["ACC1", "ACC7", "ACC9"]
    cache.ttl = 0
    time.sleep(0.01)
    assert cache.get("tt.accounts", lambda: fake.list("accounts")).stale
    assert PicklistCache(tmp_path / "none.json", 60).get("tt.users", lambda: fake.list("users")).items == []


# --- config -----------------------------------------------------------------

def test_tt_config(tmp_path):
    from wft.config import ConfigError, load_config
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_TT_BASE_URL=https://tt\nWFT_TT_ENVIRONMENT=ext_uat_cert\n"
                                      "WFT_TT_APP_KEY=k\nWFT_TT_APP_SECRET=s3cret\nWFT_TT_CACHE_MINUTES=5\n")
    cfg = load_config("uat", home=tmp_path)
    assert cfg.tt.environment == "ext_uat_cert" and cfg.picklist_ttl_minutes == 5 and "s3cret" not in repr(cfg)
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_TT_BASE_URL=https://tt\nWFT_TT_ENVIRONMENT=ext_uat_cert\n")
    with pytest.raises(ConfigError, match="WFT_TT_APP_KEY, WFT_TT_APP_SECRET missing"):
        load_config("uat", home=tmp_path)
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_TT_BASE_URL=fake:\n")
    cfg = load_config("uat", home=tmp_path)
    assert cfg.tt is None and cfg.tt_fake
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_TT_BASE_URL=https://tt\nWFT_TT_ENVIRONMENT=ext_prod_live\n"
                                      "WFT_TT_APP_KEY=k\nWFT_TT_APP_SECRET=s\n")
    with pytest.raises(ConfigError, match="WFT_TT_ENVIRONMENT=ext_prod_live"):
        load_config("uat", home=tmp_path)


def test_secret_values_are_hidden_in_environment_errors():
    from wft.config import ConfigError, check_environment
    rules = {"uat": {"WFT_TT_APP_SECRET": ["^uat"]}, "prod": {}}
    with pytest.raises(ConfigError) as exc:
        check_environment("uat", {"WFT_TT_APP_SECRET": "hunter2"}, rules)
    assert "hunter2" not in str(exc.value) and "<hidden>" in str(exc.value)


# --- services: picklists, prefill, verify -------------------------------------

def active(svc, max_=1000, account="ACC1"):
    rid = svc.create_request("tt_limits@1", "x")
    svc.submit(rid, {"capture": {"account": account, "limits": {"max": max_, "currency": "USD"}}})
    svc.set_checklist_item(rid, "apply", 0, True)
    svc.complete_step(rid, "apply")
    return rid


def test_picklists_fall_back_when_tt_unavailable_or_unconfigured(tt_services):
    assert [e.id for e in tt_services.picklist("tt.accounts").items] == ["ACC1", "ACC7", "ACC9"]
    tt_services.adapters["tt"].available = False
    stale = tt_services.picklist("tt.accounts", force=True)
    assert stale.stale and stale.items  # last saved list still offered
    tt_services.adapters = {}
    p = tt_services.picklist("tt.users")
    assert p.items == [] and "isn't configured" in p.error


def test_prefill_fills_current_values(tt_services):
    rid = tt_services.create_request("tt_limits@1", "x")
    filled = tt_services.prefill(rid, "capture", {"account": "ACC7"})
    assert filled["current"] == {"max": 2500, "currency": "USD"}
    assert tt_services.get(rid)[0].step("capture").draft == filled
    with pytest.raises(CommandError, match="Fill in account first"):
        tt_services.prefill(rid, "capture", {})
    with pytest.raises(NotFoundInSystem):
        tt_services.prefill(rid, "capture", {"account": "NOPE"})


def test_verify_match_then_accept(tt_services):
    rid = active(tt_services)
    with pytest.raises(CommandError, match="Check it"):
        tt_services.accept_verification(rid, "check")
    with pytest.raises(CommandError, match="check it, then accept"):
        tt_services.complete_step(rid, "check")
    check = tt_services.verify_check(rid, "check")
    assert check["outcome"] == "match" and check["key"] == "ACC1" and check["source"] == "fake TT"
    state = tt_services.get(rid)[0]
    assert state.step("check").status == engine.IN_PROGRESS
    assert state.step("check").data["last_check"]["checked_by"] == "alice"
    tt_services.accept_verification(rid, "check")
    state = tt_services.get(rid)[0]
    assert state.status == engine.COMPLETED and state.step("check").data["result"] == "accepted"


def test_verify_mismatch_needs_a_reason_or_an_issue(tt_services):
    rid = active(tt_services, max_=2500)
    check = tt_services.verify_check(rid, "check")
    assert check["outcome"] == "mismatch"
    assert check["differences"] == [{"field": "max", "expected": 2500, "actual": 1000}]
    with pytest.raises(CommandError, match="Say why"):
        tt_services.accept_verification(rid, "check")
    tt_services.wait_step(rid, "check", "trading_system", "TT still shows 1000")  # record an issue
    assert tt_services.get(rid)[0].step("check").wait_reason == "trading_system"
    tt_services.resume_step(rid, "check")
    tt_services.adapters["tt"].data["accounts"]["ACC1"]["limits"]["max"] = 2500  # fixed in TT
    assert tt_services.verify_check(rid, "check")["outcome"] == "match"
    tt_services.accept_verification(rid, "check")
    assert tt_services.get(rid)[0].status == engine.COMPLETED


def test_verify_when_tt_is_down_or_item_missing(tt_services):
    rid = active(tt_services)
    tt_services.adapters["tt"].available = False
    check = tt_services.verify_check(rid, "check")
    assert check["outcome"] == "unavailable" and "unavailable" in check["error"]
    with pytest.raises(CommandError, match="complete it manually"):
        tt_services.accept_verification(rid, "check")
    with pytest.raises(CommandError, match="Say how"):
        tt_services.complete_verification_manually(rid, "check", " ")
    tt_services.complete_verification_manually(rid, "check", "Checked on the TT Setup screen")
    data = tt_services.get(rid)[0].step("check").data
    assert data["result"] == "manual" and data["last_check"]["outcome"] == "unavailable"
    other = active(tt_services, account="GHOST")
    tt_services.adapters["tt"].available = True
    check = tt_services.verify_check(other, "check")
    assert check["outcome"] == "mismatch" and check["differences"][0]["actual"] == "not found"


def test_validation_of_operations_and_picklists(tmp_path):
    import copy

    from tests.defs import TT_FILES, TT_FLOW, write_defs
    from wft.validation import validate_repo
    assert validate_repo(write_defs(tmp_path / "ok", TT_FLOW, files=TT_FILES)).ok
    d = copy.deepcopy(TT_FLOW)
    d["steps"][2]["adapter"] = "tt.nonsense"
    d["steps"][0]["prefill"]["operation"] = "kx.limits"
    files = copy.deepcopy(TT_FILES)
    files["schemas/tt_capture.schema.json"]["properties"]["account"]["x-picklist"] = "tt.traders"
    errs = [i.message for i in validate_repo(write_defs(tmp_path / "bad", d, files=files)).errors]
    assert any("unknown adapter operation 'tt.nonsense'" in e for e in errs)
    assert any("unknown prefill operation 'kx.limits'" in e for e in errs)
    assert any("unknown picklist 'tt.traders'" in e for e in errs)
    d = copy.deepcopy(TT_FLOW)
    del d["steps"][2]["key"]
    assert any("'key' is a required property" in i.message
               for i in validate_repo(write_defs(tmp_path / "nokey", d, files=TT_FILES)).errors)
