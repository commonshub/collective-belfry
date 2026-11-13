#!/usr/bin/env python3
"""Put back the settings a first admin changed on the setup page (``belfry setup-reset``).

Usage: setup-revert.py RESET_JSON ENV_FILE SECRETS_DIR

RESET_JSON is what ``python -m belfry_onboarding setup-reset`` printed; its ``revert`` maps each
BELFRY_* key the setup page queued during the claim to ``{"secret": true}`` or ``{"secret":
false, "before": "<value before>"}`` (setup.py ``_note_changes``).

- Secrets are cleared: their file in SECRETS_DIR is emptied (it stays: Compose mounts it). A
  cleared mail key also puts mail back to none (SMTP_HOST=mailpit, as a new belfry starts), so
  no mail goes through the claimant's account.
- Other settings get their value from before. A name with no earlier value is kept.

Prints what it changed on stderr (never a secret's value) and ``1`` on stdout when something
changed, else ``0``.
"""

from __future__ import annotations

import json
import os
import re
import sys

NAMES = {
    "BELFRY_NAME": ["COMMUNITY_NAME", "COMPANY_NAME"],
    "BELFRY_MAIL_FROM": ["MAIL_FROM"],
    "BELFRY_THEME": ["THEME"],
    "BELFRY_ENV_HOMEPAGE": ["HOMEPAGE"],
    "BELFRY_GIT_REPO": ["GIT_REPO"],
    "BELFRY_EXTRA_APPS": ["EXTRA_APPS"],
    "BELFRY_STRIPE_PUBLISHABLE_KEY": ["STRIPE_PUBLISHABLE_KEY"],
    "BELFRY_ACCOUNTING_ODOO_URL": ["ACCOUNTING_ODOO_URL"],
    "BELFRY_ACCOUNTING_ODOO_DB": ["ACCOUNTING_ODOO_DB"],
    "BELFRY_ACCOUNTING_ODOO_ISSUER": ["ACCOUNTING_ISSUER"],
}
SECRET_FILES = {
    "BELFRY_RESEND_API_KEY": "smtp_password",
    "BELFRY_LETTERMINT_API_KEY": "smtp_password",
    "BELFRY_STRIPE_SECRET_KEY": "stripe_secret_key",
    "BELFRY_MOLLIE_API_KEY": "mollie_api_key",
    "BELFRY_ACCOUNTING_ODOO_API_KEY": "accounting_odoo_api_key",
}
NO_MAIL = {"SMTP_HOST": "mailpit", "SMTP_PORT": "1025", "SMTP_USER": "", "SMTP_ENCRYPTION": "none"}


def fmt(v: str) -> str:
    return f'"{v}"' if re.search(r"[\s#\"']", v) else v


def revert(reset: dict, env_file: str, secrets_dir: str) -> bool:
    values: dict[str, str] = {}
    cleared: list[str] = []
    for key, change in sorted((reset.get("revert") or {}).items()):
        if change.get("secret"):
            name = SECRET_FILES.get(key)
            if name and name not in cleared:
                with open(os.path.join(secrets_dir, name), "w"):
                    pass
                cleared.append(name)
                print(f"  cleared secret {name}", file=sys.stderr)
            if name == "smtp_password":
                values.update(NO_MAIL)
            continue
        before = str(change.get("before") or "")
        if key == "BELFRY_NAME" and not before:
            print("  BELFRY_NAME: no earlier name known; kept", file=sys.stderr)
            continue
        for n in NAMES.get(key, []):
            values[n] = before
    if values:
        with open(env_file) as f:
            lines = f.read().splitlines(keepends=True)
        seen: set[str] = set()
        for i, line in enumerate(lines):
            m = re.match(r"^([A-Z0-9_]+)=", line)
            if m and m.group(1) in values:
                k = m.group(1)
                lines[i] = "" if k in seen else f"{k}={fmt(values[k])}\n"
                seen.add(k)
        lines += [f"{k}={fmt(v)}\n" for k, v in values.items() if k not in seen]
        with open(env_file, "w") as f:
            f.write("".join(lines))
        for k, v in values.items():
            print(f"  {k}={v}", file=sys.stderr)
    return bool(values or cleared)


if __name__ == "__main__":
    print(1 if revert(json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]) else 0)
