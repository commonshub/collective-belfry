from __future__ import annotations

import datetime as dt

import pytest
import respx
from fastapi.testclient import TestClient

from belfry_onboarding.app import create_app
from belfry_onboarding.clients.authentik import AuthentikClient
from belfry_onboarding.clients.keycast import KeycastClient
from belfry_onboarding.core import Services
from belfry_onboarding.settings import Settings

from .fakes import FakeAuthentik, FakeKeycast, FakeOdoo

AUTHENTIK_URL = "http://authentik.test"
KEYCAST_URL = "http://keycast.test"
HOOK_TOKEN = "hook-secret"
STRIPE_SECRET = "whsec_test_secret"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ODOO_URL="http://odoo.test",
        ODOO_API_KEY="odoo-key",
        AUTHENTIK_URL=AUTHENTIK_URL,
        AUTHENTIK_TOKEN="ak-token",
        KEYCAST_URL=KEYCAST_URL,
        KEYCAST_ADMIN_TOKEN="kc-admin",
        KEYCAST_SERVICE_TOKEN="kc-service",
        HOOK_TOKEN=HOOK_TOKEN,
        STRIPE_WEBHOOK_SECRET=STRIPE_SECRET,
        EVENT_START=dt.date(2027, 1, 25),
        EVENT_END=dt.date(2027, 1, 31),
        _env_file=None,
    )


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def odoo() -> FakeOdoo:
    return FakeOdoo()


@pytest.fixture
def authentik(router) -> FakeAuthentik:
    return FakeAuthentik(router, AUTHENTIK_URL)


@pytest.fixture
def keycast(router) -> FakeKeycast:
    return FakeKeycast(router, KEYCAST_URL)


@pytest.fixture
def services(settings, odoo, authentik, keycast) -> Services:
    return Services(
        odoo=odoo,
        authentik=AuthentikClient(AUTHENTIK_URL, "ak-token"),
        keycast=KeycastClient(KEYCAST_URL, "kc-admin", "kc-service"),
        settings=settings,
    )


@pytest.fixture
def client(services):
    with TestClient(create_app(services)) as c:
        yield c


def seed_products(odoo: FakeOdoo) -> dict[str, int]:
    return {
        "day": odoo.add(
            "product.product", name="Day pass", belfry_tier="day", belfry_duration_days=1
        ),
        "week": odoo.add(
            "product.product", name="Week pass", belfry_tier="week", belfry_duration_days=7
        ),
        "full": odoo.add(
            "product.product", name="Full access", belfry_tier="full", belfry_duration_days=0
        ),
    }


def seed_paid_invoice(
    odoo: FakeOdoo,
    tier: str = "week",
    partner_id: int | None = None,
    payment_state: str = "paid",
    tx_state: str = "done",
    provider_reference: str | None = "pi_123",
    paid_on: str = "2027-01-20 10:00:00",
    with_transaction: bool = True,
) -> dict[str, int]:
    products = (
        seed_products(odoo)
        if not odoo.tables["product.product"]
        else {p["belfry_tier"]: p["id"] for p in odoo.tables["product.product"].values()}
    )
    if partner_id is None:
        partner_id = odoo.add("res.partner", name="Alice Example", email="alice@example.com")
    invoice_id = odoo.add(
        "account.move",
        name="INV/2027/00001",
        move_type="out_invoice",
        state="posted",
        payment_state=payment_state,
        partner_id=partner_id,
        invoice_date="2027-01-19",
        transaction_ids=[],
    )
    odoo.add("account.move.line", move_id=invoice_id, product_id=products[tier])
    tx_id = None
    if with_transaction:
        tx_id = odoo.add(
            "payment.transaction",
            reference="INV/2027/00001",
            state=tx_state,
            operation="online_direct",
            provider_reference=provider_reference or False,
            last_state_change=paid_on,
            invoice_ids=[invoice_id],
        )
    return {"partner": partner_id, "invoice": invoice_id, "transaction": tx_id, **products}
