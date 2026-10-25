"""Phase 3, holder side: the claim page that puts the pass into a wallet, the Token Status
List verifiers read, the Type Metadata document, and the door check. HTML is rendered inline
(no template engine; the pages are two screens)."""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
from typing import Any

import segno

from . import credentials
from .core import PASS_FIELDS, Services, m2o_id, odoo_date

log = logging.getLogger(__name__)

TIER_NAMES = {"day": "Day pass", "week": "Week pass", "full": "Full access"}


class ClaimError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def qr_svg(data: str) -> str:
    """Inline SVG data URI of a QR code: dark modules on white, the pages put it on a white tile
    so phone cameras read it against the dark theme."""
    return segno.make(data, error="m").svg_data_uri(
        scale=6, border=2, dark="#1a231d", light="#ffffff"
    )


# TEST_RUN_NOTICE (settings.test_run_notice), set by create_app: a strip on top of every page.
TEST_NOTICE = ""


def page(title: str, body: str) -> str:
    """Open Source Village page frame, dark theme (brand/tokens.css, served at /brand): wordmark,
    one card, footer "run by Belfry". Mail clients never see this; the pages are small."""
    notice = (
        f'<div class="test-notice" role="note">{html.escape(TEST_NOTICE)}</div>'
        if TEST_NOTICE
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#2b3a31">
<link rel="icon" href="/brand/favicon.svg" type="image/svg+xml"><link rel="icon" href="/brand/favicon.ico" sizes="any">
<link rel="stylesheet" href="/brand/fonts.css"><link rel="stylesheet" href="/brand/tokens.css">
<title>{html.escape(title)}</title>
<style>
 body {{ font: 16px/1.5 var(--osv-font-sans); margin: 0; background: var(--osv-background); color: var(--osv-foreground); }}
 main {{ max-width: 640px; margin: 0 auto; padding: 32px 16px 48px; }}
 header {{ margin: 0 0 24px; }} header img {{ height: 28px; width: auto; display: block; }}
 .card {{ background: var(--osv-surface); border: 1px solid var(--osv-border); border-radius: 12px; padding: 24px; }}
 h1 {{ font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 4px; }}
 .muted {{ color: var(--osv-foreground-muted); }} a {{ color: var(--osv-accent); }}
 .qr {{ margin: 20px 0; }} .qr img {{ display: block; width: 280px; max-width: 100%; background: #fff; padding: 12px; border-radius: 12px; box-sizing: border-box; }}
 .btn {{ display: inline-block; background: var(--osv-accent); color: var(--osv-on-accent); font-weight: 600; padding: 10px 18px; border-radius: 8px; text-decoration: none; }}
 .btn:hover {{ background: var(--osv-accent-hover); }}
 .ok {{ color: var(--osv-accent); font-weight: 600; }} .bad {{ color: var(--osv-accent-2); font-weight: 600; }}
 code {{ font-family: var(--osv-font-mono); word-break: break-all; background: var(--osv-surface-deep); padding: 2px 6px; border-radius: 4px; }}
 footer {{ margin-top: 24px; font-size: 0.85rem; color: var(--osv-foreground-faint); }}
 footer nav {{ margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px 14px; }} footer nav a {{ color: var(--osv-foreground-muted); }}
 .test-notice {{ background: var(--osv-accent-2); color: var(--osv-on-accent); font-weight: 600; text-align: center; padding: 8px 16px; font-size: 0.95rem; }}
</style></head><body>{notice}<main>
<header><a href="https://opensourcevillage.org"><img src="/brand/osv-logo-horizontal-white.svg" alt="Open Source Village"></a></header>
{body}
<footer>Open Source Village, run by Belfry
<nav><a href="/help">Help</a><a href="/feedback">Feedback</a><a href="/rules">Community rules</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/refunds">Refunds</a></nav></footer>
</main></body></html>"""


def home_page(settings: Any) -> str:
    """The start page (SITE_URL, https://<domain>; also PUBLIC_URL when no SITE_URL is set): what
    Open Source Village and Belfry are, how people join (an invite from someone already in), and
    where participants go from here."""
    apps = [
        (
            settings.apps_public_url,
            "Your account and the apps",
            "Sign in by e-mail; chat, calendar, polls, tokens, directory, market, photos.",
        ),
        (
            "/me",
            "Show your pass",
            "Your pass as a QR code for the door. Invite friends from there.",
        ),
        ("/help", "Help", "Signing in, your pass at the door, the apps, tokens, stewards."),
    ]
    items = "".join(
        f'<li><a href="{html.escape(url)}"><strong>{html.escape(name)}</strong>'
        f'<span class="muted">{html.escape(text)}</span></a></li>'
        for url, name, text in apps
        if url
    )
    body = f"""<div class="card hero">
<h1>Open Source Village</h1>
<p class="lead">A gathering of people who make and use open source: to work, learn and live
together for a few days. <a href="https://opensourcevillage.org">About the village</a></p>
<p class="muted">Belfry runs the village's passes, sign-in and community apps: everything here is
open source too.</p>
</div>
<div class="card">
<h2>How to join</h2>
<ol class="steps">
<li><strong>Get an invite.</strong> The village grows through people who know each other: every
participant can invite a few friends, and stewards invite too. An invite is a personal link; ask
someone who is already in.</li>
<li><strong>Choose your pass</strong> (a day, a week or the whole event) and pay on the invite page.</li>
<li><strong>Check your e-mail.</strong> A sign-in link arrives; there is no password, ever.</li>
<li><strong>Use the apps and show your pass</strong> at the door, on your phone or in a wallet.</li>
</ol>
<p class="muted">No invite yet? There is no public sign-up. Ask a friend who is coming.</p>
</div>
<h2 class="section">Already in?</h2>
<ul class="links">{items}</ul>
<style>
 .hero h1 {{ font-size: 2rem; }} .lead {{ font-size: 1.1rem; }}
 h2 {{ font-size: 1.15rem; margin: 0 0 8px; }} h2.section {{ margin: 24px 0 0; }}
 .card + .card {{ margin-top: 16px; }}
 .steps {{ padding-left: 1.2em; margin: 0 0 12px; display: grid; gap: 8px; }}
 .links {{ list-style: none; padding: 0; margin: 12px 0 0; display: grid; gap: 12px; }}
 .links a {{ display: block; background: var(--osv-surface); border: 1px solid var(--osv-border); border-radius: 12px; padding: 16px 20px; color: var(--osv-foreground); text-decoration: none; }}
 .links a:hover {{ border-color: var(--osv-accent); }}
 .links strong {{ display: block; color: var(--osv-accent); }}
</style>"""
    return page("Open Source Village", body)


def not_found_page(home: str = "/") -> str:
    body = f"""<div class="card"><h1>Page not found</h1>
<p class="muted">This address does not lead anywhere. If it came from an e-mail, the link may be
incomplete; copy the whole link, or ask a steward for a new one.</p>
<p><a class="btn" href="{html.escape(home)}">Go to the start page</a></p></div>"""
    return page("Page not found", body)


# -- pass info for the community apps ------------------------------------------------------------

PASS_STATE_ORDER = {"active": 0, "pending": 1, "expired": 2, "revoked": 3}


def pass_info(svc: Services, partner_id: int) -> dict[str, Any] | None:
    """The partner's pass as the community apps show it (Keycast's "Your pass" card): the active
    pass if there is one, else the newest. None when the partner has no pass."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", partner_id]],
        ["state", "tier", "date_start", "date_end", "npub", "claim_token", "cashu_amount"],
        order="id desc",
    )
    if not rows:
        return None
    row = min(rows, key=lambda r: PASS_STATE_ORDER.get(r.get("state"), 9))
    partners = svc.odoo.read("res.partner", [partner_id], ["belfry_member_number"])
    base = svc.settings.public_url.rstrip("/")
    active = row.get("state") == "active"
    return {
        "state": row.get("state"),
        "tier": row.get("tier") or None,
        "tier_name": TIER_NAMES.get(row.get("tier"), row.get("tier")) or None,
        "date_start": row.get("date_start") or None,
        "date_end": row.get("date_end") or None,
        "member_number": (partners[0].get("belfry_member_number") if partners else None) or None,
        "npub": row.get("npub") or None,
        "claim_url": f"{base}/claim/{row['claim_token']}"
        if active and row.get("claim_token")
        else None,
        # the pass as a renewing QR code for the door, no wallet needed (mypass.py)
        "show_url": f"{base}/me" if active else None,
        "tokens": row.get("cashu_amount") or 0,
        "tokens_url": f"{base}/tokens/{row['claim_token']}"
        if active and row.get("claim_token") and row.get("cashu_amount")
        else None,
    }


