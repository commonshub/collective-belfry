"""Environment configuration. Names match the contract in docs/slice-1-onboarding.md."""

from __future__ import annotations

import datetime as dt

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    odoo_url: str = Field(default="http://odoo:8069", alias="ODOO_URL")
    odoo_db: str = Field(default="odoo", alias="ODOO_DB")
    odoo_user: str = Field(default="onboarding@belfry.local", alias="ODOO_USER")
    odoo_api_key: str = Field(alias="ODOO_API_KEY")

    authentik_url: str = Field(default="http://authentik-server:9000", alias="AUTHENTIK_URL")
    authentik_token: str = Field(alias="AUTHENTIK_TOKEN")
    authentik_source_slug: str = Field(default="pass-bridge", alias="AUTHENTIK_SOURCE_SLUG")
    authentik_user_path: str = Field(default="belfry/participants", alias="AUTHENTIK_USER_PATH")

    keycast_url: str = Field(default="http://keycast:3000", alias="KEYCAST_URL")
    keycast_admin_token: str = Field(alias="KEYCAST_ADMIN_TOKEN")
    keycast_service_token: str = Field(alias="KEYCAST_SERVICE_TOKEN")
    # Sent as the Host header to Keycast (tenant = Host); must be in ALLOWED_TENANT_DOMAINS.
    keycast_tenant_domain: str | None = Field(default=None, alias="KEYCAST_TENANT_DOMAIN")

    hook_token: str = Field(alias="HOOK_TOKEN")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")

    event_start: dt.date = Field(default=dt.date(2027, 1, 25), alias="EVENT_START")
    event_end: dt.date = Field(default=dt.date(2027, 1, 31), alias="EVENT_END")

    # Not in the contract: when true, a posted invoice with payment_state paid/in_payment but no
    # payment.transaction at all (a payment registered by hand in Odoo) is accepted. Off by
    # default so that the contract's "done transaction with provider_reference" rule applies.
    allow_manual_payments: bool = Field(default=False, alias="ALLOW_MANUAL_PAYMENTS")

    log_level: str = Field(default="info", alias="LOG_LEVEL")
    reconcile_days: int = Field(default=30, alias="RECONCILE_DAYS")
