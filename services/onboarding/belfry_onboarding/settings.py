"""Environment configuration. Names match the contract in docs/slice-1-onboarding.md.

Secrets come as files: under Compose every secret is /run/secrets/<name in lower case>
(infra/docker-compose.yml, "secrets"); an environment variable of the same name wins (tests).
"""

from __future__ import annotations

import datetime as dt
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        secrets_dir=SECRETS_DIR if os.path.isdir(SECRETS_DIR) else None,
    )

    odoo_url: str = Field(default="http://odoo:8069", alias="ODOO_URL")
    odoo_db: str = Field(default="odoo", alias="ODOO_DB")
    odoo_user: str = Field(default="onboarding@belfry.local", alias="ODOO_USER")
    odoo_api_key: str = Field(default="", alias="ODOO_API_KEY")
    # Where people, passes and invites live: "odoo" (the Odoo add-on) or "belfry" (the belfry's
    # own register, one SQLite file: register.py). Empty: Odoo when it has an API key.
    register_mode: str = Field(default="", alias="REGISTER")
    register_path: str = Field(default="/data/register/register.sqlite", alias="REGISTER_PATH")
    # The sender of the belfry's own mails (register mode; Odoo has its own otherwise)
    mail_from: str = Field(default="", alias="MAIL_FROM")

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
    # The same relay as Nostr clients reach it (wss://relay.<domain>); written into the
    # participant's relay lists (kinds 10002 and 10050). Empty: RELAY_URL.
    community_relay_public_url: str = Field(default="", alias="COMMUNITY_RELAY_URL")
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
    # the vote service's polls key (its notes on polls and results); whitelisted like the above
    polls_pubkey: str = Field(default="", alias="POLLS_PUBKEY")

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
    # The start page's address (https://<domain>): this service's landing page, also served on the
    # apex; PUBLIC_URL's own "/" forwards there. Empty: the landing page stays on PUBLIC_URL.
    site_url: str = Field(default="", alias="SITE_URL")
    # The community's homepage on the apex (homepage.py): empty = the start page above; an
    # https:// URL = that website through a proxy; "nsite" = the site published with the homepage
    # key (homepage-publish); "nsite:<npub>[/<name>]" = another key's nsite (NIP-5A).
    homepage: str = Field(default="", alias="HOMEPAGE")
    # belfry.<domain> (console.py): the belfry's own page, status and members; empty: off
    console_url: str = Field(default="", alias="CONSOLE_URL")
    # The look of the pages (theme.py): one base look, Belfry's (THEME is not read here; the
    # host's `belfry apply` derives OSV_APPS and PASS_SIGNIN from it once), and the community's
    # own files (custom.css, logo, theme.json) in THEME_DIR (infra/data/theme).
    theme: str = Field(default="", alias="THEME")
    theme_dir: str = Field(default="/belfry-theme", alias="THEME_DIR")
    # The code shown at /git (gitview.py): nostr://<npub>/<relay>/<identifier> (ngit) or an
    # https:// git URL; empty = no /git.
    git_repo: str = Field(default="", alias="GIT_REPO")
    # name@domain sign-in (sessions.py): the open relay apps reach the belfry on (the bunker
    # relay, internal URL); its public URLs are BUNKER_PUBLIC_RELAYS (bunker_relays)
    session_relay_url: str = Field(default="", alias="SESSION_RELAY_URL")
    # belfry.<domain>/setup (setup.py): pending.env, code.json and status.json (infra/data/setup)
    setup_dir: str = Field(default="/belfry-setup", alias="SETUP_DIR")
    # the installed static apps (infra/data/apps, read-only), listed at apps.<domain>/ (appshome.py)
    apps_dir: str = Field(default="/belfry-apps", alias="APPS_DIR")
    # Open Source Village's app suite on chat., calendar., vote., … (compose profile osv-apps)
    osv_apps: bool = Field(default=False, alias="OSV_APPS")
    # the ones of that suite an admin added (appshome.EXTRAS keys, space-separated)
    extra_apps: str = Field(default="", alias="EXTRA_APPS")
    # the pass pages' sign-in: belfry (own e-mail + code page, passlogin.py) or authentik
    pass_signin: str = Field(default="belfry", alias="PASS_SIGNIN")
    # /changelog (community.py): written by infra/scripts/changelog.sh on the host, mounted here
    changelog_path: str = Field(default="/belfry-changelog/changelog.json", alias="CHANGELOG_PATH")
    # Where nsite manifests are looked up besides the community relay (space or comma separated).
    homepage_relays: str = Field(default="", alias="HOMEPAGE_RELAYS")
    # The community's Blossom server inside Compose: reads (blobs) and the gate (uploads).
    homepage_blossom_url: str = Field(default="http://blossom:3000", alias="HOMEPAGE_BLOSSOM_URL")
    homepage_upload_url: str = Field(default="http://blossom-gate", alias="HOMEPAGE_UPLOAD_URL")
    # secret belfry_nsec (hex): the belfry's own Nostr key (publishes the homepage nsite); the
    # relay whitelist and the Blossom gate let its pubkey in.
    belfry_nsec: str = Field(default="", alias="BELFRY_NSEC")
    # Push notifications to the phone apps (push.py, docs/push.md): the push gateway that holds
    # the publisher's APNs and FCM credentials; requests are signed with the belfry key. Empty:
    # no push (the devices API still answers).
    push_gateway_url: str = Field(default="https://push.belfry.sh", alias="PUSH_GATEWAY_URL")
    # E-mail as messages (mail.py): <username>@<domain> and <channel>@<domain> receive mail
    # (the mail-in service on port 25 hands it over); members answer as <username>@<domain>
    # through the same SMTP server Odoo uses. MAIL_IN=0 turns both off.
    mail_in: bool = Field(default=True, alias="MAIL_IN")
    mail_max_bytes: int = Field(default=15_000_000, alias="MAIL_MAX_BYTES")
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_encryption: str = Field(default="starttls", alias="SMTP_ENCRYPTION")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    # A notice on every page during a test run ("Test run: no real money …"); empty: none.
    test_run_notice: str = Field(default="", alias="TEST_RUN_NOTICE")
    # Tokens (decision 2026-10-24): Belfry's Cashu mint (cdk-mintd, no monetary value). MINT_URL is
    # the mint inside Compose (minting is only reachable there); empty turns tokens off.
    mint_url: str = Field(default="", alias="MINT_URL")
    mint_public_url: str = Field(default="http://localhost:3338", alias="MINT_PUBLIC_URL")
    mint_unit: str = Field(default="osv", alias="MINT_UNIT")
    tokens_per_day: int = Field(default=50, alias="TOKENS_PER_DAY")
    wallet_public_url: str = Field(default="http://localhost:3400", alias="WALLET_PUBLIC_URL")
    # The landing page's links (pass.<domain>/); empty hides the link.
    apps_public_url: str = Field(default="", alias="APPS_PUBLIC_URL")
    chat_public_url: str = Field(default="", alias="CHAT_PUBLIC_URL")
    calendar_public_url: str = Field(default="", alias="CALENDAR_PUBLIC_URL")
    # The community's name and the other public addresses, for /.well-known/belfry.json
    # (discovery.py): what an app needs to connect to this Belfry. Empty ones are left out.
    community_name: str = Field(default="", alias="COMMUNITY_NAME")
    odoo_public_url: str = Field(default="", alias="ODOO_PUBLIC_URL")
    groups_relay_public_url: str = Field(default="", alias="GROUPS_RELAY_URL")
    bunker_relays: str = Field(default="", alias="BUNKER_PUBLIC_RELAYS")
    blossom_public_url: str = Field(default="", alias="BLOSSOM_PUBLIC_URL")
    # Origins (comma separated, or *) whose pages may call the JSON APIs (/api/*) from the
    # browser. The APIs take no cookies, only NIP-98 headers. Empty: same origin only.
    api_cors_origins: str = Field(default="", alias="API_CORS_ORIGINS")
    # status_list.uri in the credential, as the verifier reaches it (Compose: service name).
    status_list_url: str = Field(default="", alias="STATUS_LIST_URL")
    # How long verifiers may cache the status list (ttl claim and Cache-Control max-age), in
    # seconds: the longest a revocation takes to reach the door. Low locally for the smoke test.
    status_list_ttl: int = Field(default=300, alias="STATUS_LIST_TTL")
    issuer_jwk_path: str = Field(default="", alias="ISSUER_JWK_PATH")
    issuer_x5c_path: str = Field(default="", alias="ISSUER_X5C_PATH")
    bridge_jwk_path: str = Field(default="", alias="BRIDGE_JWK_PATH")
    # The EUDI registrar's access and registration certificates (eudi.py); empty or without
    # wrpac.jwk.json: the issuer metadata is signed with the issuer key, no verifier_info.
    eudi_dir: str = Field(default="", alias="EUDI_DIR")
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

    # Accounting elsewhere (accounting.py): the organisation that sells the tickets books each
    # one in its own Odoo (JSON-2 API, Odoo Online too). Empty URL or key: Belfry's own invoice
    # is the buyer's. Ids are the issuer's records; `accounting-check` lists the candidates.
    accounting_odoo_url: str = Field(default="", alias="ACCOUNTING_ODOO_URL")
    accounting_odoo_db: str = Field(default="", alias="ACCOUNTING_ODOO_DB")
    accounting_odoo_api_key: str = Field(default="", alias="ACCOUNTING_ODOO_API_KEY")
    # Its name, in the activation mail ("Your invoice comes from ..."); bootstrap tells Odoo.
    accounting_issuer: str = Field(default="", alias="ACCOUNTING_ISSUER")
    # Pass tier -> product, "*" for any other line: e.g. "day=11,week=12,full=13,*=14"
    accounting_products: str = Field(default="", alias="ACCOUNTING_PRODUCTS")
    accounting_tax_included: str = Field(default="", alias="ACCOUNTING_TAX_INCLUDED")
    accounting_tax_excluded: str = Field(default="", alias="ACCOUNTING_TAX_EXCLUDED")
    # Payment journals: Stripe's payouts, and payments stewards mark by hand (empty: left open)
    accounting_journal: str = Field(default="", alias="ACCOUNTING_JOURNAL")
    accounting_manual_journal: str = Field(default="", alias="ACCOUNTING_MANUAL_JOURNAL")
    # The issuer's invoice ref: this prefix and Belfry's invoice number (the idempotency key)
    accounting_ref_prefix: str = Field(default="Belfry ", alias="ACCOUNTING_REF_PREFIX")
    # The issuer's Odoo mails its invoice (and credit note) to the buyer
    accounting_send: bool = Field(default=True, alias="ACCOUNTING_SEND")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")

    event_start: dt.date = Field(default=dt.date(2027, 1, 25), alias="EVENT_START")
    event_end: dt.date = Field(default=dt.date(2027, 1, 31), alias="EVENT_END")
    # Volunteers' passes (volunteer.py): from EVENT_START, this many days, unless a steward set
    # the pass's dates.
    volunteer_days: int = Field(default=14, alias="VOLUNTEER_DAYS")

    # Programme sync (programme.py, `python -m belfry_onboarding sync-programme`): the website's
    # programme grid and the rooms' public Google calendars as public calendar events, signed
    # with the programme key (secret programme_nsec, hex; the relay whitelist adds its pubkey).
    programme_nsec: str = Field(default="", alias="PROGRAMME_NSEC")

    # The groups relay's member API (groups_members.py), synced with the relay whitelist; signed
    # with the groups API key (secret groups_api_nsec, hex). Empty URL or key: no sync.
    # Morning digest of the announcements channel to its e-mail followers (followers.py): sent
    # once a day after this hour, local time in this zone.
    digest_hour: int = Field(default=8, alias="DIGEST_HOUR")
    digest_tz: str = Field(default="Europe/Brussels", alias="DIGEST_TZ")
    groups_api_url: str = Field(default="", alias="GROUPS_API_URL")
    # The git server's members file (git_members.py), written with the relay whitelist; the git
    # server (ngit-grasp, git.<domain>) re-reads it. Empty: no git server.
    git_members_path: str = Field(default="", alias="GIT_MEMBERS_PATH")
    # The git server as clients reach it (discovery, the skill); empty: none.
    git_public_url: str = Field(default="", alias="GIT_PUBLIC_URL")
    groups_api_nsec: str = Field(default="", alias="GROUPS_API_NSEC")
    programme_schedule_url: str = Field(
        default="https://opensourcevillage.org/schedule.json", alias="PROGRAMME_SCHEDULE_URL"
    )
    programme_site_url: str = Field(
        default="https://opensourcevillage.org", alias="PROGRAMME_SITE_URL"
    )
    programme_rooms_url: str = Field(
        default="https://raw.githubusercontent.com/commonshub/commonshub.brussels/main/src/settings/rooms.json",
        alias="PROGRAMME_ROOMS_URL",
    )
    # The times of the website's slots (it names only morning, afternoon and evening).
    programme_slots: str = Field(
        default="morning=09:30-12:30,afternoon=14:00-18:00,evening=18:30-21:30",
        alias="PROGRAMME_SLOTS",
    )
    programme_timezone: str = Field(default="Europe/Brussels", alias="PROGRAMME_TIMEZONE")
    programme_venue: str = Field(
        default="Commons Hub Brussels, Rue de la Madeleine 51, 1000 Brussels",
        alias="PROGRAMME_VENUE",
    )

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

    # The pass on the phone (/me, /check, mypass.py): Authentik application belfry-pass, and the
    # key of the session cookie and the QR codes. Without both the pages answer 503.
    pass_page_oidc_client_id: str = Field(default="belfry-pass", alias="PASS_PAGE_OIDC_CLIENT_ID")
    pass_page_oidc_client_secret: str = Field(default="", alias="PASS_PAGE_OIDC_CLIENT_SECRET")
    pass_page_secret: str = Field(default="", alias="PASS_PAGE_SECRET")
    pass_page_session_days: int = Field(default=14, alias="PASS_PAGE_SESSION_DAYS")

    # Pass info for the community apps (Keycast's "Your pass" card): bearer token of
    # GET /admin/partners/<id>/pass. Empty: the endpoint answers 503.
    pass_info_token: str = Field(default="", alias="PASS_INFO_TOKEN")

    log_level: str = Field(default="info", alias="LOG_LEVEL")
    reconcile_days: int = Field(default=30, alias="RECONCILE_DAYS")


def uses_register(settings: Settings) -> bool:
    """True when the belfry keeps its own register instead of Odoo (register.py)."""
    mode = (settings.register_mode or "").strip().lower()
    return mode == "belfry" or (mode != "odoo" and not settings.odoo_api_key)