# -- claim page ------------------------------------------------------------------------------------


def pass_for_claim(svc: Services, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(pass row, partner) for a claim token; the pass must be active."""
    if not token or len(token) < 16:
        raise ClaimError(404, "This claim link does not exist.")
    rows = svc.odoo.search_read("belfry.pass", [["claim_token", "=", token]], PASS_FIELDS, limit=1)
    if not rows:
        raise ClaimError(404, "This claim link does not exist.")
    pass_row = rows[0]
    if pass_row.get("state") != "active":
        raise ClaimError(410, f"This pass is {pass_row.get('state')}; it cannot be claimed.")
    partner_id = m2o_id(pass_row.get("partner_id"))
    partner = svc.odoo.read_one(
        "res.partner", partner_id, ["name", "email", "belfry_member_number", "belfry_npub"]
    )
    if partner is None:
        raise ClaimError(404, "This claim link does not exist.")
    return pass_row, partner


def create_claim_offer(
    svc: Services, pass_row: dict[str, Any], partner: dict[str, Any], sign_in: bool = False
):
    """Mint a fresh credential offer for this pass: pre-authorized, or with ``sign_in`` an
    authorization-code offer for wallets that only do that flow (DIDroom). Returns the Issuer2
    response (``credentialOffer`` is the QR payload) with the pass validity added."""
    if svc.issuer is None:
        raise ClaimError(503, "The credential issuer is not configured.")
    date_start = odoo_date(pass_row.get("date_start")) or dt.date.today()
    date_end = odoo_date(pass_row.get("date_end")) or date_start
    member_number = partner.get("belfry_member_number") or ""
    data = credentials.pass_credential_data(
        tier=pass_row.get("tier") or "day",
        display_name=partner.get("name") or member_number,
        member_number=member_number,
        npub=pass_row.get("npub") or partner.get("belfry_npub") or "",
        date_start=date_start,
        date_end=date_end,
    )
    idx = pass_row.get("credential_status_index") or pass_row["id"]
    status = credentials.status_claim(svc.settings.effective_status_list_url, int(idx))
    offer = svc.issuer.create_offer(
        svc.settings.issuer_profile,
        data,
        status,
        svc.settings.offer_expiry_seconds,
        sign_in=sign_in,
    )
    log.info(
        "claim: %s offer %s for pass %s",
        "sign-in" if sign_in else "pre-authorized",
        offer.get("offerId"),
        pass_row["id"],
    )
    return {
        **offer,
        "pass_id": pass_row["id"],
        "tier": pass_row.get("tier"),
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "sign_in": sign_in,
    }


def claim_page(svc: Services, pass_row: dict[str, Any], partner: dict[str, Any], offer) -> str:
    offer_url = offer["credentialOffer"]
    expires = dt.datetime.fromtimestamp(offer["expiresAt"] / 1000, dt.UTC)
    tier = TIER_NAMES.get(pass_row.get("tier"), pass_row.get("tier"))
    if offer.get("sign_in"):
        variant = """<p>This code is for wallets that ask you to sign in (DIDroom, for example). After
scanning, the wallet opens the Open Source Village sign-in: enter your e-mail and open the link we
send you, then return to the wallet.</p>"""
        switch = '<p class="muted"><a href="?">Back to the standard code</a></p>'
    else:
        variant = ""
        switch = (
            '<p class="muted">Your wallet asks you to sign in, or says the offer is not supported? '
            '<a href="?signin=1">Use the sign-in code</a>.</p>'
        )
    body = f"""
<div class="card">
<h1>Your Open Source Village pass</h1>
<p class="muted">{html.escape(tier or "")} · valid {offer["date_start"]} to {offer["date_end"]} ·
member {html.escape(partner.get("belfry_member_number") or "")}</p>
<p>Scan this code with your EUDI wallet (or another OpenID4VCI wallet) to add the pass certificate.
The code is valid until {expires.strftime("%H:%M UTC")}; reload the page for a new one.</p>
{variant}
<p class="qr"><img alt="QR code with the credential offer" src="{qr_svg(offer_url)}"></p>
<p><a class="btn" href="{html.escape(offer_url)}">Open in a wallet on this device</a></p>
<p class="muted">The pass shows your tier and validity to anyone who scans it at the door. Your name,
member number and Nostr key are only shared when you choose to disclose them.</p>
{switch}
<p class="muted">No wallet? <a href="/me">Show your pass as a QR code</a> instead: sign in and show
it at the door.</p>
</div>
{tokens_card(pass_row)}"""
    return page("Claim your Open Source Village pass", body)


# -- tokens ----------------------------------------------------------------------------------------


def tokens_card(pass_row: dict[str, Any]) -> str:
    amount = pass_row.get("cashu_amount") or 0
    if not amount or not pass_row.get("claim_token"):
        return ""
    return f"""<div class="card" style="margin-top:16px">
<h1>Your {amount} village tokens</h1>
<p class="muted">Tokens to pass on to other participants. They have no monetary value.</p>
<p><a class="btn" href="/tokens/{html.escape(pass_row["claim_token"])}">Get your tokens</a></p>
</div>"""


def tokens_page(svc: Services, pass_row: dict[str, Any]) -> str:
    """The pass's token bundle: a button into the wallet (wallet.<domain>/#token=…), the token
    to copy for any other Cashu wallet, or a note that it has been redeemed already."""
    rows = svc.odoo.read("belfry.pass", [pass_row["id"]], ["cashu_token", "cashu_amount"])
    token = rows[0].get("cashu_token") if rows else None
    amount = (rows[0].get("cashu_amount") if rows else 0) or 0
    if not token or svc.mint is None:
        raise ClaimError(404, "There are no tokens for this pass yet. Try again later.")
    state = svc.mint.token_state(token)
    wallet = svc.settings.wallet_public_url.rstrip("/")
    if state == "spent":
        body = f"""<div class="card"><h1>Your {amount} tokens are in a wallet</h1>
<p class="muted">They were added to a wallet already. Open it to see and send them.</p>
<p><a class="btn" href="{html.escape(wallet)}/">Open the wallet</a></p></div>"""
        return page("Your village tokens", body)
    note = (
        '<p class="bad">Some of these tokens are in a wallet already; the rest are added now.</p>'
        if state == "partial"
        else ""
    )
    body = f"""<div class="card">
<h1>Your {amount} village tokens</h1>
<p class="muted">Pass them on to other participants: as a payment at a stall, a thank-you, a vote of
confidence. They have no monetary value and cannot be paid in or out.</p>
{note}
<p><a class="btn" href="{html.escape(wallet)}/#token={html.escape(token)}">Add them to your wallet</a></p>
<p class="muted">Opens the Open Source Village wallet on this device. Using another Cashu wallet? Copy
this token into it:</p>
<p><code style="display:block;max-height:7em;overflow:auto">{html.escape(token)}</code></p>
</div>"""
    return page("Your village tokens", body)


# -- status list and type metadata -----------------------------------------------------------------


def revoked_status_indexes(svc: Services) -> set[int]:
    """Every index up to the highest one in use, except those of active passes: a credential
    whose pass was revoked, expired or deleted (removed by hand) never reads valid. Indexes above
    stay 0, so a pass activated after a verifier cached the list (STATUS_LIST_TTL) is valid at
    once; credentials from before a database reset are refused by the new issuer key."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["credential_status_index", "!=", False]],
        ["credential_status_index", "state"],
    )
    used = {int(r["credential_status_index"]) for r in rows if r.get("credential_status_index")}
    active = {
        int(r["credential_status_index"])
        for r in rows
        if r.get("credential_status_index") and r.get("state") == "active"
    }
    return set(range(max(used, default=-1) + 1)) - active


