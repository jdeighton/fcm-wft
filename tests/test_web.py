from fastapi.testclient import TestClient

from tests.conftest import DEMO
from wft.web.app import create_app


def test_create_tick_and_complete_through_ui(services):
    client = TestClient(create_app(services, "uat"))
    page = client.get("/")
    assert page.status_code == 200 and "UAT environment" in page.text and "Demo manual checklist" in page.text

    r = client.post("/requests", data={"workflow": DEMO, "title": "Add user jdoe"}, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    rid = url.rsplit("/", 1)[1]

    assert client.post(f"/requests/{rid}/steps/do_the_thing/complete").status_code == 409

    for i in range(3):
        client.post(f"/requests/{rid}/steps/do_the_thing/checklist/{i}", data={"checked": "true"})
    detail = client.post(f"/requests/{rid}/steps/do_the_thing/complete")
    assert detail.status_code == 200 and "Completed" in detail.text

    listing = client.get("/").text
    assert "Add user jdoe" in listing and "status-completed" in listing


def test_unknown_request_is_404(services):
    client = TestClient(create_app(services, "uat"))
    assert client.get("/requests/nope").status_code == 404
