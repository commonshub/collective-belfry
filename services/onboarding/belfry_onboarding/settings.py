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
    # Authentik as the participant's browser reaches it. The sign-in links authentik
    # returns carry the host the service called, so they are rewritten to this origin.
    authentik_public_url: str = Field(default="http://localhost:9000", alias="AUTHENTIK_PUBLIC_URL")
    authentik_token: str = Field(alias="AUTHENTIK_TOKEN")
    authentik_source_slug: str = Field(default="pass-bridge", alias="AUTHENTIK_SOURCE_SLUG")
    authentik_user_path: str = Field(default="belfry/participants", alias="AUTHENTIK_USER_PATH")
    # Members of this group may use the Keycast OIDC application (blueprint
    # belfry-keycast-provider); derived from active passes like the relay whitelist.
    authentik_participants_group: str = Field(
        default="belfry-participants", alias="AUTHENTIK_PARTICIPANTS_GROUP"
    )

    keycast_url: str = Field(default="http://keycast:3000", alias="KEYCAST_URL")
    keycast_admin_token: str = Field(alias="KEYCAST_ADMIN_TOKEN")
    keycast_service_token: str = Field(alias="KEYCAST_SERVICE_TOKEN")
    # Sent as the Host header to Keycast (tenant = Host); must be in ALLOWED_TENANT_DOMAINS.
    keycast_tenant_domain: str | None = Field(default=None, alias="KEYCAST_TENANT_DOMAIN")

    # Community relay (ws:// or wss://). Empty: no kind-0 profile is published at provisioning.
    relay_url: str = Field(default="", alias="RELAY_URL")
    # Domain of the NIP-05 names (member number @ domain); Keycast serves nostr.json for it.
    nip05_domain: str = Field(default="belfry.local", alias="NIP05_DOMAIN")
    # Relay whitelist (Phase 2): the rnostr config is rendered from RELAY_CONFIG_TEMPLATE into
    # RELAY_CONFIG_PATH with the pubkeys of all active passes. Empty path: no whitelist sync.
    relay_config_path: str = Field(default="", alias="RELAY_CONFIG_PATH")
    relay_config_template: str = Field(
        default="/relay-template/rnostr.toml", alias="RELAY_CONFIG_TEMPLATE"
    )
    # Comma-separated hex pubkeys always on the whitelist (Belfry's own keys, stewards).
    relay_extra_pubkeys: str = Field(default="", alias="RELAY_EXTRA_PUBKEYS")

    # Validity of the one-time sign-in link mailed at activation (authentik flow token), in days.
    login_link_days: int = Field(default=7, alias="LOGIN_LINK_DAYS")

    # Phase 3: pass certificates. Empty ISSUER_URL disables claim offers and the status list.
    issuer_url: str = Field(default="", alias="ISSUER_URL")  # walt.id Issuer2 management API
    # The issuer as wallets see it; the credential's iss is <this>/openid4vci.
    issuer_public_url: str = Field(default="http://localhost:7005", alias="ISSUER_PUBLIC_URL")
    issuer_profile: str = Field(default="villagePass", alias="ISSUER_PROFILE")
    # A claim offer (pre-authorized code) is minted when the claim page is opened and lives
    # this long; Issuer2 keeps offers in memory only.
    offer_expiry_seconds: int = Field(default=600, alias="OFFER_EXPIRY_SECONDS")
    verifier_url: str = Field(default="", alias="VERIFIER_URL")  # walt.id Verifier2, internal
    # This service as the browser sees it: claim pages, door, bridge, status list, vct.
    public_url: str = Field(default="http://localhost:8100", alias="PUBLIC_URL")
    # status_list.uri in the credential, as the verifier reaches it (Compose: service name).
    status_list_url: str = Field(default="", alias="STATUS_LIST_URL")
    # How long verifiers may cache the status list (ttl claim and Cache-Control max-age), in
    # seconds: the longest a revocation takes to reach the door. Low locally for the smoke test.
    status_list_ttl: int = Field(default=300, alias="STATUS_LIST_TTL")
    issuer_jwk_path: str = Field(default="", alias="ISSUER_JWK_PATH")
    issuer_x5c_path: str = Field(default="", alias="ISSUER_X5C_PATH")
    bridge_jwk_path: str = Field(default="", alias="BRIDGE_JWK_PATH")
    # The OIDC client Authentik's pass-bridge source uses (blueprint belfry-pass-bridge-source).
    bridge_client_id: str = Field(default="belfry-authentik", alias="BRIDGE_CLIENT_ID")
    bridge_client_secret: str = Field(default="", alias="BRIDGE_CLIENT_SECRET")

    @property
    def vct(self) -> str:
        return self.public_url.rstrip("/") + "/vct/village-pass/v1"

    @property
    def credential_issuer(self) -> str:
        return self.issuer_public_url.rstrip("/") + "/openid4vci"

    @property
    def effective_status_list_url(self) -> str:
        return self.status_list_url or self.public_url.rstrip("/") + "/status/1"

    # Blossom gate: hostnames (host[:port], lower case) a BUD-11 "server" tag may name; empty
    # accepts any. The gate itself (infra/blossom/Caddyfile) calls GET /blossom/auth.
    blossom_hosts: str = Field(default="", alias="BLOSSOM_HOSTS")

    hook_token: str = Field(alias="HOOK_TOKEN")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")

    event_start: dt.date = Field(default=dt.date(2027, 1, 25), alias="EVENT_START")
    event_end: dt.date = Field(default=dt.date(2027, 1, 31), alias="EVENT_END")

    # Not in the contract: when true, a posted invoice with payment_state paid/in_payment but no
    # payment.transaction at all (a payment registered by hand in Odoo) is accepted. Off by
    # default so that the contract's "done transaction with provider_reference" rule applies.
    allow_manual_payments: bool = Field(default=False, alias="ALLOW_MANUAL_PAYMENTS")

    # Steward pages (/steward, steward.py): Authentik application belfry-steward. Without the
    # client secret and the session secret the pages answer 503.
    steward_oidc_client_id: str = Field(default="belfry-steward", alias="STEWARD_OIDC_CLIENT_ID")
    steward_oidc_client_secret: str = Field(default="", alias="STEWARD_OIDC_CLIENT_SECRET")
    steward_session_secret: str = Field(default="", alias="STEWARD_SESSION_SECRET")
    steward_session_hours: int = Field(default=12, alias="STEWARD_SESSION_HOURS")
    steward_group: str = Field(default="belfry-stewards", alias="STEWARD_GROUP")

    log_level: str = Field(default="info", alias="LOG_LEVEL")
    reconcile_days: int = Field(default=30, alias="RECONCILE_DAYS")
