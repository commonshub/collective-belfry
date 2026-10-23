from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from belfry_onboarding.app import create_app
from belfry_onboarding.clients.authentik import AuthentikClient
from belfry_onboarding.clients.keycast import KeycastClient
from belfry_onboarding.core import Services
from belfry_onboarding.relay_whitelist import RelayWhitelist
from belfry_onboarding.settings import Settings

from .fakes import FakeAuthentik, FakeIssuer, FakeKeycast, FakeOdoo, FakeRelay, FakeVerifier

AUTHENTIK_URL = "http://authentik.test"
KEYCAST_URL = "http://keycast.test"
ISSUER_URL = "http://issuer.test"
VERIFIER_URL = "http://verifier.test"
BRIDGE_SECRET = "bridge-secret"
HOOK_TOKEN = "hook-secret"
STRIPE_SECRET = "whsec_test_secret"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ODOO_URL="http://odoo.test",
        ODOO_API_KEY="odoo-key",
        AUTHENTIK_URL=AUTHENTIK_URL,
        AUTHENTIK_TOKEN="ak-token",
        AUTHENTIK_PUBLIC_URL="https://login.belfry.test",
        KEYCAST_URL=KEYCAST_URL,
        KEYCAST_ADMIN_TOKEN="kc-admin",
        KEYCAST_SERVICE_TOKEN="kc-service",
        HOOK_TOKEN=HOOK_TOKEN,
        STRIPE_WEBHOOK_SECRET=STRIPE_SECRET,
        EVENT_START=dt.date(2027, 1, 25),
        EVENT_END=dt.date(2027, 1, 31),
        ISSUER_URL=ISSUER_URL,
        ISSUER_PUBLIC_URL="http://localhost:7005",
        VERIFIER_URL=VERIFIER_URL,
        PUBLIC_URL="https://belfry.test",
        STATUS_LIST_URL="http://onboarding:8100/status/1",
        BRIDGE_CLIENT_SECRET=BRIDGE_SECRET,
        _env_file=None,
    )


def make_jwk() -> dict[str, str]:
    """A fresh P-256 private JWK with kid (the shape gen-issuer-pki.sh writes)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.private_numbers()
    pub = nums.public_numbers

    def b(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b(pub.x),
        "y": b(pub.y),
        "d": b(nums.private_value),
        "kid": "test-key",
    }


@pytest.fixture(scope="session")
def issuer_jwk() -> dict[str, str]:
    return make_jwk()


@pytest.fixture(scope="session")
def bridge_jwk() -> dict[str, str]:
    return make_jwk()


@pytest.fixture
def issuer(router) -> FakeIssuer:
    return FakeIssuer(router, ISSUER_URL)


@pytest.fixture
def verifier(router) -> FakeVerifier:
    return FakeVerifier(router, VERIFIER_URL)


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
def relay() -> FakeRelay:
    return FakeRelay(require_auth=True)


TEMPLATE = Path(__file__).resolve().parents[3] / "infra" / "relay" / "rnostr.toml"


@pytest.fixture
def whitelist(tmp_path) -> RelayWhitelist:
    """A real RelayWhitelist rendering the repo's rnostr template into a temp file."""
    return RelayWhitelist(str(TEMPLATE), str(tmp_path / "rnostr.toml"))


@pytest.fixture
def services(
    settings, odoo, authentik, keycast, relay, whitelist, issuer, verifier, issuer_jwk, bridge_jwk
) -> Services:
    from belfry_onboarding.clients.issuer import IssuerClient
    from belfry_onboarding.clients.verifier import VerifierClient

    return Services(
        odoo=odoo,
        authentik=AuthentikClient(AUTHENTIK_URL, "ak-token"),
        keycast=KeycastClient(KEYCAST_URL, "kc-admin", "kc-service"),
        settings=settings,
        relay=relay,  # type: ignore[arg-type]
        whitelist=whitelist,
        issuer=IssuerClient(ISSUER_URL),
        verifier=VerifierClient(VERIFIER_URL),
        issuer_jwk=issuer_jwk,
        issuer_x5c=["MIIBfake"],
        bridge_jwk=bridge_jwk,
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
