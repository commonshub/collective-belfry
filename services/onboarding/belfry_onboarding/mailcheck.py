"""Is the belfry's mail server answering from the outside? (docs/mail.md, "Health check")

Incoming mail broke silently twice (a shared-inbox setting clobbered, then mail-in listening on
IPv4 only: every IPv6 sender, Gmail first, was refused for hours). Every 5 minutes, from the
followers tick, this connects to port 25 of the belfry's MX the way a sending server does, once
per address family DNS gives it (A, AAAA), and asks EHLO, MAIL FROM:<mailcheck@domain>,
RCPT TO:<postmaster@domain>, then RSET and QUIT: never DATA, nothing is delivered. The RCPT goes
through mail-in to the onboarding service (/internal/mail/rcpt), so a backend that does not
answer fails the check too (451).

The probe itself runs on the host (smtpprobe.py, belfry-mailcheck.timer): a container on the edge
bridge cannot reach the box's own IPv6 on port 25. Each round writes what the host should probe
(``mailcheck-target.json`` in the setup folder) and takes each family from the host's result
(``mailcheck.json``) when it is at most 15 minutes old; otherwise it probes from here, where a
family without a route is "not checked".

When it runs (``wanted``): MAIL_IN is on, MAILCHECK is not 0, the domain is a real one, and
either the domain's MX points at this belfry (the MX host is the domain itself, or shares an
address with it: the apex points at the box), or MAILCHECK=1, or a check here ever passed. An MX
pointing elsewhere (mail still at another provider) is never checked.

The result per family (ok, detail, since, checked, fails) is kept in the register param
``belfry.mailcheck`` (in memory with Odoo). Two failed rounds in a row tell the belfry's admins
once (a push of type ``mail_down`` and an e-mail, fixed texts); the first round that passes after
that tells them once more (``mail_up``). Every failure is logged. A family this container cannot
reach at all (no IPv6 route here) is "not checked", never a failure.

    python -m belfry_onboarding mailcheck    # one line per family; exit 1 on any failure
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import smtpprobe
from .core import Services

log = logging.getLogger(__name__)

PARAM = "belfry.mailcheck"
INTERVAL = 300  # seconds between rounds (the followers tick is 5 minutes too)
START_DELAY = 60  # not in the first minute after a start (short-lived test apps never run it)
TIMEOUT = 20.0  # seconds, per connection and per command
FAILS_TO_TELL = 2  # failed rounds in a row before the admins hear (no flapping on one blip)
PORT = 25
HOST_FRESH = 900  # the host's last round counts for 15 minutes; older, onboarding probes itself
FAMILIES = {"v4": ("A", "IPv4"), "v6": ("AAAA", "IPv6")}
NOT_REAL = {"test", "localhost", "local", "example", "invalid", "internal", "lan", "home"}

_lock = threading.Lock()
_running = threading.Lock()
_memory: dict[str, Any] = {}  # the state when the register has no params (Odoo)
_last: dict[str, float] = {"round": 0.0}
_started = time.monotonic()


# ── DNS ─────────────────────────────────────────────────────────────────────────────────────
def _dns(name: str, rtype: str) -> list[str]:
    """The records of NAME (text form), [] when there are none. Raises on a DNS failure."""
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rtype, lifetime=10.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as exc:
        raise OSError(f"DNS: {type(exc).__name__}") from exc
    return [r.to_text() for r in answer]


RESOLVE: Callable[[str, str], list[str]] = _dns  # tests: a fake DNS


def _addresses(host: str) -> dict[str, list[str]]:
    return {fam: sorted(set(RESOLVE(host, rr))) for fam, (rr, _) in FAMILIES.items()}


def domain(svc: Services) -> str:
    return (svc.settings.nip05_domain or "").strip().lower().rstrip(".")


def real_domain(name: str) -> bool:
    if "." not in name:
        return False
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        pass
    reserved = ("example.com", "example.net", "example.org")  # RFC 2606
    return name.rsplit(".", 1)[1] not in NOT_REAL and not any(
        name == r or name.endswith("." + r) for r in reserved
    )


def target(svc: Services) -> dict[str, Any]:
    """Where the world sends this belfry's mail: {"host", "addresses": {v4: [...], v6: [...]},
    "mx": True|False, "ours": True|False}. Raises OSError when DNS does not answer."""
    dom = domain(svc)
    records = []
    for r in RESOLVE(dom, "MX"):
        parts = r.split()
        if len(parts) == 2 and parts[0].isdigit():
            records.append((int(parts[0]), parts[1].rstrip(".").lower()))
    records = [r for r in records if r[1]]  # a null MX ("0 .") says: no mail here
    if not records:
        return {"host": dom, "addresses": _addresses(dom), "mx": False, "ours": False}
    apex: set[str] | None = None
    for _, host in sorted(records):
        found = _addresses(host)
        if host == dom:
            return {"host": host, "addresses": found, "mx": True, "ours": True}
        if apex is None:
            apex = {a for v in _addresses(dom).values() for a in v}
        if apex & {a for v in found.values() for a in v}:
            return {"host": host, "addresses": found, "mx": True, "ours": True}
    host = sorted(records)[0][1]
    return {"host": host, "addresses": _addresses(host), "mx": True, "ours": False}


# ── one SMTP conversation ───────────────────────────────────────────────────────────────────
_clean = smtpprobe.clean


def _no_admin(svc: Services | None) -> bool:
    if svc is None:
        return False
    try:
        from . import apps

        return not apps.admin_pubkeys(svc)
    except Exception:  # noqa: BLE001
        return False


def probe(ip: str, dom: str, svc: Services | None = None, port: int | None = None) -> dict:
    """EHLO, MAIL FROM, RCPT TO postmaster@, RSET, QUIT at IP from this container
    (smtpprobe.probe). {"ok": True|False|None, "detail"} (None: no route from here)."""
    return smtpprobe.probe(ip, dom, lambda: _no_admin(svc), port=port or PORT, timeout=TIMEOUT)


# ── the host's results and what it should probe (the setup folder) ──────────────────────────
def _setup(svc: Services) -> Path | None:
    from .setup import setup_dir

    return setup_dir(svc.settings)


def write_target(svc: Services, active: bool, reason: str = "", where: dict | None = None) -> None:
    """What the host's probe (belfry-mailcheck.timer) connects to: ``mailcheck-target.json``."""
    folder = _setup(svc)
    if folder is None:
        return
    data: dict[str, Any] = {"at": int(time.time()), "domain": domain(svc), "active": active}
    if reason:
        data["reason"] = reason
    if where:
        data.update(host=where["host"], addresses=where["addresses"], no_admin=_no_admin(svc))
    try:
        smtpprobe.write_json(folder / smtpprobe.TARGET, data)
    except OSError as exc:
        log.info("mailcheck: target not written: %s", type(exc).__name__)