def status_list_jwt(svc: Services) -> str:
    if not svc.issuer_jwk:
        raise ClaimError(503, "The issuer key is not configured.")
    return credentials.status_list_token(
        issuer=svc.settings.credential_issuer,
        uri=svc.settings.effective_status_list_url,
        revoked=revoked_status_indexes(svc),
        jwk=svc.issuer_jwk,
        x5c=svc.issuer_x5c,
        ttl=svc.settings.status_list_ttl,
    )


def type_metadata(svc: Services) -> dict[str, Any]:
    logo = svc.settings.public_url.rstrip("/") + "/brand/apple-touch-icon.png"
    return credentials.type_metadata(svc.settings.vct, logo_uri=logo)


# -- door -------------------------------------------------------------------------------------------


def door_session(svc: Services) -> dict[str, Any]:
    """A verification session asking for the tier only; validity and status come from the
    policies (expiration, not-before, credential-status) and are never disclosed by name."""
    if svc.verifier is None:
        raise ClaimError(503, "The verifier is not configured.")
    from .clients.verifier import village_pass_policies, village_pass_query

    session = svc.verifier.create_session(
        village_pass_query(svc.settings.vct, ["pass_tier"]),
        village_pass_policies(svc.settings.credential_issuer, check_status=True),
    )
    return {
        "session_id": session["sessionId"],
        "authorization_request_url": session["bootstrapAuthorizationRequestUrl"],
    }


