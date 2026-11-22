"""The 5-minute check of port 25 from outside (mailcheck.py, docs/mail.md "Health check")."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import threading

import pytest
from aiosmtpd.smtp import SMTP

from belfry_onboarding import agentrequests, apps, mailcheck, mailer
from belfry_onboarding.register import Register

DOMAIN = "belfry-check.org"
BOX4, BOX6 = "203.0.113.7", "2001:db8::7"


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    """A mail server that answers like mail-in and remembers the commands it got."""

    def __init__(self, rcpt: str = "250 OK"):
        self.rcpt = rcpt
        self.seen: list[str] = []

    async def handle_EHLO(self, server, session, envelope, hostname, responses):
        self.seen.append("EHLO")
        session.host_name = hostname
        return responses

    async def handle_MAIL(self, server, session, envelope, address, options):
        self.seen.append(f"MAIL {address}")
        envelope.mail_from = address
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, options):
        self.seen.append(f"RCPT {address}")
        return self.rcpt

    async def handle_RSET(self, server, session, envelope):
        self.seen.append("RSET")
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.seen.append("DATA")
        return "250 OK"


class Server:
    def __init__(self, handler, families=("0.0.0.0", "::")):
        self.handler, self.families = handler, families
        self.port = free_port()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        async def up():
            return await self.loop.create_server(
                lambda: SMTP(handler, hostname=DOMAIN), host=list(families), port=self.port
            )

        self.server = asyncio.run_coroutine_threadsafe(up(), self.loop).result(5)

    def stop(self):
        async def down():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(down(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def mc(services, settings, tmp_path, monkeypatch):
    settings.register_path = str(tmp_path / "register.sqlite")
    settings.nip05_domain = DOMAIN
    settings.community_name = "Check Belfry"
    settings.console_url = f"https://belfry.{DOMAIN}"
    settings.mail_in = True
    settings.mailcheck = ""
    settings.smtp_host = ""  # system mails land in mailer.SENT
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    mailcheck._memory.clear()
    mailer.SENT.clear()
    dns = {
        (DOMAIN, "MX"): [f"10 {DOMAIN}."],
        (DOMAIN, "A"): [BOX4],
        (DOMAIN, "AAAA"): [BOX6],
    }
    services.dns = dns  # type: ignore[attr-defined]
    monkeypatch.setattr(mailcheck, "RESOLVE", lambda name, rr: list(dns.get((name, rr), [])))
    monkeypatch.setattr(
        agentrequests,
        "admins",
        lambda svc: [{"email": "ada@example.net", "pubkey": "a" * 64, "name": "Ada"}],
    )
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {"a" * 64})
    pushed: list[tuple[list[str], str, bool]] = []
    monkeypatch.setattr(
        mailcheck, "push", lambda svc, keys, line, down: pushed.append((keys, line, down)) or 1
    )
    services.pushed = pushed  # type: ignore[attr-defined]
    return services


def answers(mc, monkeypatch, v4, v6):
    """probe() answers per family: True ok, False fail, None no route."""
    table = {BOX4: v4, BOX6: v6}

    def probe(ip, dom, svc=None, port=None):
        got = table[ip]
        if got is True:
            return {"ok": True, "detail": "answers (3 ms)", "ms": 3}
        if got is None:
            return {"ok": None, "detail": "no route from inside the belfry"}
        return {"ok": False, "detail": "connection refused"}

    monkeypatch.setattr(mailcheck, "probe", probe)


# ── one SMTP conversation, on real sockets ──────────────────────────────────────────────────
def test_the_probe_asks_ehlo_mail_rcpt_and_never_data():
    rec = Recorder()
    srv = Server(rec, families=("127.0.0.1",))
    try:
        got = mailcheck.probe("127.0.0.1", DOMAIN, port=srv.port)
    finally:
        srv.stop()
    assert got["ok"] is True
    assert rec.seen == [
        "EHLO",
        f"MAIL mailcheck@{DOMAIN}",
        f"RCPT postmaster@{DOMAIN}",
        "RSET",
    ]


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")
def test_a_server_on_ipv4_only_fails_the_ipv6_check(mc, monkeypatch):
    """The 2026-11-22 outage: mail-in listened on IPv4 only."""
    srv = Server(Recorder(), families=("127.0.0.1",))
    mc.dns.update({(DOMAIN, "A"): ["127.0.0.1"], (DOMAIN, "AAAA"): ["::1"]})
    monkeypatch.setattr(mailcheck, "PORT", srv.port)
    try:
        got = mailcheck.check(mc)
        out = io.StringIO()
        code = mailcheck.cli(mc, out)
    finally:
        srv.stop()
    assert got["families"]["v4"]["ok"] is True
    assert got["families"]["v6"] == {
        "ok": False,
        "detail": "connection refused",
        "ip": "::1",
        "by": "onboarding",
    }
    lines = out.getvalue().splitlines()
    assert code == 1
    assert lines[0].startswith("v4 ok: answers") and lines[1].startswith(
        "v6 FAIL: connection refused"
    )


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")
def test_both_families_pass_on_a_server_listening_on_both(mc, monkeypatch):
    srv = Server(Recorder())
    mc.dns.update({(DOMAIN, "A"): ["127.0.0.1"], (DOMAIN, "AAAA"): ["::1"]})
    monkeypatch.setattr(mailcheck, "PORT", srv.port)
    try:
        out = io.StringIO()
        assert mailcheck.cli(mc, out) == 0
    finally:
        srv.stop()
    assert [x.split(":")[0] for x in out.getvalue().splitlines()] == ["v4 ok", "v6 ok"]


def test_a_backend_that_does_not_answer_fails_the_check():
    """RCPT 451: mail-in is up but the onboarding service did not answer it."""
    srv = Server(Recorder(rcpt="451 4.3.0 Try again later"), families=("127.0.0.1",))
    try:
        got = mailcheck.probe("127.0.0.1", DOMAIN, port=srv.port)
    finally:
        srv.stop()
    assert got == {"ok": False, "detail": "RCPT 451 4.3.0 Try again later"}


def test_postmaster_refused_is_fine_only_while_there_is_no_admin(mc, monkeypatch):
    rec = Recorder(rcpt=f"550 5.1.1 No one is called postmaster@{DOMAIN} here.")
    srv = Server(rec, families=("127.0.0.1",))
    try:
        assert mailcheck.probe("127.0.0.1", DOMAIN, mc, port=srv.port)["ok"] is False
        monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: set())
        got = mailcheck.probe("127.0.0.1", DOMAIN, mc, port=srv.port)
    finally:
        srv.stop()
    assert got["ok"] is True and "no admin yet" in got["detail"]


def test_nothing_listening_is_connection_refused():
    got = mailcheck.probe("127.0.0.1", DOMAIN, port=free_port())
    assert got == {"ok": False, "detail": "connection refused"}


def test_answers_never_carry_addresses():
    assert mailcheck._clean(b"550 No one is called bob@example.org here") == (
        "550 No one is called … here"
    )


# ── a round: state, the admins, flapping ────────────────────────────────────────────────────
def test_all_good_is_stored_and_tells_nobody(mc, monkeypatch):
    answers(mc, monkeypatch, True, True)
    state = mailcheck.run_round(mc, now=1000)
    assert state["active"] and state["host"] == DOMAIN and state["ever_ok"]
    assert {f: r["ok"] for f, r in state["families"].items()} == {"v4": True, "v6": True}
    assert state["families"]["v6"]["since"] == 1000
    assert json.loads(mc.odoo.param(mailcheck.PARAM))["families"]["v4"]["ok"] is True
    assert not mailer.SENT and not mc.pushed
    rows = mailcheck.status_rows(mc)
    assert [(r["name"], r["up"]) for r in rows] == [
        ("Mail in (IPv4)", True),
        ("Mail in (IPv6)", True),
    ]


def test_ipv6_refused_twice_tells_the_admins_once_then_once_on_recovery(mc, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="belfry_onboarding.mailcheck")
    answers(mc, monkeypatch, True, True)
    mailcheck.run_round(mc, now=1000)
    answers(mc, monkeypatch, True, False)
    state = mailcheck.run_round(mc, now=1300)
    assert state["families"]["v6"]["ok"] is False and state["families"]["v6"]["fails"] == 1
    assert not mailer.SENT and not mc.pushed  # one failed round: nobody yet
    assert any("IPv6 to port 25" in r.getMessage() for r in caplog.records)
    mailcheck.run_round(mc, now=1600)
    [down] = mailer.SENT
    assert down["To"] == "ada@example.net"
    assert down["Subject"] == f"Mail to {DOMAIN} is not arriving over IPv6"
    assert "connection refused" in down.get_body(("plain",)).get_content()
    assert mc.pushed == [(["a" * 64], f"Mail to {DOMAIN} is not arriving over IPv6", True)]
    # still down: no more mails
    mailcheck.run_round(mc, now=1900)
    mailcheck.run_round(mc, now=2200)
    assert len(mailer.SENT) == 1 and len(mc.pushed) == 1
    assert mailcheck.status_rows(mc)[1]["up"] is False
    assert "since" in mailcheck.status_rows(mc)[1]["detail"]
    # it comes back: once
    answers(mc, monkeypatch, True, True)
    state = mailcheck.run_round(mc, now=2500)
    assert state["families"]["v6"] == {**state["families"]["v6"], "ok": True, "fails": 0}
    assert state["families"]["v6"]["since"] == 2500
    assert len(mailer.SENT) == 2
    assert mailer.SENT[-1]["Subject"] == f"Mail to {DOMAIN} arrives again over IPv6"
    assert mc.pushed[-1][2] is False
    mailcheck.run_round(mc, now=2800)
    assert len(mailer.SENT) == 2
    # nothing of a person in the logs
    assert all("ada@example.net" not in r.getMessage() for r in caplog.records)


def test_one_failed_round_between_good_ones_tells_nobody(mc, monkeypatch):
    for i, v6 in enumerate([True, False, True, False, True, False, True]):
        answers(mc, monkeypatch, True, v6)
        mailcheck.run_round(mc, now=1000 + 300 * i)
    assert not mailer.SENT and not mc.pushed


def test_both_down_at_once_is_one_message(mc, monkeypatch):
    answers(mc, monkeypatch, False, False)
    mailcheck.run_round(mc, now=1000)
    mailcheck.run_round(mc, now=1300)
    [down] = mailer.SENT
    assert down["Subject"] == f"Mail to {DOMAIN} is not arriving over IPv4 and IPv6"


def test_no_route_from_here_is_not_checked_never_a_failure(mc, monkeypatch):
    answers(mc, monkeypatch, True, None)
    for i in range(3):
        state = mailcheck.run_round(mc, now=1000 + 300 * i)
    assert state["families"]["v6"]["ok"] is None and not mailer.SENT
    v6 = mailcheck.status_rows(mc)[1]
    assert v6["up"] is None and v6["detail"].startswith("no route")
    out = io.StringIO()
    monkeypatch.setattr(
        mailcheck,
        "probe",
        lambda ip, dom, svc=None, port=None: (
            {"ok": True, "detail": "answers"} if ip == BOX4 else {"ok": None, "detail": "no route"}
        ),
    )
    assert mailcheck.cli(mc, out) == 0
    assert "v6 not checked: no route" in out.getvalue()


def test_a_family_without_an_address_is_left_out(mc, monkeypatch):
    del mc.dns[(DOMAIN, "AAAA")]
    answers(mc, monkeypatch, True, False)
    state = mailcheck.run_round(mc, now=1000)
    assert state["families"]["v6"]["none"] and state["families"]["v6"]["ok"] is None
    assert [r["name"] for r in mailcheck.status_rows(mc)] == ["Mail in (IPv4)"]


# ── when it runs ────────────────────────────────────────────────────────────────────────────
def test_an_mx_pointing_elsewhere_is_never_checked(mc, monkeypatch):
    mc.dns[(DOMAIN, "MX")] = ["10 inbound-smtp.eu-west-1.amazonaws.com."]
    mc.dns[("inbound-smtp.eu-west-1.amazonaws.com", "A")] = ["198.51.100.1"]
    called = []
    monkeypatch.setattr(mailcheck, "probe", lambda *a, **k: called.append(a))
    state = mailcheck.run_round(mc, now=1000)
    assert not state["active"] and "another mail server" in state["reason"]
    assert mailcheck.status_rows(mc) == []
    out = io.StringIO()
    assert mailcheck.cli(mc, out) == 0 and "not checked" in out.getvalue()
    assert not called


def test_an_mx_host_on_the_same_box_counts(mc, monkeypatch):
    mc.dns[(DOMAIN, "MX")] = [f"10 mx.{DOMAIN}."]
    mc.dns[(f"mx.{DOMAIN}", "A")] = [BOX4]
    answers(mc, monkeypatch, True, True)
    state = mailcheck.run_round(mc, now=1000)
    assert state["active"] and state["host"] == f"mx.{DOMAIN}"
    assert state["families"]["v6"]["none"]  # mx.<domain> has no AAAA


def test_no_mx_is_checked_only_with_mailcheck_1_or_after_a_pass(mc, monkeypatch):
    del mc.dns[(DOMAIN, "MX")]
    answers(mc, monkeypatch, True, True)
    assert not mailcheck.run_round(mc, now=1000)["active"]
    mc.settings.mailcheck = "1"
    assert mailcheck.run_round(mc, now=1300)["active"]
    mc.settings.mailcheck = ""
    assert mailcheck.run_round(mc, now=1600)["active"]  # it passed here once: keep checking


@pytest.mark.parametrize(
    "change,why",
    [
        ({"mail_in": False}, "MAIL_IN=0"),
        ({"mailcheck": "0"}, "MAILCHECK=0"),
        ({"nip05_domain": "belfry.test"}, "no public domain"),
        ({"nip05_domain": "localhost"}, "no public domain"),
        ({"nip05_domain": "belfry.example.org"}, "no public domain"),
    ],
)
def test_off_switches(mc, monkeypatch, change, why):
    for k, v in change.items():
        setattr(mc.settings, k, v)
    monkeypatch.setattr(mailcheck, "RESOLVE", lambda *a: pytest.fail("no DNS"))
    go, reason = mailcheck.wanted(mc)
    assert not go and why in reason
    out = io.StringIO()
    assert mailcheck.cli(mc, out) == 0 and "not checked" in out.getvalue()


def test_the_tick_waits_for_the_start_and_the_interval(mc, monkeypatch):
    rounds = []
    monkeypatch.setattr(mailcheck, "run_round", lambda svc: rounds.append(1))
    monkeypatch.setattr(mailcheck, "_started", 0.0)
    monkeypatch.setitem(mailcheck._last, "round", -1000.0)
    assert not mailcheck.tick(mc, now=30, wait=True)  # the first minute after a start
    assert mailcheck.tick(mc, now=100, wait=True)
    assert not mailcheck.tick(mc, now=200, wait=True)
    assert mailcheck.tick(mc, now=100 + 300, wait=True)
    assert len(rounds) == 2


def test_the_status_page_shows_the_rows(mc, monkeypatch):
    from belfry_onboarding import console

    answers(mc, monkeypatch, True, False)
    mailcheck.run_round(mc, now=1000)
    rows = mailcheck.status_rows(mc)
    html = console.status_html(
        {"services": rows, "up": 1, "total": 2, "checked": 1000, "last_change": None}, None
    )
    assert "Mail in (IPv4)" in html and "Mail in (IPv6)" in html and "connection refused" in html


# ── the host's probe (smtpprobe.py, belfry-mailcheck.timer) and its file ───────────────────
from belfry_onboarding import smtpprobe  # noqa: E402


@pytest.fixture
def setup_dir(mc, tmp_path):
    folder = tmp_path / "setup"
    folder.mkdir()
    mc.settings.setup_dir = str(folder)
    return folder


def host_file(folder, v4, v6, at):
    data = {"active": True, "at": at, "host": DOMAIN}
    for fam, ok in (("v4", v4), ("v6", v6)):
        data[fam] = {
            "ok": ok,
            "detail": "answers (5 ms)" if ok else "no answer within 20 s",
            "at": at,
            "ip": BOX4 if fam == "v4" else BOX6,
        }
    smtpprobe.write_json(folder / smtpprobe.RESULT, data)


def test_a_round_writes_the_hosts_target(mc, setup_dir, monkeypatch):
    answers(mc, monkeypatch, True, True)
    mailcheck.run_round(mc, now=1000)
    target = json.loads((setup_dir / smtpprobe.TARGET).read_text())
    assert target["active"] is True and target["domain"] == DOMAIN and target["host"] == DOMAIN
    assert target["addresses"] == {"v4": [BOX4], "v6": [BOX6]} and target["no_admin"] is False
    mc.settings.mailcheck = "0"
    mailcheck.run_round(mc, now=1300)
    target = json.loads((setup_dir / smtpprobe.TARGET).read_text())
    assert target["active"] is False and "MAILCHECK=0" in target["reason"]


def test_a_fresh_host_result_is_used_instead_of_probing_from_here(mc, setup_dir, monkeypatch):
    """belfry.sh: from the edge bridge the box's own IPv6:25 times out; from the host it answers."""
    import time as _time

    called = []
    monkeypatch.setattr(
        mailcheck, "probe", lambda ip, dom, svc=None, port=None: called.append(ip) or {}
    )
    host_file(setup_dir, True, True, _time.time() - 60)
    state = mailcheck.run_round(mc)
    assert not called
    assert {f: (r["ok"], r["by"]) for f, r in state["families"].items()} == {
        "v4": (True, "host"),
        "v6": (True, "host"),
    }
    assert "(from the host)" in mailcheck.status_rows(mc)[1]["detail"]


