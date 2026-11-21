#!/usr/bin/env python3
"""End-to-end check of Belfry's own signer (services/signer) with a real relay.

Run from services/onboarding (it uses the service's own client and NIP-46 code):

    PYTHONPATH=. python ../../infra/scripts/signer-smoke.py DIR [SIGNER_URL]

DIR holds the file ``token`` (the signer's API token); SIGNER_URL defaults to
http://127.0.0.1:8293. The signer must run with a relay both it and this script reach
(BUNKER_RELAYS = BUNKER_PUBLIC_RELAYS) and SIGNER_OWN_DOMAIN=belfry.test. It makes two accounts
(odoo-partner-1 and -2): use a throwaway database. docs/own-signer.md §7 has the commands.
"""
import json, sys, time
from coincurve import PrivateKey
from belfry_onboarding.clients.signer import SignerClient
from belfry_onboarding.clients.keycast import ClientKeyTaken
from belfry_onboarding.nip46check import BunkerClient, HostMap, Nip46Error
from belfry_onboarding.blossom import verify_signature

d = sys.argv[1]
url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8293"
c = SignerClient(url, open(d + "/token").read().strip(), domain="belfry.test")
acc = c.preload_user(vine_id="odoo-partner-1", username="0001", display_name="Ana")
tok, pk = acc["token"], acc["pubkey"]
c.set_username(tok, "ana")
print("account", pk[:12], "name ->", (c.pubkey_for_name("ana") or "")[:12])
ok = 0
def check(label, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + label); ok += 0 if cond else 1

def ev(kind, *tags):
    return {"kind": kind, "created_at": int(time.time()), "tags": [list(t) for t in tags], "content": "smoke"}

def nip46(url, key=None):
    return BunkerClient(url, HostMap({}), timeout=10, client_key=key)

# an outside app's connection: bound by the first connect, limited in what it signs
out = c.create_bunker(tok, "Notes", scope="outside")
b = nip46(out["bunker_url"])
r = b.call("connect", [b.bunker_pubkey, b.secret or ""])
check("connect (outside, by secret) -> " + str(r.get("result")), r.get("result") == "ack" and not r.get("error"))
r = b.call("get_public_key", [])
check("get_public_key is the account's key", r.get("result") == pk)
r = b.call("sign_event", [json.dumps(ev(1))])
signed = json.loads(r["result"]) if r.get("result") else {}
check("sign_event kind 1: signed by the account, signature verifies", signed.get("pubkey") == pk and verify_signature(signed))
for label, e in (("kind 9000 (NIP-29 moderation)", ev(9000, ("h", "general"))),
                 ("NIP-98 for belfry.belfry.test", ev(27235, ("u", "https://belfry.belfry.test/api/v1/admins"), ("method", "POST"))),
                 ("AUTH for relay.belfry.test", ev(22242, ("relay", "wss://relay.belfry.test"), ("challenge", "x"))),
                 ("kind 24133", ev(24133, ("p", pk)))):
    r = b.call("sign_event", [json.dumps(e)])
    check(f"outside refused {label}: {r.get('error')!r}", bool(r.get("error")) and not r.get("result"))
r = b.call("sign_event", [json.dumps(ev(27235, ("u", "https://api.example.org/x"), ("method", "GET")))])
check("outside may sign NIP-98 for another host", bool(r.get("result")))
# another client key with the same (spent) secret gets nothing
thief = nip46(out["bunker_url"])
try:
    r = thief.call("connect", [thief.bunker_pubkey, thief.secret or ""]); bad = not r.get("error")
except Nip46Error:
    bad = False
check("a second key with the spent secret is refused", not bad)
thief.close(); b.close()

# one of the belfry's own apps: bound to its client key when made, signs moderation too
key = PrivateKey()
own = c.create_bunker(tok, "Chat · smoke", key.public_key_xonly.format().hex())
check("own connection bound at creation", own["bound"] is True)
o = nip46(own["bunker_url"], key.secret)
r = o.call("sign_event", [json.dumps(ev(9000, ("h", "general")))])
check("own app signs kind 9000 without a connect first", bool(r.get("result")))
r = o.call("nip44_encrypt", [pk, "hello"]); enc = r.get("result")
r = o.call("nip44_decrypt", [pk, enc or ""])
check("nip44 round trip", r.get("result") == "hello")
o.close()
# one client key, one identity
other = c.preload_user(vine_id="odoo-partner-2", username="0002", display_name="Bob")
try:
    c.create_bunker(other["token"], "Chat", key.public_key_xonly.format().hex()); taken = False
except ClientKeyTaken:
    taken = True
check("the same client key for another account: client_key_taken", taken)
names = sorted(s["application_name"] for s in c.list_sessions(tok))
check(f"sessions listed: {names}", names == ["Chat · smoke", "Notes"])
c.revoke_session(tok, own["bunker_pubkey"])
check("revoked session gone from the list", [s["application_name"] for s in c.list_sessions(tok)] == ["Notes"])
check("derive_secret gives 64 hex", len(c.derive_secret(tok, "cashu-wallet-v1")) == 64)
print("FAILED" if ok else "ALL OK")
sys.exit(1 if ok else 0)