def host_results(svc: Services, now: float | None = None) -> dict[str, Any]:
    """The host's last round (``mailcheck.json``) when it is at most HOST_FRESH old, else {}:
    {v4: {...}, v6: {...}} as probe() answers. The host reaches the box's public addresses on
    every family; a container on the edge bridge cannot reach its own IPv6 on port 25."""
    folder = _setup(svc)
    if folder is None:
        return {}
    got = smtpprobe.read_json(folder / smtpprobe.RESULT)
    now = time.time() if now is None else now
    if not got.get("active") or now - float(got.get("at") or 0) > HOST_FRESH:
        return {}
    return {f: got[f] for f in FAMILIES if isinstance(got.get(f), dict) and "ok" in got[f]}


def check(svc: Services, anyway: bool = False, use_host: bool = True) -> dict[str, Any]:
    """One round: {"host", "mx", "ours", "families": {v4: {...}, v6: {...}}} (a family without
    an address: {"ok": None, "detail": "no A record"}). A target that is not this belfry's is
    not connected to (no "families") unless ANYWAY. Writes the host's target. Each family comes
    from the host's last round when it is fresh (USE_HOST), else from a probe from here. Raises
    OSError when DNS does not answer."""
    dom = domain(svc)
    where = target(svc)
    out: dict[str, Any] = {k: where[k] for k in ("host", "mx", "ours")}
    if not where["ours"] and (where["mx"] or not anyway):
        write_target(svc, False, "the MX points to another mail server" if where["mx"] else "no MX")
        return out  # an MX elsewhere is never probed, not even with MAILCHECK=1
    write_target(svc, True, where=where)
    fams: dict[str, Any] = {}
    hosted = host_results(svc) if use_host else {}

    def one(fam: str) -> tuple[str, dict]:
        ips = where["addresses"].get(fam) or []
        if not ips:
            return fam, {"ok": None, "detail": f"no {FAMILIES[fam][0]} record", "none": True}
        if fam in hosted:
            got = {k: hosted[fam][k] for k in ("ok", "detail", "ms") if k in hosted[fam]}
            return fam, {**got, "ip": hosted[fam].get("ip") or ips[0], "by": "host"}
        got = probe(ips[0], dom, svc)
        return fam, {**got, "ip": ips[0], "by": "onboarding"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        for fam, got in pool.map(one, FAMILIES):
            fams[fam] = got
    out["families"] = fams
    return out


# ── when ────────────────────────────────────────────────────────────────────────────────────
def load(svc: Services) -> dict[str, Any]:
    getter = getattr(svc.odoo, "param", None)
    if getter is None:
        with _lock:
            return json.loads(json.dumps(_memory)) if _memory else {}
    try:
        raw = getter(PARAM, "")
        found = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}
    return found if isinstance(found, dict) else {}