def test_the_host_seeing_ipv6_down_twice_tells_the_admins(mc, setup_dir, monkeypatch):
    import time as _time

    monkeypatch.setattr(mailcheck, "probe", lambda *a, **k: pytest.fail("no probe from here"))
    now = _time.time()
    host_file(setup_dir, True, True, now - 30)
    mailcheck.run_round(mc, now=now)
    host_file(setup_dir, True, False, now - 30)
    mailcheck.run_round(mc, now=now + 1)
    assert not mailer.SENT
    mailcheck.run_round(mc, now=now + 2)
    [down] = mailer.SENT
    assert down["Subject"] == f"Mail to {DOMAIN} is not arriving over IPv6"
    host_file(setup_dir, True, True, now - 30)
    mailcheck.run_round(mc, now=now + 3)
    assert mailer.SENT[-1]["Subject"] == f"Mail to {DOMAIN} arrives again over IPv6"


def test_a_stale_host_result_means_probing_from_here(mc, setup_dir, monkeypatch):
    import time as _time

    host_file(setup_dir, False, False, _time.time() - mailcheck.HOST_FRESH - 60)
    answers(mc, monkeypatch, True, None)  # from here: IPv4 answers, IPv6 has no route
    state = mailcheck.run_round(mc)
    assert state["families"]["v4"]["ok"] is True and state["families"]["v4"]["by"] == "onboarding"
    assert state["families"]["v6"]["ok"] is None  # not checked, never a failure
    assert not mailer.SENT
    assert mailcheck.host_results(mc) == {}


