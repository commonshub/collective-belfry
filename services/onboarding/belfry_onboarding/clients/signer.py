"""Client of Belfry's own signer (``services/signer``), with the interface of
:class:`~belfry_onboarding.clients.keycast.KeycastClient`.

``SIGNER=belfry`` makes ``build_services`` put this class where the Keycast client is
(``services.keycast``), so the callers do not change: the same method names, arguments, return
shapes and exceptions (``ClientKeyTaken`` is the Keycast module's class; an HTTP refusal is an
``httpx.HTTPStatusError`` with its ``response``; a refusal of the request itself by the signer
is a ``RuntimeError``; a taken username is a ``ValueError``). The signer's API is in
``services/signer/README.md``; the plan is ``docs/own-signer.md``.

Where the two signers differ, and what the caller sees:

- **The user token.** Keycast hands out a UCAN per user and every per-user call carries it. The
  signer has one caller (this service, with the API's bearer token) and takes the account in
  the path. ``preload_user`` therefore returns ``"token": "acct:<vine_id>"``: not a credential,
  only a name for the account, which the per-user methods turn back into the path. Nothing
  stored it before and nothing should now.
- **preload_user on an account that exists.** Keycast answers with the account as it is,
  whatever ``key_version`` or ``pubkey`` say. So does this client: it reads the account first
  and returns its public key, so callers that compare it with what they expected
  (``restore-keys``, ``sessions.py``) see a difference as before. ``display_name`` is not kept
  by the signer (the name lives in the register and in the kind-0 profile).
- **preload_user on an account that does not exist.** It is made, but never with another key
  than the register already has for the person: ``expected`` (set by ``signerguard.install``)
  gives that key, the signer is asked with ``expect_pubkey`` and writes nothing when the key it
  would make differs; :class:`KeyNotHeld` is raised. A username somebody else holds is 409
  (``httpx.HTTPStatusError``), as with Keycast: ``core.preload_account`` then asks again with
  another name.
- **Statuses.** The signer knows ``active`` and ``suspended``; ``banned`` is stored as
  ``suspended`` (both mean: nothing is signed). The reason is not kept.
- **sign_event / nostr_rpc.** Keycast answers 200 with ``{"error"}`` when it refuses a request;
  the signer answers 422 ``invalid``: both become ``RuntimeError``. A suspended account (403),
  an account with the member's own key (409 ``own_key``) or an unknown one (404) is an
  ``httpx.HTTPStatusError``. The signer fills in a missing ``pubkey`` where Keycast refused the
  event.
- **create_bunker.** ``app_name`` is the connection's application name. With ``client_pubkey``
  the connection is bound from the start and ``"bound"`` is always true. Without it the
  connection must be claimed by a first ``connect`` within ten minutes
  (``SIGNER_UNCLAIMED_SECONDS``), where Keycast kept it for thirty days.
- **list_sessions.** The signer's connections in Keycast's shape: ``application_name``,
  ``redirect_origin`` (always empty: the signer keeps no origin), ``bunker_pubkey``,
  ``client_pubkey``, ``created_at`` and ``last_activity`` as ISO 8601 UTC (the signer stores
  seconds), ``activity_count``.
- **revoke_session** of a bunker key that is not a live connection of the account does nothing
  and does not fail.
- **get_profile.** ``nip05`` is ``<username>@<domain>`` with the domain this client was given
  (Keycast derives it from the tenant).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from .keycast import USER_STATUSES, ClientKeyTaken

log = logging.getLogger(__name__)

TOKEN_PREFIX = "acct:"

_CRYPT = {
    "nip44_encrypt": ("encrypt", "nip44"),
    "nip44_decrypt": ("decrypt", "nip44"),
    "nip04_encrypt": ("encrypt", "nip04"),
    "nip04_decrypt": ("decrypt", "nip04"),
}


def _iso(seconds: Any) -> str | None:
    """The signer's seconds as Keycast wrote times: ISO 8601, UTC, ``Z``."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return dt.datetime.fromtimestamp(int(seconds), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    return str(data.get("error") or "") if isinstance(data, dict) else ""


class KeyNotHeld(RuntimeError):
    """The register has a key for this person, the signer has no account for them, and the
    account it would make has another key: nothing was made (signerguard.py)."""

    def __init__(self, vine_id: str):
        super().__init__(f"the signer does not hold the key of {vine_id} and may not make another")
        self.vine_id = vine_id


