import pytest
from werkzeug.datastructures import MultiDict

from amc_bot.web import create_app


class Coordinator:
    def __init__(self):
        self.payload = None

    async def start(self, payload):
        self.payload = payload


def form_data(**overrides):
    data = {
        "email": "person@example.com",
        "password": "super-secret",
        "payment_cvv": "123",
        "primary_movie": "A Movie",
        "aliases": "A Movie Alt\nAnother Title",
        "location": "10001",
        "radius_miles": "25",
        "ticket_count": "2",
        "availability": ["0:18", "6:20"],
        "dry_run": "on",
        "form_token": "test-token",
    }
    data.update(overrides)
    return MultiDict(data)


@pytest.fixture
def app():
    value = create_app(Coordinator())
    value.config["FORM_TOKEN"] = "test-token"
    return value


@pytest.mark.asyncio
async def test_home_page_has_accessible_form(app):
    client = app.test_client()
    response = await client.get("/")
    body = await response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="password"' in body
    assert 'type="password"' in body
    assert 'name="availability"' in body
    assert 'value="0:18"' in body
    assert "PURCHASE" not in body


@pytest.mark.asyncio
async def test_start_validates_required_values(app):
    response = await app.test_client().post(
        "/start", form={"email": "bad", "form_token": "test-token"}
    )
    body = await response.get_data(as_text=True)
    assert response.status_code == 400
    assert "valid AMC email" in body
    assert "AMC password" in body


@pytest.mark.asyncio
async def test_start_passes_config_and_does_not_render_credentials():
    coordinator = Coordinator()
    app = create_app(coordinator)
    app.config["FORM_TOKEN"] = "test-token"
    response = await app.test_client().post("/start", form=form_data())
    body = await response.get_data(as_text=True)
    assert response.status_code == 200
    assert coordinator.payload["password"] == "super-secret"
    assert coordinator.payload["payment_cvv"] == "123"
    assert coordinator.payload["availability"] == ["0:18"]
    assert "super-secret" not in body
    assert ">123<" not in body
    status = await app.test_client().get("/status")
    status_body = await status.get_data(as_text=True)
    assert "super-secret" not in status_body
    assert "person@example.com" not in status_body
    assert "123" not in status_body


@pytest.mark.asyncio
async def test_cvv_is_validated_and_never_echoed():
    coordinator = Coordinator()
    app = create_app(coordinator)
    app.config["FORM_TOKEN"] = "test-token"
    response = await app.test_client().post("/start", form=form_data(payment_cvv="12x"))
    body = await response.get_data(as_text=True)
    assert response.status_code == 400
    assert "CVV must be 3 or 4 digits" in body
    assert "12x" not in body


@pytest.mark.asyncio
async def test_live_purchase_requires_exact_acknowledgement():
    coordinator = Coordinator()
    app = create_app(coordinator, live_purchases_allowed=True)
    app.config["FORM_TOKEN"] = "test-token"
    data = form_data(dry_run=None, live_purchase_acknowledgement="buy")
    response = await app.test_client().post("/start", form=data)
    assert response.status_code == 400
    assert coordinator.payload is None

    data["live_purchase_acknowledgement"] = "PURCHASE"
    response = await app.test_client().post("/start", form=data)
    assert response.status_code == 200
    assert coordinator.payload["dry_run"] is False
    assert coordinator.payload["live_purchase_acknowledgement"] == "PURCHASE"


@pytest.mark.asyncio
async def test_start_rejects_cross_site_form_without_token(app):
    response = await app.test_client().post("/start", form=form_data(form_token="wrong"))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_dns_rebinding_host_is_rejected(app):
    response = await app.test_client().get("/", headers={"Host": "attacker.example"})
    assert response.status_code == 400