def test_a_host_file_marked_inactive_or_broken_is_ignored(mc, setup_dir):
    (setup_dir / smtpprobe.RESULT).write_text("{not json")
    assert mailcheck.host_results(mc) == {}
    smtpprobe.write_json(setup_dir / smtpprobe.RESULT, {"active": False, "at": 10**10})
    assert mailcheck.host_results(mc) == {}


def test_the_target_command(mc, setup_dir):
    out = io.StringIO()
    assert mailcheck.cli(mc, out, target_only=True) == 0
    assert out.getvalue() == f"mail: target {DOMAIN}\n"
    assert json.loads((setup_dir / smtpprobe.TARGET).read_text())["addresses"]["v4"] == [BOX4]


def test_the_host_probe_reads_the_target_and_writes_its_result(tmp_path, capsys):
    import time as _time

    srv = Server(Recorder(), families=("127.0.0.1",))
    try:
        smtpprobe.write_json(
            tmp_path / smtpprobe.TARGET,
            {
                "at": _time.time(),
                "active": True,
                "domain": DOMAIN,
                "host": DOMAIN,
                "addresses": {"v4": ["127.0.0.1"], "v6": []},
            },
        )
        got = smtpprobe.host_round(tmp_path, port=srv.port)
    finally:
        srv.stop()
    assert got["v4"]["ok"] is True and got["v4"]["ip"] == "127.0.0.1"
    assert got["v6"]["ok"] is None and got["v6"]["none"]


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")
def test_the_host_probe_without_a_target_uses_the_domains_own_addresses(tmp_path, monkeypatch):
    srv = Server(Recorder(), families=("127.0.0.1",))  # IPv4 only: IPv6 must fail
    monkeypatch.setattr(smtpprobe, "PORT", srv.port)
    try:
        assert smtpprobe.main(["--setup", str(tmp_path), "--domain", "localhost", "--write"]) == 1
    finally:
        srv.stop()
    kept = json.loads((tmp_path / smtpprobe.RESULT).read_text())
    assert kept["v4"]["ok"] is True and kept["v6"]["ok"] is False
    assert oct((tmp_path / smtpprobe.RESULT).stat().st_mode & 0o777) == "0o664"


def test_the_host_probe_does_nothing_when_the_belfry_says_so(tmp_path, capsys):
    import time as _time

    smtpprobe.write_json(
        tmp_path / smtpprobe.TARGET,
        {"at": _time.time(), "active": False, "reason": "the MX points to another mail server"},
    )
    assert smtpprobe.main(["--setup", str(tmp_path), "--write"]) == 0
    assert "not checked: the MX points" in capsys.readouterr().out
    assert not (tmp_path / smtpprobe.RESULT).exists()


def test_the_host_probe_runs_as_a_plain_script(tmp_path):
    """The host has python3 and nothing else of the belfry's: no package import."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(smtpprobe.__file__)
    r = subprocess.run(
        [sys.executable, "-I", str(script), "--setup", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0 and "not checked" in r.stdout