class SignerClient:
    def __init__(
        self,
        url: str,
        token: str,
        domain: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.url = url.rstrip("/")
        self.domain = domain
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        # vine_id -> the key the register has for that person, or None (signerguard.install)
        self.expected: Callable[[str], str | None] | None = None

    # -- plumbing -------------------------------------------------------------------------------

    def _account(self, token: str) -> str:
        """The path of the account a "user token" names."""
        vine_id = token.removeprefix(TOKEN_PREFIX)
        if not vine_id:
            raise ValueError("no account in the signer token")
        return f"{self.url}/accounts/{quote(vine_id, safe='')}"

    def _request(self, method: str, url: str, body: Any = None) -> httpx.Response:
        if body is None:
            return self._client.request(method, url, headers=self._headers)
        return self._client.request(method, url, json=body, headers=self._headers)

    # -- signing as a member --------------------------------------------------------------------

    def sign_event(self, token: str, event: dict[str, Any]) -> dict[str, Any]:
        """Sign an unsigned event as the token's account."""
        r = self._request("POST", self._account(token) + "/sign", {"event": event})
        if r.status_code == 422:
            raise RuntimeError(f"signer sign_event failed: {_code(r) or 'invalid'}")
        r.raise_for_status()
        return r.json()["event"]

    def nostr_rpc(self, token: str, method: str, params: list[Any]) -> Any:
        """``get_public_key``, ``sign_event`` ([event]), ``nip44_encrypt`` / ``nip44_decrypt``
        / ``nip04_encrypt`` / ``nip04_decrypt`` ([the other side's pubkey, text]) as the
        token's account. RuntimeError (naming the method only) when the signer refuses."""
        base = self._account(token)
        try:
            if method == "get_public_key":
                r = self._request("GET", base)
                key = "pubkey"
            elif method == "sign_event":
                r = self._request("POST", base + "/sign", {"event": params[0]})
                key = "event"
            elif method in _CRYPT:
                op, scheme = _CRYPT[method]
                peer, text = params
                r = self._request(
                    "POST", f"{base}/{op}", {"scheme": scheme, "peer": peer, "text": text}
                )
                key = "result"
            else:
                raise RuntimeError(f"signer {method} failed")
        except (IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"signer {method} failed") from exc
        if r.status_code == 422:
            raise RuntimeError(f"signer {method} failed")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or key not in data:
            raise RuntimeError(f"signer {method} failed")
        return data[key]

    def _text(self, token: str, method: str, peer: str, text: str) -> str:
        out = self.nostr_rpc(token, method, [peer, text])
        if not isinstance(out, str):
            raise RuntimeError(f"signer {method}: not a string")
        return out

    def nip44_encrypt(self, token: str, peer: str, plaintext: str) -> str:
        return self._text(token, "nip44_encrypt", peer, plaintext)

    def nip44_decrypt(self, token: str, peer: str, ciphertext: str) -> str:
        return self._text(token, "nip44_decrypt", peer, ciphertext)

    def nip04_encrypt(self, token: str, peer: str, plaintext: str) -> str:
        return self._text(token, "nip04_encrypt", peer, plaintext)

    def nip04_decrypt(self, token: str, peer: str, ciphertext: str) -> str:
        return self._text(token, "nip04_decrypt", peer, ciphertext)

    # -- accounts -------------------------------------------------------------------------------

    def preload_user(
        self,
        vine_id: str,
        username: str,
        display_name: str,
        key_version: int = 0,
        pubkey: str | None = None,
    ) -> dict[str, Any]:
        """Returns ``{"pubkey": <hex>, "token": "acct:<vine_id>"}``; creates the account when
        it does not exist (idempotent on ``vine_id``). ``key_version``: the derived key's
        version; ``pubkey``: an account without a key, for a member who holds their own."""
        if not vine_id:
            raise ValueError("vine_id must not be empty")
        token = TOKEN_PREFIX + vine_id
        have = self._request("GET", self._account(token))
        if have.status_code == 200:
            # it exists: answered as it is, never changed (as Keycast's preload); the caller
            # compares the key with the one it expected
            if pubkey and str(have.json().get("pubkey") or "").lower() != pubkey.lower():
                log.warning("signer: %s exists with another key than the one asked for", vine_id)
            return {"pubkey": have.json()["pubkey"], "token": token}
        if have.status_code != 404:
            have.raise_for_status()
        body: dict[str, Any] = {"key_version": int(key_version or 0), "username": username}
        expect = None
        if pubkey:
            body["own_pubkey"] = pubkey
        elif self.expected is not None:
            # a new account for somebody the register already has a key for: the signer may
            # make it only if it comes out with that key (signerguard.py). A register that
            # cannot be read raises here: not knowing is not "no key".
            expect = self.expected(vine_id)
            if expect:
                body["expect_pubkey"] = expect
        r = self._request("PUT", self._account(token), body)
        if r.status_code == 409 and _code(r) == "mismatch":
            again = self._request("GET", self._account(token))
            if again.status_code == 404 and expect:
                # no account, and the one the signer would make has another key
                log.error(
                    "signer: no account %s here, and the key this signer would make is not the "
                    "one the register has (%s…): refusing to make a new key. Was this belfry "
                    "migrated (docs/own-signer.md, Moving a belfry)?",
                    vine_id,
                    expect[:8],
                )
                raise KeyNotHeld(vine_id)
            # made by somebody else in between, with another key: answered as it is
            log.warning("signer: %s exists with another key than the one asked for", vine_id)
            r = again
        r.raise_for_status()
        return {"pubkey": r.json()["pubkey"], "token": token}

    def account(self, vine_id: str) -> dict[str, Any] | None:
        """The account as the signer has it (``vine_id, pubkey, custody, key_version, status,
        username``), or None when it has none. Never creates anything."""
        r = self._request("GET", self._account(TOKEN_PREFIX + vine_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def set_user_status(self, pubkey: str, status: str, reason: str | None = None) -> None:
        """``active``, ``suspended`` or ``banned`` (kept as suspended) for the account with
        this public key."""
        if status not in USER_STATUSES:
            raise ValueError(f"unknown signer user status {status!r}")
        r = self._request(
            "POST",
            f"{self.url}/pubkeys/{quote(pubkey, safe='')}/status",
            {"status": "active" if status == "active" else "suspended"},
        )
        r.raise_for_status()

    def set_username(self, token: str, username: str) -> None:
        """The account's username: its NIP-05 name (``<username>@<domain>``)."""
        r = self._request("PUT", self._account(token) + "/username", {"username": username})
        if r.status_code == 409:
            raise ValueError(f"the name {username} is taken")
        r.raise_for_status()

    def get_profile(self, token: str) -> dict[str, Any]:
        """The token's account: ``{"username", "nip05"}`` (either may be null)."""
        r = self._request("GET", self._account(token))
        r.raise_for_status()
        name = r.json().get("username")
        return {
            "username": name,
            "nip05": f"{name}@{self.domain}" if name and self.domain else None,
        }

    def pubkey_for_name(self, name: str) -> str | None:
        """The public key of the account with this username, or None."""
        r = self._request("GET", f"{self.url}/names/{quote(name, safe='')}")
        if r.status_code != 200:
            return None
        return r.json().get("pubkey") or None

    # -- NIP-46 connections ---------------------------------------------------------------------

    def create_bunker(
        self,
        token: str,
        app_name: str,
        client_pubkey: str | None = None,
        scope: str = "own",
    ) -> dict[str, Any]:
        """A new NIP-46 connection for the token's account: ``{"bunker_url", "bunker_pubkey",
        "app_name", "origin", "created_at", "bound"}``.

        With ``client_pubkey`` (hex) the connection is bound to that client key when it is
        made: no other key can use it, and the URL's secret opens nothing. A key linked to
        another account raises :class:`ClientKeyTaken`. Without it, the URL's secret binds the
        client key that first sends ``connect`` with it, within ten minutes
        (``SIGNER_UNCLAIMED_SECONDS``): after that the connection is gone.

        ``scope``: ``own`` for the belfry's own apps, ``outside`` for any other app. The signer
        never signs for an outside connection what remoteapps.py refuses a remote app (NIP-98
        to the belfry's hosts, AUTH to its relays, kind 24133, NIP-29 moderation, …)."""
        if scope not in ("own", "outside"):
            raise ValueError(f"scope must be own or outside, not {scope!r}")
        body: dict[str, Any] = {"app": app_name[:60] or "App", "scope": scope}
        if client_pubkey:
            body["client_pubkey"] = client_pubkey
        r = self._request("POST", self._account(token) + "/connections", body)
        if r.status_code == 409 and client_pubkey and _code(r) == "client_key_taken":
            raise ClientKeyTaken(client_pubkey)
        r.raise_for_status()
        data = r.json()
        out: dict[str, Any] = {
            "bunker_url": data["bunker_url"],
            "bunker_pubkey": data["bunker_pubkey"],
            "app_name": data.get("app") or body["app"],
            "origin": None,
            "created_at": _iso(data.get("created_at")),
        }
        if data.get("client_pubkey"):
            out["client_pubkey"] = data["client_pubkey"]
        out["bound"] = bool(client_pubkey) and (
            str(data.get("client_pubkey") or "").lower() == str(client_pubkey).lower()
        )
        return out

    def derive_secret(self, token: str, purpose: str) -> str:
        """The account's secret for PURPOSE (64 hex; the wallet's seed and nutzap key, Keycast
        patch 0011's formula). The caller decides which app may ask for which purpose."""
        r = self._request("POST", self._account(token) + "/derive", {"purpose": purpose})
        if r.status_code in (403, 422):
            raise RuntimeError("signer derive_secret failed")
        r.raise_for_status()
        return str(r.json()["secret"])

    def list_sessions(self, token: str) -> list[dict[str, Any]]:
        """The token's account's NIP-46 connections: application_name, redirect_origin,
        bunker_pubkey, client_pubkey, created_at, last_activity, activity_count."""
        r = self._request("GET", self._account(token) + "/connections")
        r.raise_for_status()
        return [
            {
                "application_name": c.get("app") or "",
                "redirect_origin": "",
                "bunker_pubkey": c.get("bunker_pubkey"),
                "client_pubkey": c.get("client_pubkey"),
                "created_at": _iso(c.get("created_at")),
                "last_activity": _iso(c.get("last_used_at")),
                "activity_count": int(c.get("uses") or 0),
            }
            for c in (r.json() or [])
        ]

    def revoke_session(self, token: str, bunker_pubkey: str) -> None:
        if not bunker_pubkey:
            raise ValueError("revoke_session needs a bunker_pubkey")
        r = self._request(
            "POST",
            self._account(token) + "/connections/revoke",
            {"bunker_pubkey": bunker_pubkey},
        )
        r.raise_for_status()
