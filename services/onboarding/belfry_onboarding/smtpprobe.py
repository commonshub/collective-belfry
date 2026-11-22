"""One SMTP conversation with the belfry's mail server, the way a sending server starts one:
EHLO, MAIL FROM:<mailcheck@domain>, RCPT TO:<postmaster@domain>, RSET, QUIT. Never DATA.

Standard library only: the onboarding service uses it (mailcheck.py) and so does the host, as a
plain script, because a container on Docker's edge bridge cannot reach the box's own public IPv6
address on port 25 (the DNAT rule skips traffic that starts on that bridge; belfry.sh,
2026-11-22). The host runs it every 5 minutes (belfry-mailcheck.timer, ``belfry setup-units``):

    python3 belfry_onboarding/smtpprobe.py --setup infra/data/setup [--domain D] [--write]

It probes the addresses in ``mailcheck-target.json`` (written by the onboarding service, which
has DNS: the MX host and its A / AAAA addresses, whether the check is wanted at all), else the
domain's own addresses, prints one line per family and, with --write, keeps the result in
``mailcheck.json`` next to it: {"at", "host", "v4": {"ok", "detail", "at", "ip"}, "v6": …}.
Exit 1 when a family fails.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import smtplib
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PORT = 25
TIMEOUT = 20.0
FAMILIES = {"v4": ("A", "IPv4", socket.AF_INET), "v6": ("AAAA", "IPv6", socket.AF_INET6)}
TARGET = "mailcheck-target.json"
RESULT = "mailcheck.json"
TARGET_MAX_AGE = 86400  # an older target (onboarding down for a day): the domain's own addresses
# a connect that fails at once because this machine has no route for the family: not a failure
LOCAL_ERRNOS = {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EHOSTUNREACH}
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


def clean(text: Any) -> str:
    """An SMTP answer for the logs and the status page: one line, no addresses."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return ADDRESS.sub("…", " ".join(str(text).split()))[:160]


def probe(
    ip: str,
    dom: str,
    no_admin: Callable[[], bool] | bool = False,
    port: int | None = None,
    timeout: float = TIMEOUT,
) -> dict[str, Any]:
    """{"ok": True|False|None, "detail", "ms"?} (None: no route from here for that family).
    NO_ADMIN: a 5xx for postmaster@ is fine while the belfry has no admin (it goes to them)."""
    start = time.monotonic()
    try:
        conn = smtplib.SMTP(timeout=timeout, local_hostname=f"mailcheck.{dom}")
        try:
            code, banner = conn.connect(ip, port or PORT)
        except OSError as exc:
            if getattr(exc, "errno", None) in LOCAL_ERRNOS and time.monotonic() - start < 2:
                return {"ok": None, "detail": "no route from here"}
            raise
        if code != 220:
            return {"ok": False, "detail": f"greeting {code} {clean(banner)}"}
        code, text = conn.ehlo()
        if code != 250:
            return {"ok": False, "detail": f"EHLO {code} {clean(text)}"}
        code, text = conn.mail(f"mailcheck@{dom}")
        if code != 250:
            return {"ok": False, "detail": f"MAIL FROM {code} {clean(text)}"}
        code, text = conn.rcpt(f"postmaster@{dom}")
        detail = ""
        if code != 250:
            if 500 <= code < 600 and (no_admin() if callable(no_admin) else no_admin):
                detail = f"answers (postmaster@ refused, {code}: no admin yet)"
            else:
                return {"ok": False, "detail": f"RCPT {code} {clean(text)}"}
        try:
            conn.rset()
            conn.quit()
        except (smtplib.SMTPException, OSError):
            conn.close()
        ms = int((time.monotonic() - start) * 1000)
        return {"ok": True, "detail": detail or f"answers ({ms} ms)", "ms": ms}
    except smtplib.SMTPServerDisconnected as exc:
        return {"ok": False, "detail": f"disconnected: {clean(exc) or 'no answer'}"}
    except TimeoutError:
        return {"ok": False, "detail": f"no answer within {int(timeout)} s"}
    except ConnectionRefusedError:
        return {"ok": False, "detail": "connection refused"}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {clean(exc)}"}


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically, readable by the onboarding service (the setup folder's group)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def own_addresses(dom: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fam, (_, _, af) in FAMILIES.items():
        try:
            infos = socket.getaddrinfo(dom, PORT, af, socket.SOCK_STREAM)
        except OSError:
            infos = []
        out[fam] = sorted({i[4][0] for i in infos})
    return out


def host_round(
    setup: Path, domain: str = "", now: float | None = None, port: int | None = None
) -> dict[str, Any]:
    """The host's round: {"active": bool, "reason"?, "host", "v4", "v6"} (families as probe()
    answers, plus "ip" and "at")."""
    now = time.time() if now is None else now
    target = read_json(setup / TARGET)
    fresh = target and now - float(target.get("at") or 0) < TARGET_MAX_AGE
    if fresh and not target.get("active"):
        return {"active": False, "reason": str(target.get("reason") or "not wanted")}
    if fresh:
        dom = str(target.get("domain") or domain)
        host = str(target.get("host") or dom)
        addresses = target.get("addresses") or {}
        no_admin = bool(target.get("no_admin"))
    else:
        if not domain:
            return {"active": False, "reason": "no target from the belfry and no --domain"}
        dom = host = domain
        addresses = own_addresses(domain)
        no_admin = False
    out: dict[str, Any] = {"active": True, "at": int(now), "host": host}
    for fam, (rr, _, _) in FAMILIES.items():
        ips = [str(x) for x in addresses.get(fam) or []]
        if not ips:
            out[fam] = {"ok": None, "detail": f"no {rr} record", "none": True, "at": int(now)}
            continue
        got = probe(ips[0], dom, no_admin, port=port)
        out[fam] = {**got, "ip": ips[0], "at": int(now)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--setup", required=True, help="the setup folder (infra/data/setup)")
    parser.add_argument("--domain", default="", help="when the belfry wrote no target yet")
    parser.add_argument("--write", action="store_true", help="keep the result in mailcheck.json")
    args = parser.parse_args(argv)
    setup = Path(args.setup)
    result = host_round(setup, args.domain)
    if not result["active"]:
        print(f"mail: not checked: {result['reason']}")
        return 0
    if args.write:
        write_json(setup / RESULT, result)
    bad = 0
    for fam, (_, label, _) in FAMILIES.items():
        got = result[fam]
        if got["ok"] is True:
            state = "ok"
        elif got["ok"] is None:
            state = "not checked"
        else:
            state, bad = "FAIL", bad + 1
        print(f"{fam} {state}: {got['detail']} ({result['host']}, {label}, from the host)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
