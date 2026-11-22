"""The signer's interface, as the rest of the service uses it: the same assertions against the
Keycast client and against the client of Belfry's own signer (``clients/signer.py``), each on
its fake. ``SIGNER_LIVE_URL`` (+ ``SIGNER_LIVE_TOKEN``) adds a third run against a real
``belfry-signer`` (services/signer), which is how the fake is kept honest; see
``docs/own-signer.md``.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime

import httpx
import pytest
import respx
from coincurve import PrivateKey

from belfry_onboarding import app as app_mod
from belfry_onboarding.clients.keycast import ClientKeyTaken, KeycastClient
from belfry_onboarding.clients.signer import SignerClient
from belfry_onboarding.nip46check import conversation_key, nip44_decrypt, nip44_encrypt
from belfry_onboarding.settings import Settings

from .fakes import FakeKeycast, FakeSigner

LIVE_URL = os.environ.get("SIGNER_LIVE_URL", "")
KINDS = ["keycast", "belfry"] + (["live"] if LIVE_URL else [])


def pub(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


class World:
    """One signer, its client, and what a test may need to know about it."""

    def __init__(self, kind: str, router: respx.MockRouter | None):
        self.kind = kind
        self.run = secrets.token_hex(4)  # a live signer keeps its accounts between runs
        self.fake: FakeKeycast | None = None
        if kind == "keycast":
            assert router is not None
            self.fake = FakeKeycast(router, "http://keycast.test")
            self.client = KeycastClient("http://keycast.test", "kc-admin", "kc-service")
        elif kind == "belfry":
            assert router is not None
            self.fake = FakeSigner(router, "http://signer.test", "http://keycast.test")
            self.client = SignerClient("http://signer.test", "t" * 20, domain="belfry.test")
        else:
            self.client = SignerClient(
                LIVE_URL, os.environ.get("SIGNER_LIVE_TOKEN", ""), domain="belfry.test"
            )

    def vine(self, n: int) -> str:
        return f"odoo-partner-{n}" if self.fake else f"contract-{self.run}-{n}"

    def name(self, base: str) -> str:
        return base if self.fake else f"{base}-{self.run}"

    def member(self, n: int, name: str, real_key: bool = False) -> dict:
        """Preloads account N. ``real_key``: the fakes give it a real key (a live signer's
        keys are real anyway), so what it signs and encrypts can be checked."""
        vine_id, name = self.vine(n), self.name(name)
        if real_key and self.fake is not None:
            key = PrivateKey()
            self.fake.users[vine_id] = {"pubkey": pub(key), "username": name}
            self.fake.status[pub(key)] = "active"
            self.fake.keys[pub(key)] = key.secret
        return self.client.preload_user(vine_id, name, name.title())


@pytest.fixture(params=KINDS)
def world(request):
    if request.param == "live":
        yield World("live", None)
        return
    with respx.mock(assert_all_called=False) as router:
        yield World(request.param, router)


def test_preload_is_idempotent_and_gives_a_token_for_the_account(world):
    a = world.member(1, "0001")
    assert len(a["pubkey"]) == 64 and a["token"]
    again = world.member(1, "0001")
    assert again["pubkey"] == a["pubkey"]
    b = world.member(2, "0002")
    assert b["pubkey"] != a["pubkey"]
    # each token acts for its own account
    assert world.client.nostr_rpc(a["token"], "get_public_key", []) == a["pubkey"]
    with pytest.raises(ValueError):
        world.client.preload_user("", "x", "X")


def test_an_account_with_the_members_own_key_gets_that_key(world):
    own = pub(PrivateKey())
    got = world.client.preload_user(world.vine(7), world.name("0007"), "Own", pubkey=own)
    assert got["pubkey"] == own
    assert world.client.preload_user(world.vine(7), world.name("0007"), "Own")["pubkey"] == own


def test_preload_with_another_key_answers_with_the_account_as_it_is(world):
    """Never a new key: the caller sees the key the account has and compares (restore-keys)."""
    a = world.member(1, "0001")
    other = pub(PrivateKey())
    got = world.client.preload_user(world.vine(1), world.name("0001"), "A", pubkey=other)
    assert got["pubkey"] == a["pubkey"] != other
    assert (
        world.client.preload_user(world.vine(1), world.name("0001"), "A", 5)["pubkey"]
        == (a["pubkey"])
    )


def test_usernames_are_set_read_found_and_unique(world):
    a = world.member(1, "0001")
    b = world.member(2, "0002")
    lina = world.name("lina")
    assert world.client.get_profile(a["token"])["username"] == world.name("0001")
    world.client.set_username(a["token"], lina)
    profile = world.client.get_profile(a["token"])
    assert profile == {"username": lina, "nip05": f"{lina}@belfry.test"}
    assert world.client.pubkey_for_name(lina) == a["pubkey"]
    assert world.client.pubkey_for_name(lina.upper()) == a["pubkey"]
    assert world.client.pubkey_for_name(world.name("nobody")) is None
    # the old name no longer resolves; the new one is taken, whatever the case
    assert world.client.pubkey_for_name(world.name("0001")) is None
    with pytest.raises(ValueError, match="taken"):
        world.client.set_username(b["token"], lina.upper())
    assert world.client.get_profile(b["token"])["username"] == world.name("0002")
    world.client.set_username(a["token"], lina)  # one's own name again: fine


def test_a_new_account_cannot_take_a_username_that_is_taken(world):
    world.member(1, "0001")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        world.client.preload_user(world.vine(2), world.name("0001"), "Second")
    assert exc.value.response.status_code == 409


def test_a_session_is_bound_to_its_client_key_and_one_key_is_one_account(world):
    a = world.member(1, "0001")
    b = world.member(2, "0002")
    device = pub(PrivateKey())
    made = world.client.create_bunker(a["token"], "Chat · Firefox on Linux", device)
    assert made["bound"] is True
    assert made["bunker_url"].startswith(f"bunker://{made['bunker_pubkey']}?")
    assert "secret=" in made["bunker_url"] and "relay=" in made["bunker_url"]
    assert made["app_name"] == "Chat · Firefox on Linux"
    assert made.get("client_pubkey", device) == device
    datetime.fromisoformat(made["created_at"].replace("Z", "+00:00"))
    with pytest.raises(ClientKeyTaken):
        world.client.create_bunker(b["token"], "Chat", device)
    assert world.client.list_sessions(b["token"]) == []
    # the same account, the same device, once more
    assert world.client.create_bunker(a["token"], "Chat", device.upper())["bound"] is True
    # revoking does not hand the key to another account
    world.client.revoke_session(a["token"], made["bunker_pubkey"])
    with pytest.raises(ClientKeyTaken):
        world.client.create_bunker(b["token"], "Chat", device)
    # without a client key: a connection the first connect will bind
    open_one = world.client.create_bunker(b["token"], "Notes")
    assert open_one["bound"] is False and "client_pubkey" not in open_one


def test_sessions_are_listed_as_the_apps_decode_them_and_revoked_by_bunker_key(world):
    a = world.member(1, "0001")
    b = world.member(2, "0002")
    device = pub(PrivateKey())
    first = world.client.create_bunker(a["token"], "Chat · Safari on iPhone", device)
    second = world.client.create_bunker(a["token"], "Notes")
    world.client.create_bunker(b["token"], "Chat", pub(PrivateKey()))
    rows = world.client.list_sessions(a["token"])
    assert {r["bunker_pubkey"] for r in rows} == {first["bunker_pubkey"], second["bunker_pubkey"]}
    row = next(r for r in rows if r["bunker_pubkey"] == first["bunker_pubkey"])
    # exactly the fields /api/me/sessions keeps (the phone apps decode them strictly)
    assert set(row) >= {
        "application_name",
        "redirect_origin",
        "bunker_pubkey",
        "client_pubkey",
        "created_at",
        "last_activity",
        "activity_count",
    }
    assert row["application_name"] == "Chat · Safari on iPhone"
    assert row["client_pubkey"] == device
    assert isinstance(row["created_at"], str)
    datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert row["last_activity"] is None and row["activity_count"] == 0
    assert isinstance(row["redirect_origin"], str)
    other = next(r for r in rows if r["bunker_pubkey"] == second["bunker_pubkey"])
    assert other["client_pubkey"] is None

    world.client.revoke_session(a["token"], first["bunker_pubkey"])
    left = world.client.list_sessions(a["token"])
    assert [r["bunker_pubkey"] for r in left] == [second["bunker_pubkey"]]
    assert len(world.client.list_sessions(b["token"])) == 1


def test_signing_and_encrypting_as_the_member(world):
    a = world.member(1, "0001", real_key=True)
    event = {"kind": 1, "content": "hello", "tags": [], "created_at": 1_700_000_000}
    signed = world.client.sign_event(a["token"], {**event, "pubkey": a["pubkey"]})
    assert signed["pubkey"] == a["pubkey"] and signed["content"] == "hello"
    assert len(signed["id"]) == 64 and len(signed["sig"]) == 128
    assert signed["created_at"] == 1_700_000_000
    via_rpc = world.client.nostr_rpc(a["token"], "sign_event", [{**event, "pubkey": a["pubkey"]}])
    assert via_rpc["id"] == signed["id"]

    peer = PrivateKey()
    conv = conversation_key(peer.secret, bytes.fromhex(a["pubkey"]))
    sealed = world.client.nip44_encrypt(a["token"], pub(peer), "psst")
    assert nip44_decrypt(conv, sealed) == "psst"
    assert world.client.nip44_decrypt(a["token"], pub(peer), nip44_encrypt(conv, "back")) == "back"
    old = world.client.nip04_encrypt(a["token"], pub(peer), "old style")
    assert world.client.nip04_decrypt(a["token"], pub(peer), old) == "old style"
    # what the signer refuses is a RuntimeError that names the method, not the text
    with pytest.raises(RuntimeError) as exc:
        world.client.nip44_decrypt(a["token"], pub(peer), "not a payload")
    assert "nip44_decrypt" in str(exc.value) and "not a payload" not in str(exc.value)
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        world.client.nostr_rpc(a["token"], "make_coffee", [])


def test_statuses_are_set_by_public_key(world):
    a = world.member(1, "0001")
    world.client.set_user_status(a["pubkey"], "suspended", "pass closed")
    world.client.set_user_status(a["pubkey"], "banned")
    world.client.set_user_status(a["pubkey"], "active")
    with pytest.raises(ValueError):
        world.client.set_user_status(a["pubkey"], "paused")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        world.client.set_user_status(pub(PrivateKey()), "active")
    assert exc.value.response.status_code == 404
    if world.fake is not None:
        assert world.fake.status[a["pubkey"]] == "active"
        assert [s for _, s in world.fake.status_calls][-1] == "active"


def test_a_signer_that_is_down_is_an_http_error_with_its_status(world):
    """Callers tell "the signer isn't answering" (503) from a refusal by the status."""
    if world.fake is None:
        pytest.skip("a live signer is not taken down")
    a = world.member(1, "0001")
    world.fake.down = True
    with pytest.raises(httpx.HTTPStatusError) as exc:
        world.client.sign_event(a["token"], {"kind": 1, "content": "", "pubkey": a["pubkey"]})
    assert exc.value.response.status_code == 503


def test_a_suspended_account_signs_nothing_on_belfrys_signer():
    if not LIVE_URL:
        pytest.skip("needs a real belfry-signer (SIGNER_LIVE_URL)")
    world = World("live", None)
    a = world.member(1, "0001")
    world.client.set_user_status(a["pubkey"], "suspended", "pass closed")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        world.client.sign_event(a["token"], {"kind": 1, "content": ""})
    assert exc.value.response.status_code == 403
    with pytest.raises(httpx.HTTPStatusError):
        world.client.create_bunker(a["token"], "Chat", pub(PrivateKey()))
    world.client.set_user_status(a["pubkey"], "active")
    assert world.client.sign_event(a["token"], {"kind": 1, "content": ""})["pubkey"] == a["pubkey"]


# -- which client the service builds ----------------------------------------------------------


def _settings(tmp_path, **more) -> Settings:
    return Settings(
        REGISTER_PATH=str(tmp_path / "register.sqlite"),
        ODOO_URL="http://odoo.test",
        ODOO_API_KEY="k",
        AUTHENTIK_TOKEN="t",
        KEYCAST_ADMIN_TOKEN="a",
        KEYCAST_SERVICE_TOKEN="s",
        HOOK_TOKEN="h",
        **more,
    )


def test_keycast_is_the_signer_unless_the_setting_says_belfry(tmp_path):
    assert isinstance(app_mod.build_signer(_settings(tmp_path)), KeycastClient)
    assert isinstance(app_mod.build_signer(_settings(tmp_path, SIGNER="Keycast")), KeycastClient)
    own = app_mod.build_signer(
        _settings(tmp_path, SIGNER="belfry", SIGNER_TOKEN="x" * 32, NIP05_DOMAIN="belfry.test")
    )
    assert isinstance(own, SignerClient)
    assert (own.url, own.domain) == ("http://signer:8200", "belfry.test")
    with pytest.raises(ValueError, match="SIGNER_TOKEN"):
        app_mod.build_signer(_settings(tmp_path, SIGNER="belfry"))
    with pytest.raises(ValueError, match="keycast or belfry"):
        app_mod.build_signer(_settings(tmp_path, SIGNER="nsecbunker"))


def test_the_two_clients_have_the_same_methods_and_arguments():
    import inspect

    def public(cls):
        return {
            name: inspect.signature(fn)
            for name, fn in inspect.getmembers(cls, inspect.isfunction)
            if not name.startswith("_")
        }

    keycast, own = public(KeycastClient), public(SignerClient)
    # the methods only the own signer has: a read that never creates (signerguard.py's check at
    # start and the migration's verify), its switches, and a connection's consent to decrypt
    for only in ("account", "settings", "set_consent"):
        assert only in own and only not in keycast
        del own[only]
    assert set(keycast) == set(own)
    for name, sig in keycast.items():
        assert list(sig.parameters) == list(own[name].parameters), name
        assert [p.default for p in sig.parameters.values()] == [
            p.default for p in own[name].parameters.values()
        ], name


# -- what moved out of Keycast's web side -----------------------------------------------------


def test_with_belfrys_signer_the_service_answers_nip05_itself(services, client, keycast):
    """Keycast serves nostr.json for its usernames; Belfry's signer has no web side, so the
    onboarding service answers from the signer's names (the proxy still has to send them)."""
    got = services.keycast.preload_user("odoo-partner-1", "lina", "Lina")
    services.settings.signer = "belfry"
    r = client.get("/.well-known/nostr.json", params={"name": "Lina"})
    assert r.status_code == 200 and r.json() == {"names": {"lina": got["pubkey"]}}
    assert r.headers["access-control-allow-origin"] == "*"
    for name in ("nobody", "not a name", "", "a" * 80):
        r = client.get("/.well-known/nostr.json", params={"name": name})
        assert r.status_code == 200 and r.json() == {"names": {}}
    # with Keycast nothing changes here: only _smtp and _groups are this service's
    services.settings.signer = "keycast"
    assert client.get("/.well-known/nostr.json", params={"name": "lina"}).json() == {"names": {}}