def save(svc: Services, state: dict[str, Any]) -> None:
    setter = getattr(svc.odoo, "set_param", None)
    if setter is None:
        with _lock:
            _memory.clear()
            _memory.update(state)
        return
    setter(PARAM, json.dumps(state))


def wanted(svc: Services, state: dict[str, Any] | None = None) -> tuple[bool, str]:
    """(run?, why not) before DNS: MAIL_IN, MAILCHECK and the domain."""
    s = svc.settings
    flag = str(getattr(s, "mailcheck", "") or "").strip()
    if not s.mail_in:
        return False, "this belfry takes no mail (MAIL_IN=0)"
    if flag == "0":
        return False, "turned off (MAILCHECK=0)"
    if not real_domain(domain(svc)):
        return False, "no public domain"
    return True, ""


def forced(svc: Services, state: dict[str, Any]) -> bool:
    return str(getattr(svc.settings, "mailcheck", "") or "").strip() == "1" or bool(
        state.get("ever_ok")
    )


# ── a round: check, store, tell ─────────────────────────────────────────────────────────────
def run_round(svc: Services, now: float | None = None) -> dict[str, Any]:
    """Check, store the result, tell the admins of a change. Returns the stored state."""
    now = time.time() if now is None else now
    state = load(svc)
    go, why = wanted(svc, state)
    if not go:
        write_target(svc, False, why)
        state.update(active=False, reason=why, checked=int(now))
        save(svc, state)
        return state
    try:
        result = check(svc, anyway=forced(svc, state))
    except OSError as exc:
        # DNS itself did not answer: counted like a failure of every family checked before
        log.warning("mailcheck: %s", _clean(exc))
        result = None
        if not state.get("active"):
            state.update(reason=f"DNS: {_clean(exc)}", checked=int(now))
            save(svc, state)
            return state
    if result is not None and "families" not in result:
        reason = (
            "the MX points to another mail server"
            if result["mx"]
            else "the domain has no MX record (MAILCHECK=1 checks the domain itself)"
        )
        state.update(active=False, reason=reason, host=result["host"], checked=int(now))
        save(svc, state)
        return state
    fams = state.get("families") or {}
    went_down: list[str] = []
    came_back: list[str] = []
    for fam in FAMILIES:
        prev = fams.get(fam) or {}
        if result is None:
            if prev.get("ok") is None and not prev.get("fails"):
                continue
            got = {"ok": False, "detail": "DNS did not answer"}
        else:
            got = result["families"][fam]
        rec = dict(prev)
        rec["checked"] = int(now)
        rec["detail"] = got["detail"]
        if got.get("ip"):
            rec["ip"] = got["ip"]
        rec["by"] = got.get("by") or ""
        rec["ms"] = int(got.get("ms") or 0)
        if got["ok"] is True:
            if prev.get("ok") is not True:
                rec["since"] = int(now)
            if prev.get("told"):
                came_back.append(fam)
            rec.update(ok=True, fails=0, told=False)
            state["ever_ok"] = True
        elif got["ok"] is False:
            if prev.get("ok") is not False:
                rec["since"] = int(now)
            rec["ok"] = False
            rec["fails"] = int(prev.get("fails") or 0) + 1
            log.warning(
                "mailcheck: %s to port 25 of %s failed (%d in a row): %s",
                FAMILIES[fam][1],
                (result or {}).get("host") or domain(svc),
                rec["fails"],
                got["detail"],
            )
            if rec["fails"] >= FAILS_TO_TELL and not prev.get("told"):
                went_down.append(fam)
                rec["told"] = True
        else:  # no address in this family, or no route from here: not a failure
            if prev.get("ok") is not None:
                rec["since"] = int(now)
            rec.update(ok=None, fails=0, none=bool(got.get("none")))
            if prev.get("told"):
                came_back.append(fam)
                rec["told"] = False
        fams[fam] = rec
    state.update(active=True, reason="", families=fams, checked=int(now))
    if result is not None:
        state["host"] = result["host"]
    save(svc, state)
    if went_down:
        tell(svc, went_down, state, down=True)
    if came_back:
        tell(svc, came_back, state, down=False)
    return state


