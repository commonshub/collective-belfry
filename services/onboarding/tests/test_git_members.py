"""The git server's members file (ngit-grasp, GRASP-08): written from active passes."""

from __future__ import annotations

import os

import pytest

from belfry_onboarding import core
from belfry_onboarding.git_members import GitMembers

from .test_relay_whitelist import seed_paid_invoice

A, B, C = "a" * 64, "b" * 64, "c" * 64


def lines(path):
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#")]


def test_sync_writes_sorted_hex_and_reports_changes(tmp_path):
    members = GitMembers(str(tmp_path / "members.txt"))
    result = members.sync([B, A.upper(), "not-a-key", B])
    assert result["changed"] and result["pubkeys"] == 2
    assert lines(tmp_path / "members.txt") == [A, B]
    assert members.sync([A, B])["changed"] is False
    assert members.sync([A])["changed"] is True
    assert lines(tmp_path / "members.txt") == [A]
    # replaced by rename: no temporary files are left behind
    assert os.listdir(tmp_path) == ["members.txt"]


def test_relay_whitelist_sync_also_writes_the_git_members(services, tmp_path, monkeypatch):
    services.git = GitMembers(str(tmp_path / "members.txt"))
    monkeypatch.setattr(core, "active_pubkeys", lambda svc: [B])
    core.sync_relay_whitelist(services, include=[C])
    assert lines(tmp_path / "members.txt") == [B, C]


def test_git_members_follow_activation_and_revocation(services, odoo, tmp_path):
    services.git = GitMembers(str(tmp_path / "members.txt"))
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    pass_ = next(iter(odoo.tables["belfry.pass"].values()))
    assert len(lines(tmp_path / "members.txt")) == 1
    assert core.revoke_pass(services, pass_["id"], "steward") == "revoked"
    assert lines(tmp_path / "members.txt") == []


def test_unwritable_members_file_does_not_stop_the_sync(services, tmp_path, monkeypatch):
    services.git = GitMembers(str(tmp_path / "missing-dir" / "members.txt"))
    monkeypatch.setattr(core, "active_pubkeys", lambda svc: [B])
    core.sync_relay_whitelist(services)  # logged, not raised
    with pytest.raises(OSError):
        core.sync_relay_whitelist(services, strict=True)
