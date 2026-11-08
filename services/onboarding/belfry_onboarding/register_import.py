"""Move a belfry from Odoo to its own register (register.py): ``python -m belfry_onboarding
register-import``, run while Odoo still answers (REGISTER still odoo), before switching.

Copies people, passes, invites, applications, reports, feedback and followers with their ids
(the bunker derives every key from ``odoo-partner-<id>``: the same ids keep the same keys).
Only stored fields are copied, without files (screenshots) and without links to billing records
(invoices, orders, products): those stay in Odoo's backup. E-mail codes are not copied (they
expire in minutes). Nothing is written to Odoo."""

from __future__ import annotations

import logging
from typing import Any

from .clients.odoo import OdooApi
from .register import Register

log = logging.getLogger(__name__)

MODELS = [
    "res.partner",
    "belfry.pass",
    "belfry.invite",
    "belfry.application",
    "belfry.report",
    "belfry.feedback",
    "belfry.follower",
]
SKIP_TYPES = {"binary", "one2many", "many2many", "html"}
SKIP_FIELDS = {
    "id",
    "display_name",
    "write_uid",
    "create_uid",
    "write_date",
    "__last_update",
    "message_main_attachment_id",
    "access_token",
}


def _fields(odoo: OdooApi, model: str) -> dict[str, dict[str, Any]]:
    got = odoo.call(model, "fields_get", [], {"attributes": ["type", "store", "relation"]})
    out = {}
    for name, f in got.items():
        if name in SKIP_FIELDS or not f.get("store") or f.get("type") in SKIP_TYPES:
            continue
        if f.get("type") == "many2one" and f.get("relation") not in MODELS:
            continue  # billing and users: Odoo's
        out[name] = f
    return out


def _partner_ids(odoo: OdooApi) -> set[int]:
    """The people the belfry knows: on a pass, an invite, a follow, feedback, or with a number,
    a key or a verified address."""
    ids: set[int] = set()
    for model, fields in (
        ("belfry.pass", ["partner_id"]),
        ("belfry.invite", ["inviter_partner_id", "used_by_partner_id", "requested_by_partner_id"]),
        ("belfry.follower", ["partner_id"]),
        ("belfry.feedback", ["partner_id"]),
    ):
        for row in odoo.search_read(model, [], fields):
            for f in fields:
                v = row.get(f)
                if isinstance(v, list) and v:
                    ids.add(int(v[0]))
    for field in ("belfry_member_number", "belfry_npub", "belfry_verified_pubkey"):
        for row in odoo.search_read("res.partner", [[field, "!=", False]], ["id"]):
            ids.add(int(row["id"]))
    return ids


def run(odoo: OdooApi, reg: Register, force: bool = False) -> dict[str, int]:
    existing = reg._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    if existing and not force:
        raise SystemExit(f"the register at {reg.path} has {existing} records already (--force)")
    people = _partner_ids(odoo)
    counts: dict[str, int] = {}
    with reg._lock:
        reg._db.execute("BEGIN IMMEDIATE")
        try:
            if force:
                reg._db.execute("DELETE FROM records")
                reg._db.execute("DELETE FROM sequences")
            for model in MODELS:
                fields = _fields(odoo, model)
                domain = [["id", "in", sorted(people)]] if model == "res.partner" else []
                rows = odoo.search_read(model, domain, list(fields)) if fields else []
                for row in rows:
                    rec: dict[str, Any] = {"id": int(row["id"])}
                    for name, f in fields.items():
                        v = row.get(name, False)
                        if f.get("type") == "many2one":
                            v = int(v[0]) if isinstance(v, list) and v else False
                        rec[name] = v
                    reg._put(model, rec)
                counts[model] = len(rows)
                top = max((int(r["id"]) for r in rows), default=0)
                reg._db.execute(
                    "INSERT OR REPLACE INTO sequences (name, value) VALUES (?, ?)",
                    ("id:" + model, top),
                )
                log.info("register-import: %s %d", model, len(rows))
        except BaseException:
            reg._db.execute("ROLLBACK")
            raise
        reg._db.execute("COMMIT")
    return counts
