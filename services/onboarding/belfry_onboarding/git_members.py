"""Members of the git server (ngit-grasp at git.<domain>): the pubkeys of active passes.

ngit-grasp runs as a private GRASP-08 service: every relay session needs NIP-42 AUTH and every
git request a NIP-98 credential from a member. Belfry's patch (``infra/git/patches``) lets it
re-read its members from a file every five seconds; this module writes that file, one hex pubkey
per line, whenever the relay whitelist is synced. The file is replaced with a rename, so the
server never reads half of it (it keeps its previous members when a file does not parse).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .relay_whitelist import normalize_pubkeys

log = logging.getLogger(__name__)

HEADER = "# Written by Belfry's onboarding service: the pubkeys of active passes.\n"


def render(pubkeys: list[str]) -> str:
    return HEADER + "".join(f"{k}\n" for k in pubkeys)


class GitMembers:
    def __init__(self, path: str):
        self.path = Path(path)

    def sync(self, pubkeys: list[str]) -> dict[str, Any]:
        keys = normalize_pubkeys(pubkeys)
        content = render(keys)
        try:
            if self.path.read_text(encoding="utf-8") == content:
                return {"pubkeys": len(keys), "changed": False, "path": str(self.path)}
        except FileNotFoundError:
            pass
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".members-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        log.info("git members: %s rewritten with %d pubkeys", self.path, len(keys))
        return {"pubkeys": len(keys), "changed": True, "path": str(self.path)}