def _names(fams: list[str]) -> str:
    return " and ".join(FAMILIES[f][1] for f in fams)


def tell(svc: Services, fams: list[str], state: dict[str, Any], down: bool) -> None:
    """The admins hear once: an e-mail and a push (fixed texts; nothing from the SMTP answer
    goes into the push)."""
    from . import agentrequests, mailer

    try:
        people = agentrequests.admins(svc)
    except Exception as exc:  # noqa: BLE001
        log.warning("mailcheck: admins not readable: %s", exc)
        people = []
    dom = domain(svc)
    community = svc.settings.community_name or dom or "your belfry"
    which = _names(fams)
    if down:
        subject = f"Mail to {dom} is not arriving over {which}"
        reasons = [
            f"{FAMILIES[f][1]}: {(state['families'].get(f) or {}).get('detail', '')}" for f in fams
        ]
        paragraphs = [
            f"The belfry's mail server for {dom} did not answer properly over {which} in "
            f"{FAILS_TO_TELL} checks in a row (every 5 minutes, from the belfry itself).",
            "While this lasts, mail sent to the members' and channels' addresses over "
            f"{which} is refused or delayed. Sending servers usually try again for a few days.",
            *reasons,
            "To look: on the server, run `belfry apply` (it ends with this check) and "
            "`docker logs belfry-mail-in-1`. You will get one more e-mail when it works again.",
        ]
        line = f"Mail to {dom} is not arriving over {which}"
    else:
        subject = f"Mail to {dom} arrives again over {which}"
        paragraphs = [
            f"The belfry's mail server for {dom} answers again over {which}.",
        ]
        line = f"Mail to {dom} arrives again over {which}"
    log.warning("mailcheck: telling %d admin(s): %s", len(people), line)
    if not people:
        return
    try:
        m = getattr(svc.odoo, "mailer", None) or mailer.Mailer(svc.settings)
        m.send(
            [a["email"] for a in people],
            subject,
            paragraphs,
            footer="You get this because you are an admin of " + community + ".",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("mailcheck: the e-mail to the admins failed: %s", type(exc).__name__)
    push(svc, [a["pubkey"] for a in people if a.get("pubkey")], line, down)


def push(svc: Services, keys: list[str], line: str, down: bool) -> int:
    from . import push as push_mod
    from .agentrequests import console_base

    kind = "mail_down" if down else "mail_up"
    try:
        if not keys or not push_mod.enabled(svc):
            return 0
        devices = svc.odoo.push_devices(keys)
        if not devices:
            return 0
        gateway = push_mod.Gateway(svc)
        plain = {
            "v": 1,
            "domain": svc.settings.nip05_domain,
            "type": kind,
            "title": svc.settings.community_name or "Belfry",
            "body": line,
            "url": console_base(svc) + "/status",
        }
        sent = 0
        for d in devices:
            payload = push_mod.encrypt(d["key"], push_mod.fit(plain))
            if gateway.send(d, payload, kind, True) == "ok":
                sent += 1
        return sent
    except Exception:  # noqa: BLE001 — the e-mail and the status page carry it anyway
        log.exception("mailcheck: no push")
        return 0


def tick(svc: Services, now: float | None = None, wait: bool = False) -> bool:
    """From the followers tick: a round every INTERVAL, aside in a thread (a round can take up
    to a minute when port 25 does not answer). True when a round was started."""
    mono = time.monotonic() if now is None else now
    if mono - _started < START_DELAY or mono - _last["round"] < INTERVAL - 5:
        return False
    if not wanted(svc)[0] and not load(svc).get("active"):
        return False
    if not _running.acquire(blocking=False):
        return False
    _last["round"] = mono

    def go() -> None:
        try:
            run_round(svc)
        except Exception as exc:  # noqa: BLE001 — the next round tries again
            log.warning("mailcheck: round failed: %r", exc)
        finally:
            _running.release()

    if wait:
        go()
    else:
        threading.Thread(target=go, name="mailcheck", daemon=True).start()
    return True


# ── the status page ─────────────────────────────────────────────────────────────────────────
def status_rows(svc: Services) -> list[dict[str, Any]]:
    """Rows for the console's status page (console.status_html): "Mail in (IPv4 / IPv6)"."""
    try:
        state = load(svc)
    except Exception:  # noqa: BLE001
        return []
    if not state.get("active"):
        return []
    rows = []
    for fam, (_, label) in FAMILIES.items():
        rec = (state.get("families") or {}).get(fam)
        if not rec or rec.get("none"):
            continue
        since = rec.get("since")
        when = time.strftime(" since %d %b %H:%M UTC", time.gmtime(since)) if since else ""
        rows.append(
            {
                "name": f"Mail in ({label})",
                "up": rec.get("ok"),
                "ms": int(rec.get("ms") or 0),
                "detail": (rec.get("detail") or "")
                + (" (from the host)" if rec.get("by") == "host" else "")
                + when,
            }
        )
    return rows


# ── the command (belfry apply) ──────────────────────────────────────────────────────────────
def cli(svc: Services, out: Any = None, target_only: bool = False) -> int:
    """``python -m belfry_onboarding mailcheck``: probes from this container now, one line per
    family, exit 1 on any failure; stores nothing and tells nobody. TARGET_ONLY (``--target``):
    only write what the host's probe connects to (``belfry mailcheck`` and ``belfry apply`` run
    it first, then smtpprobe.py on the host)."""
    import sys

    out = out or sys.stdout
    go, why = wanted(svc)
    if not go:
        write_target(svc, False, why)
        print(f"mail: not checked: {why}", file=out)
        return 0
    if target_only:
        try:
            where = target(svc)
        except OSError as exc:
            print(f"mail: no target: {_clean(exc)}", file=out)
            return 1
        if not where["ours"] and where["mx"]:
            write_target(svc, False, "the MX points to another mail server")
        else:
            write_target(svc, True, where=where)
        print(f"mail: target {where['host']}", file=out)
        return 0
    try:
        result = check(svc, anyway=True, use_host=False)
    except OSError as exc:
        print(f"mail: FAIL: {_clean(exc)}", file=out)
        return 1
    if "families" not in result:
        print(f"mail: not checked: the MX ({result['host']}) is another mail server", file=out)
        return 0
    if not result["ours"]:
        print(
            "mail: note: the domain has no MX record; checking the domain itself (MAILCHECK=1 "
            "makes the 5-minute check do the same)",
            file=out,
        )
    bad = 0
    for fam, (_, label) in FAMILIES.items():
        got = result["families"][fam]
        if got["ok"] is True:
            state = "ok"
        elif got["ok"] is None:
            state = "not checked"
        else:
            state, bad = "FAIL", bad + 1
        print(f"{fam} {state}: {got['detail']} ({result['host']}, {label})", file=out)
    return 1 if bad else 0
