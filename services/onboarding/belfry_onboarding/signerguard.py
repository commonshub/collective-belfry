"""Never a new key for somebody who has one (docs/own-signer.md §6, "The guards").

A belfry that moves from Keycast to its own signer must come out with every key unchanged. The
dangerous state is a signer that does not hold the accounts yet (``SIGNER=belfry`` set before
the import ran, or a ``signer-data`` volume that was lost) next to a register full of members:
the service asks the signer for "the account of person 12" all day long (community.member_token),
and a signer asked for an account it does not have makes one. For a key derived from the custody
seed that gives the same key again; for a key made before the seed existed, or imported, it
gives **another** key, and the member is somebody else from then on.

Three guards, all in this module:

- :func:`register_key`, given to the signer's client as ``expected``: when the register already
  knows a key for a person, the client asks the signer to create the account only if it comes
  out with that key (``expect_pubkey``); otherwise nothing is written and
  :class:`clients.signer.KeyNotHeld` is raised.
- :func:`check_start`: at start, a signer that holds none of the members' accounts, or holds
  one with another key, stops the service with a line that says why.
- :func:`has_people`: whether this belfry has, or ever had, members. The first-run setup page
  and ``belfry config`` decide on this, not on a file that a restore or a reset can lose.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from . import stewards

if TYPE_CHECKING:
    from .core import Services

log = logging.getLogger(__name__)

VINE = re.compile(r"^odoo-partner-(\d+)$")
HEX = re.compile(r"^[0-9a-f]{64}$")
# how many members the start check looks up in the signer (the newest and the oldest passes)
SAMPLE = 40


def short(pubkey: Any) -> str:
    """A public key as logs show it."""
    p = str(pubkey or "")
    return f"{p[:8]}…" if len(p) > 8 else p or "(none)"


def _m2o(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return int(value) if value else None


def register_key(svc: Services, vine_id: str) -> str | None:
    """The key the register has for the person behind VINE_ID (the newest of their passes that
    carries one), or None: a person the belfry has not given a key yet. Raises when the
    register cannot be read: not knowing is not "no key"."""
    m = VINE.fullmatch(vine_id or "")
    if m is None:
        return None
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", int(m.group(1))], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey"],
    )
    keys = [
        (int(r.get("id") or 0), str(r["keycast_pubkey"]).lower())
        for r in rows
        if HEX.fullmatch(str(r.get("keycast_pubkey") or "").lower())
    ]
    return max(keys)[1] if keys else None


def install(svc: Services) -> None:
    """Give the own signer's client the register's keys to hold new accounts to. Nothing with
    Keycast (its preload is what it always was)."""
    if stewards.own(svc) and hasattr(svc.keycast, "expected"):
        svc.keycast.expected = lambda vine_id: register_key(svc, vine_id)


def has_people(svc: Services) -> bool:
    """Whether the register has, or ever had, a member: a pass that is active, or one that was
    given a key (an activation, at any time, whatever became of it). An invitation nobody took
    and a draft are not people. Raises when the register cannot be read."""
    for domain in ([["state", "=", "active"]], [["keycast_pubkey", "!=", False]]):
        if svc.odoo.search_read("belfry.pass", domain, ["id"], limit=1):
            return True
    return False


def members(svc: Services) -> list[dict[str, Any]]:
    """Every person whose key the signer is expected to hold: ``{"partner", "vine_id",
    "pubkey", "custody"}``, one row per person (their newest pass with a key), by partner."""
    rows = svc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "!=", False]], ["keycast_pubkey", "partner_id", "state"]
    )
    newest: dict[int, tuple[int, str]] = {}
    for r in rows:
        partner = _m2o(r.get("partner_id"))
        key = str(r.get("keycast_pubkey") or "").lower()
        if partner and HEX.fullmatch(key):
            newest[partner] = max(newest.get(partner, (0, "")), (int(r.get("id") or 0), key))
    if not newest:
        return []
    custody = {
        int(p["id"]): str(p.get("belfry_key_custody") or "")
        for p in svc.odoo.read("res.partner", sorted(newest), ["belfry_key_custody"])
    }
    return [
        {
            "partner": partner,
            "vine_id": f"odoo-partner-{partner}",
            "pubkey": newest[partner][1],
            "custody": "self" if custody.get(partner) == "self" else "belfry",
        }
        for partner in sorted(newest)
    ]


class SignerNotMigrated(RuntimeError):
    """The signer does not hold the members this register has: the service must not run."""


def check_start(svc: Services) -> dict[str, Any]:
    """At start, on a belfry with its own signer: look a sample of the members up in the signer.

    - the signer answers and holds **none** of them, or holds one with **another key**:
      :class:`SignerNotMigrated` (the service does not start). ``SIGNER_RECOVERING=1`` lets a
      start with none through, for the one honest case: the signer's volume was lost and the
      keys are being derived again from the custody seed (``restore-keys``); the per-account
      guard still refuses any key that would come out different.
    - some are missing: a warning (the per-account guard decides for each when it is asked).
    - the signer does not answer: a warning; nothing can be created while it is down.

    Returns counts: ``{"checked", "held", "missing", "wrong", "skipped"}``."""
    out: dict[str, Any] = {"checked": 0, "held": 0, "missing": 0, "wrong": 0, "skipped": ""}
    if not stewards.own(svc):
        out["skipped"] = "not on the own signer"
        return out
    lookup = getattr(svc.keycast, "account", None)
    if lookup is None:
        out["skipped"] = "the signer's client cannot look accounts up"
        return out
    try:
        people = [m for m in members(svc) if m["custody"] != "self"]
    except Exception as exc:  # noqa: BLE001 — the register is down: the service says so itself
        log.warning("signer guard: the register could not be read at start: %s", exc)
        out["skipped"] = "register"
        return out
    half = SAMPLE // 2
    sample = people if len(people) <= SAMPLE else people[:half] + people[-half:]
    wrong: list[str] = []
    for m in sample:
        try:
            account = lookup(m["vine_id"])
        except Exception as exc:  # noqa: BLE001 — down or locked: nothing is created meanwhile
            log.warning("signer guard: the signer did not answer at start: %s", exc)
            out["skipped"] = "signer"
            return out
        out["checked"] += 1
        if account is None:
            out["missing"] += 1
        elif str(account.get("pubkey") or "").lower() != m["pubkey"]:
            out["wrong"] += 1
            wrong.append(
                f"{m['vine_id']} (register {short(m['pubkey'])}, signer "
                f"{short(account.get('pubkey'))})"
            )
        else:
            out["held"] += 1
    settings_of = getattr(svc.keycast, "settings", None)
    if callable(settings_of):
        try:
            closed = bool((settings_of() or {}).get("no_new_accounts"))
        except Exception:  # noqa: BLE001 — the start check reads, it does not decide on this
            closed = False
        out["closed"] = closed
        if closed:
            log.warning(
                "signer guard: the signer makes no new accounts (an import was run): nobody new "
                "can join until migrate-to-own-signer.sh open (the switch does it once its "
                "verify is clean)"
            )
    if wrong:
        raise SignerNotMigrated(
            f"SIGNER=belfry: the signer holds another key than the register for {len(wrong)} of "
            f"{out['checked']} members checked, first {wrong[0]}. Not starting: nobody's key may "
            "change. Go back to SIGNER=keycast (belfry config) or run the migration's verify "
            "(docs/own-signer.md, Moving a belfry)."
        )
    if out["checked"] and not out["held"]:
        if getattr(svc.settings, "signer_recovering", False):
            log.warning(
                "signer guard: the signer holds none of the %d members checked; "
                "SIGNER_RECOVERING is set, so accounts are made again, each only with the key "
                "the register has",
                out["checked"],
            )
            return out
        raise SignerNotMigrated(
            f"SIGNER=belfry: the register has {len(people)} members with a key and the signer "
            f"holds none of the {out['checked']} checked. This belfry's accounts were not "
            "imported (docs/own-signer.md, Moving a belfry: import, verify, then switch), or "
            "the signer's volume is gone (restore it from the backup; SIGNER_RECOVERING=1 only "
            "to derive the keys again from the custody seed). Not starting: no new keys for "
            "existing members."
        )
    if out["missing"]:
        log.warning(
            "signer guard: the signer does not hold %d of %d members checked; each is made "
            "again only if its key comes out as the register has it",
            out["missing"],
            out["checked"],
        )
    return out
