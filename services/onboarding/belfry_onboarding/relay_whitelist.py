"""Relay whitelist: render the rnostr config from its template with the pubkeys of active passes.

Odoo is the source of truth (plan: "the relay whitelist ... derived from it"). The template
(``infra/relay/rnostr.toml``) marks the lists to fill with a trailing ``# belfry:whitelist``
comment; everything else is copied verbatim. The rendered file is rewritten *in place* because
rnostr's ``--watch`` only reacts to data modifications of the config path, not to a rename.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MARKER = "belfry:whitelist"
_LINE = re.compile(
    r"^(?P<prefix>\s*pubkey_whitelist\s*=\s*)\[[^\]]*\](?P<suffix>\s*#\s*" + MARKER + r".*)$",
    re.MULTILINE,
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WhitelistTemplateError(ValueError):
    pass


def normalize_pubkeys(pubkeys: Any) -> list[str]:
    """Lower-cased, de-duplicated, sorted 64-hex pubkeys; anything else is dropped with a log."""
    out: set[str] = set()
    for raw in pubkeys or ():
        if not raw:
            continue
        key = str(raw).strip().lower()
        if _HEX64.match(key):
            out.add(key)
        else:
            log.warning("relay whitelist: ignoring value that is not a hex pubkey: %r", raw)
    return sorted(out)


def render(template: str, pubkeys: list[str]) -> str:
    """Replace every marked ``pubkey_whitelist`` list in ``template`` with ``pubkeys``."""
    if not _LINE.search(template):
        raise WhitelistTemplateError(f"template has no line marked '# {MARKER}'")
    rendered = "[" + ", ".join(f'"{k}"' for k in pubkeys) + "]"
    return _LINE.sub(lambda m: f"{m.group('prefix')}{rendered}{m.group('suffix')}", template)


def write_in_place(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` without a rename. Returns False if the file was unchanged."""
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    data = content.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


class RelayWhitelist:
    """Renders ``template_path`` into ``config_path``. Both are regular files."""

    def __init__(self, template_path: str, config_path: str, extra_pubkeys: str = ""):
        self.template_path = Path(template_path)
        self.config_path = Path(config_path)
        self.extra_pubkeys = normalize_pubkeys(extra_pubkeys.split(","))

    def sync(self, pubkeys: list[str]) -> dict[str, Any]:
        keys = normalize_pubkeys([*pubkeys, *self.extra_pubkeys])
        template = self.template_path.read_text(encoding="utf-8")
        changed = write_in_place(self.config_path, render(template, keys))
        if changed:
            log.info("relay whitelist: %s rewritten with %d pubkeys", self.config_path, len(keys))
        return {"pubkeys": len(keys), "changed": changed, "path": str(self.config_path)}