def failed_policies(info: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    results = info.get("policy_results") or {}
    for entry in results.get("vc_policies") or []:
        if isinstance(entry, dict) and entry.get("success") is False:
            failed.append(str((entry.get("policy") or {}).get("id") or "vc-policy"))
    for _q, policies in (info.get("presentation_validation_results") or {}).items():
        for name, result in (policies or {}).items():
            if isinstance(result, dict) and result.get("success") is False:
                failed.append(name)
    return failed


def door_result(svc: Services, session_id: str) -> dict[str, Any]:
    """{status: pending|valid|invalid, tier, valid_until, failed: [...]} for the door page."""
    if svc.verifier is None:
        raise ClaimError(503, "The verifier is not configured.")
    info = svc.verifier.session_info(session_id)
    status = info.get("status")
    if status == "SUCCESSFUL":
        cred = credentials.presented_credential(info) or {}
        if not credentials.issued_by_belfry(cred, svc.issuer_x5c[:1]):
            log.warning("door: session %s presented a pass not signed by Belfry's key", session_id)
            return {
                "status": "invalid",
                "verifier_status": status,
                "failed": ["issuer-certificate"],
            }
        claims = cred.get("credentialData") or {}
        exp = claims.get("exp")
        valid_until = (
            dt.datetime.fromtimestamp(int(exp), dt.UTC).date().isoformat() if exp else None
        )
        return {
            "status": "valid",
            "tier": claims.get("pass_tier"),
            "tier_name": TIER_NAMES.get(claims.get("pass_tier"), claims.get("pass_tier")),
            "valid_until": valid_until,
        }
    if info.get("attempted") or status in ("FAILED", "ERROR", "EXPIRED", "REJECTED"):
        return {"status": "invalid", "verifier_status": status, "failed": failed_policies(info)}
    return {"status": "pending", "verifier_status": status}


def door_page(session: dict[str, Any]) -> str:
    url = session["authorization_request_url"]
    body = f"""
<div class="card" id="door" data-session="{html.escape(session["session_id"])}">
<h1>Door check</h1>
<p>Ask the participant to scan this code with their wallet. Only the pass tier is requested;
validity and revocation are checked by the verifier.</p>
<p class="qr"><img alt="QR code with the verification request" src="{qr_svg(url)}"></p>
<p id="result" class="muted">Waiting for a presentation…</p>
<p><a class="btn" href="/door">New check</a></p>
</div>
<script>
const sid = document.getElementById('door').dataset.session;
async function poll() {{
  const r = await fetch('/door/sessions/' + sid); const j = await r.json();
  const el = document.getElementById('result');
  if (j.status === 'valid') {{ el.className = 'ok'; el.textContent = 'Valid pass: ' + (j.tier_name || j.tier) + ', valid until ' + j.valid_until; return; }}
  if (j.status === 'invalid') {{ el.className = 'bad'; el.textContent = 'Not valid: ' + ((j.failed || []).join(', ') || j.verifier_status); return; }}
  setTimeout(poll, 1500);
}}
poll();
</script>"""
    return page("Door check", body)


def json_dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"))