def test_the_status_page_checks_the_signer_that_is_in_use(settings):
    from belfry_onboarding import console

    settings.signer, settings.signer_url = "keycast", "http://signer:8200"
    by_name = {c[0]: c[1] for c in console.checks(settings)}
    assert by_name["Signer (Keycast)"] == "http://keycast.test/"
    assert "Sign-in (Authentik)" in by_name
    settings.signer = "belfry"
    by_name = {c[0]: c[1] for c in console.checks(settings)}
    assert by_name["Signer"] == "http://signer:8200/health" and "Signer (Keycast)" not in by_name
    assert "Sign-in (Authentik)" not in by_name  # a belfry on its own signer has none


def test_the_own_signers_client_sends_consent_origin_and_kind_and_hears_its_refusals():
    """clients/signer.py with Belfry's own signer only: the fields Keycast does not have."""
    from belfry_onboarding.clients.signer import NoNewAccounts

    with respx.mock(assert_all_called=False) as router:
        fake = FakeSigner(router, "http://signer.test", "http://keycast.test")
        client = SignerClient("http://signer.test", "t" * 20, domain="belfry.test")
        a = client.preload_user("odoo-partner-1", "ana", "Ana")
        made = client.create_bunker(
            a["token"],
            "Notes",
            scope="outside",
            decrypt=True,
            origin="https://notes.example.org",
            kind="http",
        )
        assert made["origin"] == "https://notes.example.org"
        b = fake.bunkers[-1]
        assert (b["decrypt"], b["origin"], b["kind"]) == (True, "https://notes.example.org", "http")
        plain = client.create_bunker(a["token"], "Notes", scope="outside", decrypt="yes")
        assert fake.bunkers[-1]["decrypt"] is False, "only True is a yes"
        assert plain["origin"] is None
        with pytest.raises(ValueError):
            client.create_bunker(a["token"], "Notes", kind="pigeon")
        rows = client.list_sessions(a["token"])
        assert sorted(r["redirect_origin"] for r in rows) == ["", "https://notes.example.org"]
        assert client.set_consent(a["token"], made["bunker_pubkey"], False) is True
        assert fake.bunkers[0]["decrypt"] is False
        assert client.set_consent(a["token"], "f" * 64, True) is False
        # an import is under way: the signer makes no account for a caller that names no key
        assert client.settings() == {"no_new_accounts": False, "by_environment": False}
        fake.no_new_accounts = True
        with pytest.raises(NoNewAccounts):
            client.preload_user("odoo-partner-2", "bob", "Bob")
        assert "odoo-partner-2" not in fake.users
        # with the key it must have, or the member's own, it is made
        own = pub(PrivateKey())
        assert client.preload_user("odoo-partner-3", "cy", "Cy", pubkey=own)["pubkey"] == own
        assert client.preload_user("odoo-partner-1", "ana", "Ana")["pubkey"] == a["pubkey"]


def test_keycasts_client_takes_the_new_arguments_and_sends_none_of_them():
    with respx.mock(assert_all_called=False) as router:
        fake = FakeKeycast(router, "http://keycast.test")
        client = KeycastClient("http://keycast.test", "kc-admin", "kc-service")
        a = client.preload_user("odoo-partner-1", "ana", "Ana")
        client.create_bunker(
            a["token"], "Notes", scope="outside", decrypt=True, origin="https://x", kind="http"
        )
        sent = __import__("json").loads(router.calls.last.request.content)
        assert set(sent) == {"app_name"}
        assert fake.bunkers[-1]["app_name"] == "Notes"
