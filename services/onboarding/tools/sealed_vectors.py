"""The shared test vectors of sealed conversations: docs/sealed-vectors.json, from docs/sealed.md.

    cd services/onboarding && PYTHONPATH=. python tools/sealed_vectors.py            # write it
    cd services/onboarding && PYTHONPATH=. python tools/sealed_vectors.py --check    # compare

Everything is fixed: the keys come from their names, the NIP-44 nonces and one-time keys from the
case's name, the times count from ``T0``, signatures use 32 zero bytes of auxiliary randomness.
Two runs give the same file, byte for byte.

What a case expects is written here by hand (which rosters are valid, whether a rumor is taken);
the code in belfry_onboarding/sealed.py is then run on the case and must agree, or nothing is
written. tests/test_sealed.py regenerates the file, compares, and runs every case from the JSON.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from belfry_onboarding import sealed
from belfry_onboarding.nip46check import conversation_key, nip44_encrypt

OUT = Path(__file__).resolve().parents[3] / "docs" / "sealed-vectors.json"
T0 = 1_795_000_000
AUX = bytes(32)
DAY = 86400

NAMES = [
    "belfry",
    "alice",
    "alice_phone",
    "alice_laptop",
    "alice_tablet",
    "alice_lb1",
    "alice_lb2",
    "alice_lb3",
    "bob",
    "bob_phone",
    "bob_lb1",
    "mallory",
    "mallory_phone",
]


def h(text: str) -> bytes:
    return hashlib.sha256(("belfry sealed vectors/" + text).encode()).digest()


SECRET = {n: h("key/" + n) for n in NAMES}
PUB = {n: sealed.pubkey_of(SECRET[n]) for n in NAMES}


def fixed(case: str, seal_at: int, wrap_at: int) -> dict[str, Any]:
    """What a wrap otherwise draws at random, from the case's name."""
    return {
        "seal_at": seal_at,
        "wrap_at": wrap_at,
        "one_time": h("once/" + case),
        "seal_nonce": h("nonce/seal/" + case),
        "wrap_nonce": h("nonce/wrap/" + case),
        "aux": AUX,
    }


def roster(
    signer: str,
    account: str,
    n: int | str,
    prev: dict | str | None,
    lb: str,
    devices: list[str],
    at: int,
    ready: bool = False,
    extra: list[list[str]] | None = None,
) -> dict[str, Any]:
    tags = [["p", PUB.get(account, account)], ["n", str(n)]]
    if prev is not None:
        tags.append(["prev", prev if isinstance(prev, str) else prev["id"]])
    tags.append(["lb", PUB.get(lb, lb)])
    tags += [["device", PUB.get(d, d)] for d in devices]
    if ready:
        tags.append(["ready"])
    tags += extra or []
    event = {"kind": sealed.ROSTER, "created_at": at, "tags": tags, "content": ""}
    return sealed.sign(SECRET[signer], event, AUX)


def anchor(
    account: str, root: dict | str, at: int, head: dict | str | None = None
) -> dict[str, Any]:
    """The account's anchor. HEAD absent: no head tag (= the root, §3.2)."""
    tags = [["d", sealed.ANCHOR_D], ["root", root if isinstance(root, str) else root["id"]]]
    if head is not None:
        tags.append(["head", head if isinstance(head, str) else head["id"]])
    return sealed.sign(
        SECRET[account], {"kind": sealed.ANCHOR, "created_at": at, "tags": tags, "content": ""}, AUX
    )


def summary(c: sealed.Chain | None) -> dict[str, Any]:
    if c is None:
        return {"valid_ids": [], "head": None, "devices": [], "lb": None, "ready": False}
    return {
        "valid_ids": c.valid_ids,
        "head": c.head.id,
        "devices": list(c.devices),
        "lb": c.lb,
        "ready": c.ready,
    }


def expect(valid: list[dict], ready: bool = False) -> dict[str, Any]:
    """The expected result, from the rosters that are valid, root first (written by hand)."""
    if not valid:
        return summary(None)
    head = valid[-1]
    return {
        "valid_ids": [e["id"] for e in valid],
        "head": head["id"],
        "devices": [t[1] for t in head["tags"] if t[0] == "device"],
        "lb": next(t[1] for t in head["tags"] if t[0] == "lb"),
        "ready": ready,
    }


def agree(what: str, got: Any, want: Any) -> None:
    if got != want:
        raise SystemExit(f"sealed.py disagrees with the vector {what!r}:\n {got!r}\n {want!r}")


# ── chains ──────────────────────────────────────────────────────────────────────────────────
def chains() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    A, P, L, T = "alice", "alice_phone", "alice_laptop", "alice_tablet"
    LB1, LB2, LB3 = "alice_lb1", "alice_lb2", "alice_lb3"
    out: list[dict[str, Any]] = []

    def case(name, note, anch, rosters, valid, ready=False, anchored=True, pinned=None):
        """ANCH a1 (alice's anchor with no head) stands for "the anchor a device publishes
        right after its roster": it gets the expected head, unless ANCHORED is False."""
        if anch is a1 and anchored and len(valid) > 1:
            anch = ah(valid[-1])
        want = expect(valid, ready)
        got = sealed.chain(anch, rosters, pinned["id"] if pinned else None)
        agree(name, summary(got), want)
        one = {"name": name, "note": note, "anchor": anch, "rosters": rosters}
        if pinned:
            one["pinned_head"] = pinned["id"]
        out.append({**one, "expected": want})

    def ah(head):
        """Alice's anchor naming HEAD, made a second after it."""
        return anchor(A, r1, head["created_at"] + 1, head)

    r1 = roster(P, A, 1, None, LB1, [P], T0)
    a1 = anchor(A, r1, T0 + 1)
    r2 = roster(P, A, 2, r1, LB1, [P, L], T0 + 100)
    r3 = roster(P, A, 3, r2, LB2, [P], T0 + 200)
    r4 = roster(P, A, 4, r3, LB2, [P, L], T0 + 300)

    case("a valid root", "One device, anchored by the account.", a1, [r1], [r1])
    case(
        "a device is added",
        "Roster 2 is signed by a device of roster 1 and keeps the letterbox.",
        a1,
        [r2, r1],
        [r1, r2],
    )
    case(
        "a device is removed, with a new letterbox",
        "Roster 3 drops the laptop and names a letterbox the chain never had. "
        "The wraps and keys_handover sections use this chain as alice's.",
        a1,
        [r1, r2, r3],
        [r1, r2, r3],
    )
    bad = roster(P, A, 3, r2, LB1, [P], T0 + 200)
    case(
        "a removal that keeps the old letterbox is invalid",
        "Roster 3 drops the laptop but keeps the letterbox: ignored, the chain ends at roster 2.",
        a1,
        [r1, r2, bad],
        [r1, r2],
    )
    bad = roster(P, A, 5, r4, LB1, [P], T0 + 400)
    case(
        "a removal that goes back to an earlier letterbox is invalid",
        "Roster 5 drops the laptop again and names the letterbox of rosters 1 and 2.",
        a1,
        [r1, r2, r3, r4, bad],
        [r1, r2, r3, r4],
    )
    bad = roster(P, A, 2, r1, LB2, [P, L], T0 + 100)
    case(
        "an addition that changes the letterbox is invalid",
        "No device left, so the letterbox must stay.",
        a1,
        [r1, bad],
        [r1],
    )
    bad = roster("mallory_phone", A, 2, r1, LB1, [P], T0 + 100)
    case(
        "the signer is not in the previous roster",
        "Someone else's device signs roster 2.",
        a1,
        [r1, bad],
        [r1],
    )
    bad = roster(L, A, 2, r1, LB1, [P, L], T0 + 100)
    case(
        "a device cannot add itself",
        "The laptop signs the roster that adds the laptop.",
        a1,
        [r1, bad],
        [r1],
    )
    fa = roster(P, A, 2, r1, LB1, [P, L], T0 + 100)
    fb = roster(P, A, 2, r1, LB1, [P, T], T0 + 101)
    win, lose = (fa, fb) if fa["id"] < fb["id"] else (fb, fa)
    lost_child = roster(P, A, 3, lose, LB1, [P, L, T], T0 + 150)
    case(
        "a fork after the anchored path: the lower id wins for now",
        "The anchor has no head (= the root). Two valid rosters name roster 1 as prev and "
        "neither is anchored yet: the one with the lower id (as lower-case hex) wins "
        "provisionally; the other and the roster built on it are ignored. The next anchor "
        "settles it.",
        a1,
        [r1, fa, fb, lost_child],
        [r1, win],
        anchored=False,
    )
    case(
        "the anchor names the other side of the fork",
        "The same rosters; the anchor's head is the roster with the HIGHER id: that branch is "
        "the chain, and the roster built on it follows as its tail.",
        ah(lose),
        [r1, fa, fb, lost_child],
        [r1, lose, lost_child],
    )
    at = T0 + 100
    while True:  # a loser that says ready, whatever the ids turn out to be
        fr = roster(P, A, 2, r1, LB1, [P], at, ready=True)
        if fr["id"] > fb["id"]:
            break
        at += 1
    case(
        "ready on the losing side of a fork does not count",
        "The anchor has no head; the roster with ready has the higher id.",
        a1,
        [r1, fr, fb],
        [r1, fb],
        anchored=False,
    )
    case(
        "an unknown prev",
        "Roster 3 is there but roster 2 is not.",
        a1,
        [r1, r3],
        [r1],
    )
    other_root = roster(L, A, 1, None, LB3, [L], T0 + 500)
    case(
        "only the root the anchor names counts",
        "The anchor names the laptop's root (a fresh start): the phone's chain is ignored.",
        anchor(A, other_root, T0 + 501),
        [r1, r2, other_root],
        [other_root],
    )
    case(
        "the anchor names a root that is not there",
        "No chain.",
        anchor(A, other_root, T0 + 501),
        [r1, r2],
        [],
    )
    bad = roster(L, A, 1, None, LB1, [P], T0)
    case(
        "a root whose signer is not among its devices",
        "No chain.",
        anchor(A, bad, T0 + 1),
        [bad],
        [],
    )
    bad = roster(P, "bob", 1, None, LB1, [P], T0)
    case(
        "a root with another account in p",
        "Alice's anchor names a roster whose p is Bob. No chain.",
        anchor(A, bad, T0 + 1),
        [bad],
        [],
    )
    bad = roster(P, "bob", 2, r1, LB1, [P, L], T0 + 100)
    case(
        "a roster with another account in p",
        "Roster 2 has p = Bob.",
        a1,
        [r1, bad],
        [r1],
    )
    bad = roster(P, A, 1, r1, LB1, [P], T0 + 5)
    case(
        "a root with a prev",
        "The anchor names a roster with n = 1 and a prev. No chain.",
        anchor(A, bad, T0 + 6),
        [r1, bad],
        [],
    )
    rr = roster(P, A, 2, r1, LB1, [P], T0 + 50, ready=True)
    rr3 = roster(P, A, 3, rr, LB1, [P, L], T0 + 100)
    case(
        "ready, then a roster without it: still ready",
        "Roster 2 has ready; roster 3 adds a device and has no ready tag.",
        a1,
        [r1, rr, rr3],
        [r1, rr, rr3],
        ready=True,
    )
    bad = {**r2, "sig": r2["sig"][:-2] + ("00" if r2["sig"][-2:] != "00" else "01")}
    case("a bad signature", "Roster 2's signature was changed.", a1, [r1, bad], [r1])
    bad = {**r2, "created_at": r2["created_at"] + 1}
    case(
        "an id that is not the event's hash",
        "Roster 2's created_at was changed after signing: its id is no longer its hash.",
        a1,
        [r1, bad],
        [r1],
    )
    case(
        "an anchor with a bad signature",
        "No chain.",
        {**a1, "sig": a1["sig"][:-2] + ("00" if a1["sig"][-2:] != "00" else "01")},
        [r1],
        [],
    )
    bad = roster(P, A, 3, r1, LB1, [P, L], T0 + 100)
    case("n skips a number", "Roster after 1 says n = 3.", a1, [r1, bad], [r1])
    bad = roster(P, A, 2, r1, LB1, [P, L, L], T0 + 100)
    case("a device listed twice", "Roster 2 lists the laptop twice.", a1, [r1, bad], [r1])
    many = [sealed.pubkey_of(h(f"key/extra-device-{i}")) for i in range(20)]
    ok20 = roster(P, A, 2, r1, LB1, [P, *many[:19]], T0 + 100)
    case("twenty devices", "The most a roster holds.", a1, [r1, ok20], [r1, ok20])
    bad = roster(P, A, 2, r1, LB1, [P, *many], T0 + 100)
    case("more than twenty devices", "Roster 2 lists 21.", a1, [r1, bad], [r1])
    bad = roster(P, A, 3, r2, LB3, [], T0 + 200)
    case("no device left", "Roster 3 lists no device.", a1, [r1, r2, bad], [r1, r2])
    leave = roster(L, A, 3, r2, LB2, [P], T0 + 200)
    case(
        "a device removes itself",
        "The laptop signs roster 3 without the laptop: valid, the signer need not stay.",
        a1,
        [r1, r2, leave],
        [r1, r2, leave],
    )
    # the anchor names the head: a dropped device cannot take the chain back
    at = T0 + 200
    while True:  # the laptop, dropped by roster 3, forks from roster 2 with a LOWER id
        evil = roster(L, A, 3, r2, LB3, [L], at)
        if evil["id"] < r3["id"]:
            break
        at += 1
    case(
        "a dropped device forks before its removal: ignored, the anchor names the real head",
        "Roster 3 (by the phone) drops the laptop. The laptop signs another roster 3 on roster "
        "2 that drops the phone instead; it is a valid step and has the LOWER id. The anchor's "
        "head is the phone's roster 3: the laptop's is not on the anchored path and does not "
        "descend from its end, so it is ignored whatever its id.",
        ah(r3),
        [r1, r2, r3, evil],
        [r1, r2, r3],
    )
    case(
        "the same fork with no head in the anchor yet: the lower id wins for now",
        "The same rosters under an anchor with no head (= the root): nothing after the root is "
        "anchored, so between the two rosters 3 the lower id wins provisionally, here the "
        "laptop's. This lasts until the phone's anchor (it anchors its own roster at once, "
        "§3.3), which gives the case above.",
        a1,
        [r1, r2, r3, evil],
        [r1, r2, evil],
        anchored=False,
    )
    case(
        "an unanchored tail after the anchored head",
        "The anchor's head is roster 2; roster 3 is published and not anchored yet: accepted.",
        ah(r2),
        [r1, r2, r3],
        [r1, r2, r3],
    )
    case(
        "the anchor's head does not descend from the root",
        "The head is a roster of another root: the anchored path is the root alone. What "
        "follows the root is then taken as an unanchored tail (roster 2).",
        anchor(A, r1, T0 + 600, other_root),
        [r1, r2, other_root],
        [r1, r2],
    )
    case(
        "the anchor's head is not there",
        "The head names an id no roster has: the root alone, then the unanchored tail.",
        anchor(A, r1, T0 + 600, "ab" * 32),
        [r1, r2],
        [r1, r2],
    )
    invalid3 = roster(P, A, 3, r2, LB1, [P], T0 + 200)
    case(
        "the anchor's head is an invalid roster",
        "The head is a roster 3 that drops the laptop and keeps the letterbox (invalid): the "
        "anchored path is the root alone; roster 2 follows as an unanchored tail and the "
        "invalid roster is ignored.",
        anchor(A, r1, T0 + 600, invalid3),
        [r1, r2, invalid3],
        [r1, r2],
    )
    case(
        "pinned head: an anchor that goes back to an ancestor is ignored",
        "This client accepted roster 3 (the phone's) as head. The anchor now names roster 2, an "
        "ancestor of it, and the laptop's lower-id roster 3 is there: without the pin the "
        "laptop's would win for now; with it the client keeps its head.",
        ah(r2),
        [r1, r2, r3, evil],
        [r1, r2, r3],
        pinned=r3,
    )
    case(
        "pinned head: an anchor with no head is going back too",
        "The same with an anchor that has no head (= the root, an ancestor of the pinned head).",
        a1,
        [r1, r2, r3, evil],
        [r1, r2, r3],
        anchored=False,
        pinned=r3,
    )
    case(
        "pinned head: an anchor on another branch of the same root is followed",
        "This client had accepted the laptop's roster 3 (the lower id, before any anchor). The "
        "anchor now names the phone's roster 3, another branch: followed.",
        ah(r3),
        [r1, r2, r3, evil],
        [r1, r2, r3],
        pinned=evil,
    )
    case(
        "pinned head: the anchor moved forward",
        "Pinned roster 2; the anchor names roster 3, which descends from it: followed.",
        ah(r3),
        [r1, r2, r3],
        [r1, r2, r3],
        pinned=r2,
    )
    case(
        "pinned head: a pin this client cannot find is no pin",
        "The pinned id is not among the rosters (or is of another root): the anchor decides.",
        ah(r2),
        [r1, r2],
        [r1, r2],
        pinned={"id": "cd" * 32},
    )
    return out, {"a1": ah(r3), "r1": r1, "r2": r2, "r3": r3}


# ── wraps ───────────────────────────────────────────────────────────────────────────────────
ALICE_CHAIN = "a device is removed, with a new letterbox"


def rumor(author: str, to: str, kind: int, content: str, at: int, tags=None) -> dict[str, Any]:
    return {
        "pubkey": PUB[author],
        "created_at": at,
        "kind": kind,
        "tags": [["p", PUB[to]], *(tags or [])],
        "content": content,
    }


def wraps(ev: dict[str, Any]) -> list[dict[str, Any]]:
    alice = sealed.chain(ev["a1"], [ev["r1"], ev["r2"], ev["r3"]])
    bob_root = roster("bob_phone", "bob", 1, None, "bob_lb1", ["bob_phone"], T0)
    bob_anchor = anchor("bob", bob_root, T0 + 1)
    bob = sealed.chain(bob_anchor, [bob_root])
    out: list[dict[str, Any]] = []

    def case(name, note, wrap, lb, chain, opens, accepted=None, seal_by=None, inner=None, **more):
        got: dict[str, Any] = {"opens": True}
        try:
            seal, opened = sealed.open_sealed(SECRET[lb], wrap)
            got |= {
                "seal_pubkey": seal["pubkey"],
                "rumor": opened,
                "accepted": sealed.accept(chain, seal, opened, PUB["belfry"]),
            }
        except ValueError:
            got = {"opens": False}
        want: dict[str, Any] = {"opens": opens}
        if opens:
            want |= {
                "seal_pubkey": PUB[seal_by],
                "rumor": sealed.with_id(inner),
                "accepted": accepted,
            }
        agree(name, got, want)
        out.append(
            {
                "name": name,
                "note": note,
                "wrap": wrap,
                "letterbox": lb,
                "letterbox_secret": SECRET[lb].hex(),
                "author_chain": more.get("chain_ref"),
                **({"author_chain_events": more["events"]} if "events" in more else {}),
                "belfry": PUB["belfry"],
                "expected": want,
            }
        )

    def sealed_by(name, signer, inner, account, lb):
        return sealed.seal_to_letterbox(
            SECRET[signer], inner, PUB[account], PUB[lb], **fixed(name, T0 + 1000, T0 + 1001)
        )

    name = "a sealed kind 14"
    hello = rumor("alice", "bob", 14, "Hello Bob", T0 + 900)
    first = sealed_by(name, "alice_phone", hello, "bob", "bob_lb1")
    case(
        name,
        "Alice's phone seals a message to Bob's letterbox. The seal's signer is in the head of "
        "Alice's chain.",
        first,
        "bob_lb1",
        alice,
        True,
        True,
        "alice_phone",
        hello,
        chain_ref=ALICE_CHAIN,
    )
    case(
        "the wrong letterbox secret",
        "The same wrap and a secret that is not its lb tag's: it does not open (locked).",
        first,
        "alice_lb3",
        alice,
        False,
        chain_ref=ALICE_CHAIN,
    )
    name = "the seal's signer is not a device of the author"
    forged = rumor("alice", "bob", 14, "Send me the keys", T0 + 900)
    case(
        name,
        "Mallory's phone seals a rumor that says it is Alice's: it opens and is not accepted.",
        sealed_by(name, "mallory_phone", forged, "bob", "bob_lb1"),
        "bob_lb1",
        alice,
        True,
        False,
        "mallory_phone",
        forged,
        chain_ref=ALICE_CHAIN,
    )
    for when, at, ok in (("before", 150, True), ("in the same second as", 200, True)):
        name = f"a dropped device, a rumor dated {when} the roster that dropped it"
        r = rumor("alice", "bob", 14, "From the laptop", T0 + at)
        case(
            name,
            f"The laptop left in roster 3 (created_at T0+200); the rumor is dated T0+{at}.",
            sealed_by(name, "alice_laptop", r, "bob", "bob_lb1"),
            "bob_lb1",
            alice,
            True,
            ok,
            "alice_laptop",
            r,
            chain_ref=ALICE_CHAIN,
        )
    name = "a dropped device, a rumor dated after the roster that dropped it"
    r = rumor("alice", "bob", 14, "From the laptop, later", T0 + 201)
    case(
        name,
        "One second after roster 3: not accepted.",
        sealed_by(name, "alice_laptop", r, "bob", "bob_lb1"),
        "bob_lb1",
        alice,
        True,
        False,
        "alice_laptop",
        r,
        chain_ref=ALICE_CHAIN,
    )
    name = "a mail sealed by the belfry"
    m = {
        "pubkey": PUB["belfry"],
        "created_at": T0 + 950,
        "kind": 1301,
        "tags": [
            ["email-id", "0123456789abcdef0123456789abcdef"],
            ["mail-from", "carol@example.org"],
            ["p", PUB["alice"]],
            ["subject", "Lunch"],
        ],
        "content": "From: Carol <carol@example.org>\r\nSubject: Lunch\r\n\r\nNoon?\r\n",
    }
    case(
        name,
        "§7: a kind 1301 whose author and seal signer are the belfry's key, to Alice's head "
        "letterbox. The belfry has no chain.",
        sealed_by(name, "belfry", m, "alice", "alice_lb2"),
        "alice_lb2",
        None,
        True,
        True,
        "belfry",
        m,
    )
    name = "the belfry's key does not speak for a member"
    r = rumor("bob", "alice", 14, "Not really Bob", T0 + 950)
    case(
        name,
        "The belfry's key seals a rumor whose author is Bob: not accepted.",
        sealed_by(name, "belfry", r, "alice", "alice_lb2"),
        "alice_lb2",
        bob,
        True,
        False,
        "belfry",
        r,
        chain_ref=None,
        events={"anchor": bob_anchor, "rosters": [bob_root]},
    )
    name = "a wrap to an earlier letterbox still opens"
    r = rumor("bob", "alice", 14, "Sent before your key changed", T0 + 150)
    case(
        name,
        "§9: sealed to alice_lb1, which is no longer Alice's head; a device that kept the secret "
        "opens it.",
        sealed_by(name, "bob_phone", r, "alice", "alice_lb1"),
        "alice_lb1",
        bob,
        True,
        True,
        "bob_phone",
        r,
        chain_ref=None,
        events={"anchor": bob_anchor, "rosters": [bob_root]},
    )
    # an id and a sig inside the seal are ignored: built by hand, the helper would correct them
    name = "an id and a sig inside the seal are ignored"
    r = rumor("alice", "bob", 14, "My id is wrong", T0 + 900)
    inside = {**r, "id": "00" * 32, "sig": "11" * 64}
    f = fixed(name, T0 + 1000, T0 + 1001)
    lb = bytes.fromhex(PUB["bob_lb1"])
    seal = sealed.sign(
        SECRET["alice_phone"],
        {
            "kind": 13,
            "created_at": f["seal_at"],
            "tags": [],
            "content": nip44_encrypt(
                conversation_key(SECRET["alice_phone"], lb),
                sealed.rumor_json(inside),
                f["seal_nonce"],
            ),
        },
        AUX,
    )

    def wrapped(seal_event, key):
        return sealed.sign(
            f["one_time"],
            {
                "kind": 1059,
                "created_at": f["wrap_at"],
                "tags": [["p", PUB["bob"]], ["lb", PUB["bob_lb1"]]],
                "content": nip44_encrypt(
                    conversation_key(f["one_time"], lb),
                    json.dumps(seal_event, separators=(",", ":")),
                    h("nonce/wrap/" + key),
                ),
            },
            AUX,
        )

    case(
        name,
        "The rumor inside carries an id of zeros and a sig: the opener computes the id itself.",
        wrapped(seal, name),
        "bob_lb1",
        alice,
        True,
        True,
        "alice_phone",
        r,
        chain_ref=ALICE_CHAIN,
    )
    name = "a seal with a bad signature"
    broken = {**seal, "sig": seal["sig"][:-2] + ("00" if seal["sig"][-2:] != "00" else "01")}
    case(
        name,
        "The wrap is fine, the seal inside is not signed by its pubkey: it does not open.",
        wrapped(broken, name),
        "bob_lb1",
        alice,
        False,
        chain_ref=ALICE_CHAIN,
    )
    name = "the belfry's key seals a kind 14 of its own"
    r = rumor("belfry", "alice", 14, "A message from the belfry itself", T0 + 950)
    case(
        name,
        "Author and seal signer are both the belfry's key, but the rumor is a kind 14: the "
        "exception is for mail (kind 1301) only. Not accepted.",
        sealed_by(name, "belfry", r, "alice", "alice_lb2"),
        "alice_lb2",
        None,
        True,
        False,
        "belfry",
        r,
    )
    return out


# ── the size ceiling (§8) ───────────────────────────────────────────────────────────────────
def ceiling() -> dict[str, Any]:
    cases = []
    base = {"pubkey": PUB["alice"], "created_at": T0, "kind": 14, "tags": [["p", PUB["bob"]]]}
    empty = len(sealed.rumor_json(sealed.with_id({**base, "content": ""})).encode())
    for name, unit, total, fits in (
        ("exactly 40 000 bytes", "x", sealed.MAX_RUMOR, True),
        ("one byte over", "x", sealed.MAX_RUMOR + 1, False),
        ("bytes, not characters", "é", sealed.MAX_RUMOR, True),
        ("bytes, not characters: one letter more", "é", sealed.MAX_RUMOR + 2, False),
    ):
        times = (total - empty) // len(unit.encode())
        r = sealed.with_id({**base, "content": unit * times})
        size = len(sealed.rumor_json(r).encode())
        agree(name, (size, sealed.fits(r)), (total - (total - empty) % len(unit.encode()), fits))
        one: dict[str, Any] = {
            "name": name,
            "rumor": {**base, "content": {"repeat": unit, "times": times}},
            "rumor_id": r["id"],
            "rumor_json_bytes": size,
            "may_seal": fits,
        }
        if fits:
            w = sealed.seal_to_letterbox(
                SECRET["alice_phone"], r, PUB["bob"], PUB["bob_lb1"], **fixed(name, T0, T0)
            )
            seal, _ = sealed.open_sealed(SECRET["bob_lb1"], w)
            one["seal_content_chars"] = len(seal["content"])
            one["wrap_content_chars"] = len(w["content"])
            one["wrap_id"] = w["id"]
        else:
            try:
                sealed.seal_to_letterbox(SECRET["alice_phone"], r, PUB["bob"], PUB["bob_lb1"])
                raise SystemExit(f"{name}: sealed although over the ceiling")
            except ValueError:
                pass
        cases.append(one)
    # a kind 8564 is a sealed rumor too: packed until its own JSON would pass the ceiling
    shared = [
        sealed.with_id(
            {
                "pubkey": PUB["alice" if i % 2 else "bob"],
                "created_at": T0 + i,
                "kind": 14,
                "tags": [["p", PUB["bob" if i % 2 else "alice"]]],
                "content": f'Message {i}: "quoted" \\ and é',
            }
        )
        for i in range(300)
    ]
    big = sealed.with_id({**shared[150], "content": "x" * 39_700})
    packed = sealed.pack_history(
        PUB["bob"], PUB["alice"], [*shared[:150], big, *shared[150:]], T0 + 800
    )
    counts = [len(json.loads(p["content"])["rumors"]) for p in packed]
    sizes = [len(sealed.rumor_json(p).encode()) for p in packed]
    agree("packing: all but the big one", sum(counts), 300)
    agree("packing: under the ceiling", all(n <= sealed.MAX_RUMOR for n in sizes), True)
    agree("packing: the big one fits alone as a rumor", sealed.fits(big), True)
    # a hand-over too: a long key chain goes in several kind 8561 rumors
    many = [
        {"lb": sealed.pubkey_of(h(f"key/many-lb-{i}")), "secret": h(f"key/many-lb-{i}").hex()}
        for i in range(600)
    ]
    handed = sealed.pack_keys(PUB["alice_phone"], PUB["alice"], many, T0 + 600)
    key_counts = [len(json.loads(k["content"])["keys"]) for k in handed]
    key_sizes = [len(sealed.rumor_json(k).encode()) for k in handed]
    agree("keys packing: all of them, in order", sum(key_counts), 600)
    agree("keys packing: under the ceiling", all(n <= sealed.MAX_RUMOR for n in key_sizes), True)
    agree("keys packing: more than one", len(handed) > 1, True)
    agree(
        "keys packing: one short chain, one rumor",
        len(sealed.pack_keys(PUB["alice_phone"], PUB["alice"], many[:3], T0 + 600)),
        1,
    )
    keys_packing = {
        "name": "a key chain of 600 letterboxes",
        "note": "§5.5: the giver's whole chain in several kind 8561 rumors when one would pass "
        "the ceiling. Key i (0 to 599) has secret SHA-256('belfry sealed vectors/key/many-lb-' "
        "+ i) and lb = its public key, in that order. Each rumor is kind 8561, pubkey "
        "alice_phone, created_at T0+600, tags [['p', alice]], content "
        '{"v":1,"account":"<alice>","keys":[{"lb":"…","secret":"…"},…]} in compact JSON, '
        "filled until one more key would take the rumor's JSON past 40 000 bytes. Each is "
        "wrapped on its own to the receiving device.",
        "expected": {
            "keys_per_8561": key_counts,
            "json_bytes_per_8561": key_sizes,
            "ids_of_8561": [k["id"] for k in handed],
        },
    }
    packing = {
        "name": "300 short rumors and one that cannot be shared",
        "note": "Bob shares 301 rumors with Alice, oldest first: rumor i (0 to 299) is kind 14, "
        "created_at T0+i, from alice when i is odd and bob when even, one p tag (the other of "
        "the two), content 'Message <i>: \"quoted\" \\ and é' (a double quote, one backslash "
        "and a non-ASCII letter: each counts as it is escaped). After rumor 149 comes one more "
        "from bob with 39 700 'x' as content: it fits a sealed rumor of its own but no kind "
        "8564, whose JSON would pass the ceiling with it alone, so it is left out. Each 8564 is "
        "kind 8564, pubkey bob, created_at T0+800, tags [['p', alice]], content "
        '{"v":1,"rumors":[…]} with every rumor, and the 8564 itself, serialised as the '
        "'serialisation' note at the top says. 200 rumors are never reached here: with two "
        "64-character fields each, 200 rumors are already more than 40 000 bytes.",
        "expected": {
            "rumors_per_8564": counts,
            "json_bytes_per_8564": sizes,
            "ids_of_8564": [p["id"] for p in packed],
            "left_out": [big["id"]],
        },
    }
    return {
        "max_rumor_json_bytes": sealed.MAX_RUMOR,
        "history_packing": packing,
        "keys_packing": keys_packing,
        "note": "The bytes are those of the exact JSON string that is encrypted in the seal, as "
        "UTF-8, serialised as the 'serialisation' note at the top says. "
        "The content is described (a string repeated), not written out. "
        "For a rumor that fits, seal and wrap were made with the keys alice_phone → bob_lb1 and "
        "the fixed values of the case (see 'fixed'), so wrap_id can be reproduced; the lengths "
        "are those of the two NIP-44 payloads (base64 characters).",
        "cases": cases,
    }


# ── handing keys over (§5.5) ────────────────────────────────────────────────────────────────
def keys_handover(ev: dict[str, Any]) -> list[dict[str, Any]]:
    alice = sealed.chain(ev["a1"], [ev["r1"], ev["r2"], ev["r3"]])
    out = []
    k1 = {"lb": PUB["alice_lb1"], "secret": SECRET["alice_lb1"].hex()}
    k2 = {"lb": PUB["alice_lb2"], "secret": SECRET["alice_lb2"].hex()}
    wrong = {"lb": PUB["alice_lb3"], "secret": SECRET["alice_lb2"].hex()}
    for name, note, giver, keys, taken in (
        (
            "a hand-over from a device of the head",
            "The phone gives its whole key chain to the tablet.",
            "alice_phone",
            [k1, k2],
            [k1, k2],
        ),
        (
            "a secret that is not its lb's",
            "The second entry pairs alice_lb3 with another secret: that entry is left out, "
            "the first is taken.",
            "alice_phone",
            [k1, wrong],
            [k1],
        ),
        (
            "the giver is not in the chain",
            "Mallory's phone hands 'keys' to Alice's tablet: nothing is taken.",
            "mallory_phone",
            [k1, k2],
            [],
        ),
        (
            "the giver is a device of an earlier roster",
            "The laptop was dropped in roster 3 but is a device of roster 2: its old key chain "
            "is taken (any roster of the account's chains).",
            "alice_laptop",
            [k1],
            [k1],
        ),
    ):
        w = sealed.hand_keys(
            SECRET[giver],
            PUB["alice"],
            keys,
            PUB["alice_tablet"],
            rumor_at=T0 + 600,
            **fixed(name, T0 + 601, T0 + 602),
        )
        seal, opened = sealed.open_keys(SECRET["alice_tablet"], w)
        agree(name, sealed.accept_keys(PUB["alice"], [alice], seal, opened), taken)
        out.append(
            {
                "name": name,
                "note": note,
                "wrap": w,
                "device": "alice_tablet",
                "device_secret": SECRET["alice_tablet"].hex(),
                "account": PUB["alice"],
                "chain": ALICE_CHAIN,
                "expected": {"giver": PUB[giver], "rumor": opened, "keys": taken},
            }
        )
    return out


# ── the link proof (§5.2) ───────────────────────────────────────────────────────────────────
def link() -> list[dict[str, Any]]:
    out = []
    secret = h("link/1").hex()
    for name, note, device, used, ok in (
        (
            "a request with the link's proof",
            "link = hex HMAC-SHA256(key: the 32 bytes the link secret's 64 hex characters stand "
            "for, decoded, not the hex text; message: the 32 bytes "
            "of the device's public key).",
            "alice_tablet",
            secret,
            True,
        ),
        (
            "a proof made with another secret",
            "The request's link tag does not match the code this device showed.",
            "alice_tablet",
            h("link/2").hex(),
            False,
        ),
        (
            "a proof made for another device",
            "The tag was copied from the tablet's request into the laptop's.",
            "alice_laptop",
            None,
            False,
        ),
    ):
        proof = sealed.link_proof(used or secret, PUB[device if used else "alice_tablet"])
        request = sealed.sign(
            SECRET[device],
            {
                "kind": sealed.REQUEST,
                "created_at": T0 + 700,
                "tags": [["p", PUB["alice"]], ["link", proof]],
                "content": "",
            },
            AUX,
        )
        agree(name, sealed.link_ok(secret, request), ok)
        out.append(
            {
                "name": name,
                "note": note,
                "link_secret": secret,
                "request": request,
                "expected": {
                    "link_for_this_device": sealed.link_proof(secret, PUB[device]),
                    "valid": ok,
                },
            }
        )
    return out


# ── shared history (§8) ─────────────────────────────────────────────────────────────────────
def history() -> list[dict[str, Any]]:
    bob_root = roster("bob_phone", "bob", 1, None, "bob_lb1", ["bob_phone"], T0)
    bob_anchor = anchor("bob", bob_root, T0 + 1)
    bob = sealed.chain(bob_anchor, [bob_root])
    m1 = sealed.with_id(rumor("alice", "bob", 14, "Are you coming on Friday?", T0 + 10))
    m2 = sealed.with_id(rumor("bob", "alice", 14, "Yes, with the cake.", T0 + 20))
    foreign = sealed.with_id(rumor("mallory", "alice", 14, "Bob never saw this.", T0 + 30))
    elsewhere = sealed.with_id(rumor("bob", "mallory", 14, "Alice was not in this.", T0 + 40))
    letter = sealed.with_id(
        {
            "pubkey": PUB["bob"],
            "created_at": T0 + 50,
            "kind": 1301,
            "tags": [["p", PUB["alice"]], ["mail-from", "bob@belfry.test"]],
            "content": "From: Bob <bob@belfry.test>\r\nSubject: Cake\r\n\r\nRecipe attached.\r\n",
        }
    )
    like = sealed.with_id(rumor("alice", "bob", 7, "👍", T0 + 25, [["e", m2["id"]], ["k", "14"]]))
    unlike = sealed.with_id(rumor("alice", "bob", 5, "", T0 + 26, [["e", like["id"]]]))
    out = []
    for name, note, rumors, kept, to in (
        (
            "a conversation shared by one of its two people",
            "Bob shares his conversation with Alice to her new letterbox.",
            [m1, m2],
            [m1, m2],
            "alice",
        ),
        (
            "a reaction and its taking back are shared too",
            "Kinds 14, 7 and 5.",
            [m1, m2, like, unlike],
            [m1, m2, like, unlike],
            "alice",
        ),
        (
            "an e-mail inside is dropped",
            "The third rumor is a kind 1301 between the same two people: never taken from a "
            "share. The two messages are added.",
            [m1, m2, letter],
            [m1, m2],
            "alice",
        ),
        (
            "the share's p is not the receiver",
            "The kind 8564 says p = Mallory although it is sealed to Alice's letterbox: Alice "
            "takes nothing from it.",
            [m1, m2],
            [],
            "mallory",
        ),
        (
            "a rumor of a conversation the sharer is not in",
            "The third rumor is between Mallory and Alice: Bob is neither its author nor in its "
            "p tags. It is dropped; the two others are added.",
            [m1, m2, foreign],
            [m1, m2],
            "alice",
        ),
        (
            "a rumor of a conversation the receiver is not in",
            "The third rumor is between Bob and Mallory: Alice, who receives, is neither its "
            "author nor in its p tags. It is dropped; the two others are added.",
            [m1, m2, elsewhere],
            [m1, m2],
            "alice",
        ),
    ):
        inner = sealed.history_rumor(PUB["bob"], PUB[to], rumors, T0 + 800)
        w = sealed.seal_to_letterbox(
            SECRET["bob_phone"],
            inner,
            PUB["alice"],
            PUB["alice_lb3"],
            **fixed(name, T0 + 801, T0 + 802),
        )
        seal, opened = sealed.open_sealed(SECRET["alice_lb3"], w)
        agree(name + " (accepted)", sealed.accept(bob, seal, opened), True)
        agree(
            name,
            [r["id"] for r in sealed.shared_rumors(opened, PUB["alice"])],
            [r["id"] for r in kept],
        )
        out.append(
            {
                "name": name,
                "note": note,
                "wrap": w,
                "letterbox": "alice_lb3",
                "letterbox_secret": SECRET["alice_lb3"].hex(),
                "receiver": PUB["alice"],
                "sharer_chain_events": {"anchor": bob_anchor, "rosters": [bob_root]},
                "expected": {
                    "sharer": PUB["bob"],
                    "seal_pubkey": PUB["bob_phone"],
                    "added_ids": [r["id"] for r in kept],
                },
            }
        )
    return out


# ── when a roster may say ready (§6) ────────────────────────────────────────────────────────
def iso(t: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ready() -> dict[str, Any]:
    now = T0 + 40 * DAY
    P, L, T = PUB["alice_phone"], PUB["alice_laptop"], PUB["alice_tablet"]

    def session(
        device: str | None,
        chat: bool,
        ago: float | None,
        name: str,
        made: float = 35 * DAY,
    ) -> dict:
        """AGO, MADE: seconds before now of last_activity (None: never active) and created_at."""
        return {
            "application_name": name,
            "device": "",
            "client_pubkey": device,
            "chat": chat,
            "created_at": iso(int(now - made)),
            "last_activity": None if ago is None else iso(int(now - ago)),
        }

    phone = session(P, True, 600, "Belfry for iPhone")
    cases = []
    for name, note, sessions, devices, want in (
        (
            "every chat session is a device",
            "Two chat sessions, both in the roster.",
            [phone, session(L, True, 3 * DAY, "Chat")],
            [P, L],
            True,
        ),
        (
            "a chat session that is not a device",
            "The tablet signed in two days ago and is not in the roster yet.",
            [phone, session(T, True, 2 * DAY, "Chat", made=2 * DAY)],
            [P],
            False,
        ),
        (
            "another app's session never counts",
            "A session with chat false is not a device and does not hold ready back.",
            [phone, session(T, False, DAY, "Notes")],
            [P],
            True,
        ),
        (
            "a chat session idle for 31 days does not count",
            "The laptop was last active 31 days ago.",
            [phone, session(L, True, 31 * DAY, "Chat")],
            [P],
            True,
        ),
        (
            "a chat session active 29 days ago counts",
            "Still less than 30 days.",
            [phone, session(L, True, 29 * DAY, "Chat")],
            [P],
            False,
        ),
        (
            "exactly 30 days ago does not count",
            "last_activity is 30 × 86 400 s before now: not less than 30 days.",
            [phone, session(L, True, 30 * DAY, "Chat")],
            [P],
            True,
        ),
        (
            "one second less than 30 days ago counts",
            "last_activity is 30 × 86 400 − 1 s before now.",
            [phone, session(L, True, 30 * DAY - 1, "Chat")],
            [P],
            False,
        ),
        (
            "a chat session with no client key counts and is no device",
            "client_pubkey is null (a session not bound to a key yet), active yesterday.",
            [phone, session(None, True, DAY, "Chat")],
            [P],
            False,
        ),
        (
            "no counted session at all",
            "One session of another app and one chat session idle for 40 days: nothing counts, "
            "so nothing holds ready back.",
            [session(T, False, DAY, "Notes"), session(L, True, 40 * DAY, "Chat", made=90 * DAY)],
            [P],
            True,
        ),
        (
            "no session at all",
            "An empty list: ready is allowed.",
            [],
            [P],
            True,
        ),
        (
            "a session that was never active counts from its creation",
            "last_activity is null and created_at is 2 days ago: it counts, and the tablet is "
            "not in the roster.",
            [phone, session(T, True, None, "Chat", made=2 * DAY)],
            [P],
            False,
        ),
        (
            "a session never active and created 31 days ago does not count",
            "last_activity is null, created_at is 31 days ago.",
            [phone, session(T, True, None, "Chat", made=31 * DAY)],
            [P],
            True,
        ),
        (
            "last_activity is what counts when there is one",
            "Created 300 days ago, active an hour ago: it counts.",
            [phone, session(L, True, 3600, "Chat", made=300 * DAY)],
            [P],
            False,
        ),
    ):
        agree(name, sealed.may_be_ready(sessions, devices, now), want)
        cases.append(
            {
                "name": name,
                "note": note,
                "sessions": sessions,
                "roster_devices": devices,
                "expected": {"may_carry_ready": want},
            }
        )
    return {
        "now": now,
        "now_iso": iso(now),
        "note": "sessions = GET /api/me/sessions (only the fields that matter are shown). "
        "Counted = chat true and last active less than 30 × 86 400 s before now "
        "(last_activity, or created_at when last_activity is null). The roster may carry "
        "ready when every counted session's client_pubkey is one of its devices.",
        "cases": cases,
    }


# ── what a client remembers: the pin, and who is trusted after a switch (§3.3, §3.4, §5.5) ──
EXTRA = ("alice_new", "alice_extra")  # two more of alice's devices, for these sections only


def pins() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    for n in EXTRA:
        SECRET[n] = h("key/" + n)
        PUB[n] = sealed.pubkey_of(SECRET[n])
    A, P, L, T, N, X = "alice", "alice_phone", "alice_laptop", "alice_tablet", *EXTRA
    LB1, LB2, LB3 = "alice_lb1", "alice_lb2", "alice_lb3"
    r1 = roster(P, A, 1, None, LB1, [P], T0)
    r2 = roster(P, A, 2, r1, LB1, [P, L], T0 + 100)
    a3 = roster(P, A, 3, r2, LB1, [P, L, T], T0 + 200)  # the phone adds the tablet
    b3 = roster(L, A, 3, r2, LB1, [P, L, N], T0 + 210)  # the laptop adds a new device
    b4x = roster(P, A, 4, b3, LB1, [P, L, N, X], T0 + 300)
    b4k = roster(P, A, 4, b3, LB1, [P, L, N], T0 + 300, ready=True)
    b4n = roster(N, A, 4, b3, LB1, [P, L, N, X], T0 + 300)
    b4d = roster(P, A, 4, b3, LB2, [P, N], T0 + 300)
    b5r = roster(N, A, 5, b4d, LB2, [P, N, L], T0 + 400)
    b4early = roster(P, A, 4, b3, LB1, [P, L, N, X], T0 + 150)
    b4t = roster(N, A, 4, b3, LB1, [P, L, N, T], T0 + 300)
    a4late = roster(P, A, 4, a3, LB1, [P, L, T, X], T0 + 500)
    r3 = roster(P, A, 3, r2, LB2, [P], T0 + 200)  # the phone drops the laptop
    r4 = roster(P, A, 4, r3, LB2, [P, T], T0 + 250)
    at = T0 + 200
    while True:  # the dropped laptop's fork, with the lower id
        evil = roster(L, A, 3, r2, LB3, [L], at)
        if evil["id"] < r3["id"]:
            break
        at += 1
    other = roster(L, A, 1, None, LB3, [L], T0 + 500)
    a_none = anchor(A, r1, T0 + 1)

    def ah(head, at=None):
        return anchor(A, r1, head["created_at"] + 1 if at is None else at, head)

    a_other = anchor(A, other, T0 + 501, other)
    names = {PUB[n]: n for n in (P, L, T, N, X)}

    def walk(what, pin, steps):
        """Run STEPS from PIN. A step: (anchor, rosters, event, head, anchored, trusted names or
        None, switch head or None, devices allowed locally after it). What is expected is
        written by hand in the step; the pin as a whole is what the code gave."""
        out, c = [], None
        for i, (anch, rosters, event, head, anchored, trusted, switch, *allow) in enumerate(steps):
            c, new, got = sealed.follow(anch, rosters, pin)
            for d in allow[0] if allow else []:
                new = sealed.allow_locally(new, PUB[d])
            agree(
                f"{what}, step {i + 1}",
                (
                    got,
                    c.head.id if c else None,
                    new and new["head"],
                    new and new["anchored"],
                    new and new["trusted"],
                    new and new["switch_head"],
                ),
                (
                    event,
                    head and head["id"],
                    (new and new["head"]) if head is None else head["id"],
                    anchored,
                    None if trusted is None else sorted(PUB[t] for t in trusted),
                    switch and switch["id"],
                ),
            )
            one = {"anchor": anch, "rosters": rosters}
            if allow and allow[0]:
                one["allow_locally"] = [PUB[d] for d in allow[0]]
            out.append({**one, "expected": {"event": got, "pin": new, "chain": summary(c)}})
            pin = new
        return out, c, pin

    def stored(pin):
        """As a client reads it back after a restart."""
        return json.loads(json.dumps(pin))

    def pin_after(*steps):
        return stored(walk("making a pin", None, steps)[2])

    base = [r1, r2, a3]
    on_r2 = pin_after((ah(r2), [r1, r2], "none", r2, True, None, None))
    on_tail = pin_after((a_none, [r1, r2], "none", r2, False, None, None))
    on_a3 = pin_after((ah(a3), base, "none", a3, True, None, None))
    a3_as_tail = pin_after((ah(r2), base, "none", a3, False, None, None))
    on_r3 = pin_after((ah(r3), [r1, r2, r3], "none", r3, True, None, None))
    r4_as_tail = pin_after((ah(r3), [r1, r2, r3, r4], "none", r4, False, None, None))
    switched = pin_after(
        (ah(a3), base, "none", a3, True, None, None),
        (ah(b3), [*base, b3], "switch", b3, True, [P, L], b3),
    )

    follow_cases = []

    def fcase(name, note, pin, *steps):
        made, _, _ = walk(name, pin, steps)
        follow_cases.append({"name": name, "note": note, "pin": pin, "steps": made})

    fcase(
        "the first pin, on an anchored head",
        "No pin yet. The anchor names roster 2: root and head are pinned, the head seen anchored.",
        None,
        (ah(r2), [r1, r2], "none", r2, True, None, None),
    )
    fcase(
        "the first pin, on a head that is not anchored yet",
        "The anchor has no head (= the root); roster 2 follows as an unanchored tail. It is "
        "pinned as head, not seen anchored: anchored_head is the root.",
        None,
        (a_none, [r1, r2], "none", r2, False, None, None),
    )
    fcase(
        "nothing changed",
        "The same anchor and rosters again: the pin stays as it is.",
        on_r2,
        (ah(r2), [r1, r2], "none", r2, True, None, None),
    )
    fcase(
        "the head moved forward",
        "The anchor names roster 3, which descends from the pinned head.",
        on_r2,
        (ah(a3), base, "moved", a3, True, None, None),
    )
    fcase(
        "a pinned tail becomes anchored",
        "The pinned head was an unanchored tail; the anchor now names it. The head is the same "
        "(event none) and is now seen anchored.",
        on_tail,
        (ah(r2), [r1, r2], "none", r2, True, None, None),
    )
    fcase(
        "an anchor that goes back is ignored",
        "Pinned and seen anchored: roster 3. The anchor names roster 2 again: the client keeps "
        "roster 3 and its pin.",
        on_a3,
        (ah(r2, T0 + 900), base, "ignored_back", a3, True, None, None),
    )
    fcase(
        "an anchor behind a head that was never anchored is the usual state",
        "The pinned head is an unanchored tail and the anchor still names what is before it: "
        "the head is kept and nothing is told (event none, not ignored_back: the anchor is not "
        "behind anything this client saw anchored).",
        on_tail,
        (a_none, [r1, r2], "none", r2, False, None, None),
    )
    fcase(
        "a pinned tail is kept against a sibling with a lower id",
        "The pinned head is the phone's roster 3 (anchored); the anchor goes back to roster 2 "
        "and the dropped laptop's roster 3 has the lower id: without a pin it would win for "
        "now, with it the client keeps its head.",
        on_r3,
        (ah(r2, T0 + 900), [r1, r2, r3, evil], "ignored_back", r3, True, None, None),
    )
    fcase(
        "a provisional tail gives way silently",
        "The pinned head (the phone's roster 3) was only ever an unanchored tail after roster "
        "2. The anchor names the laptop's roster 3 on the same roster 2: followed, nothing told "
        "(moved), nobody distrusted.",
        a3_as_tail,
        (ah(b3), [*base, b3], "moved", b3, True, None, None),
    )
    fcase(
        "a switch: the anchor leaves a head seen anchored",
        "The pinned head (the phone's roster 3, with the tablet) was seen anchored. The anchor "
        "names the laptop's roster 3 (with a new device), another branch of the same root: "
        "followed and told like a fresh start. trusted = the devices of the head held before "
        "that the new head still has (phone, laptop); switch_head = the head followed now.",
        on_a3,
        (ah(b3), [*base, b3], "switch", b3, True, [P, L], b3),
    )
    fcase(
        "a switch away from an anchored head the pin had moved past",
        "Roster 3 was seen anchored; the pin then moved onto roster 4, an unanchored tail. The "
        "anchor names the dropped laptop's fork from roster 2, which leaves roster 3 too: a "
        "switch, although the pinned head itself was never anchored. Nobody is trusted: no "
        "device of the head held is in the new one. (A reading beyond the contract's letter, "
        "flagged to its author.)",
        r4_as_tail,
        (ah(evil), [r1, r2, r3, r4, evil], "switch", evil, True, [], evil),
    )
    fcase(
        "a fresh start",
        "The anchor names another root: a new pin, the root held before kept as prev_root, "
        "nobody distrusted (trusted null).",
        on_a3,
        (a_other, [*base, other], "fresh_start", other, True, None, None),
    )
    fcase(
        "a pin read back after a restart, then an older anchor",
        "The pin comes from storage (plain JSON). The anchor has no head at all: ignored, the "
        "pinned head stays.",
        stored(on_a3),
        (anchor(A, r1, T0 + 900), base, "ignored_back", a3, True, None, None),
    )
    fcase(
        "a pin read back after a restart, then a new root",
        "The stored pin is one from after a switch (trusted and switch_head set). The anchor "
        "names another root: fresh start, trusted and switch_head are cleared.",
        stored(switched),
        (a_other, [*base, b3, other], "fresh_start", other, True, None, None),
    )
    fcase(
        "going back while the pinned head's rosters are not there",
        "The anchor names an ancestor of the pinned head and the relay did not give the pinned "
        "head's roster: nothing to follow (no chain), the pin stays; never the older head.",
        on_a3,
        (ah(r2, T0 + 900), [r1, r2], "ignored_back", None, True, None, None),
    )
    fcase(
        "no anchor to follow",
        "The anchor's signature is bad: no chain, the pin stays as it is.",
        on_a3,
        (
            {**a_none, "sig": a_none["sig"][:-2] + ("00" if a_none["sig"][-2:] != "00" else "01")},
            base,
            "none",
            None,
            True,
            None,
            None,
        ),
    )
    fcase(
        "moved, then the tail is anchored, then on",
        "Three reads in a row from no pin: a head-less anchor with roster 2 as tail; the anchor "
        "names roster 2; the anchor names roster 3.",
        None,
        (a_none, [r1, r2], "none", r2, False, None, None),
        (ah(r2), [r1, r2], "none", r2, True, None, None),
        (ah(a3), base, "moved", a3, True, None, None),
    )

    trust_cases = []
    switch_step = (ah(b3), [*base, b3], "switch", b3, True, [P, L], b3)
    req = T0 + 2000  # a request made well after every roster here

    def tcase(name, note, pin, steps, asks):
        made, c, new = walk(name, pin, steps)
        shown = []
        for who, want, *more in asks:
            o = {"live": [P, L, T, N, X], "request_created_at": req, "last_handover_at": None}
            o |= more[0] if more else {}
            live = [PUB[d] for d in o["live"]]
            got = sealed.answer_without_prompt(
                c, new, PUB[who], live, o["request_created_at"], None, o["last_handover_at"]
            )
            agree(f"{name}: {who} asks", got, want)
            added = c.added.get(PUB[who]) if c else None
            shown.append(
                {
                    "device": PUB[who],
                    "device_name": who,
                    "live_chat_devices": live,
                    "request_created_at": o["request_created_at"],
                    "added_at": added.created_at if added else None,
                    "last_handover_at": o["last_handover_at"],
                    "expected": want,
                }
            )
        trust_cases.append({"name": name, "note": note, "pin": pin, "steps": made, "asks": shown})

    tcase(
        "no switch: every device of the head is answered",
        "trusted is null. The laptop, in the anchored head, live, asks after it was added.",
        on_a3,
        [(ah(a3), base, "none", a3, True, None, None)],
        [(L, True), (T, True), (N, False)],
    )
    tcase(
        "after a switch the newcomer goes through Allow",
        "The new device of the head followed at the switch was not in the head held before.",
        on_a3,
        [switch_step],
        [(N, False)],
    )
    tcase(
        "after a switch a device held before is answered",
        "Phone and laptop were in the head held before and are in the new one.",
        on_a3,
        [switch_step],
        [(P, True), (L, True)],
    )
    tcase(
        "a trusted device adds a device: trusted",
        "After the switch the phone (trusted) signs a roster that ADDS one more device: that "
        "device is trusted. The newcomer of the switch still is not.",
        on_a3,
        [switch_step, (ah(b4x), [*base, b3, b4x], "moved", b4x, True, [P, L, X], b3)],
        [(X, True), (N, False)],
    )
    tcase(
        "a trusted device's roster that only keeps the newcomer vouches for nothing",
        "After the switch the phone publishes a roster with the same devices (for ready).",
        on_a3,
        [switch_step, (ah(b4k), [*base, b3, b4k], "moved", b4k, True, [P, L], b3)],
        [(N, False), (P, True)],
    )
    tcase(
        "the untrusted newcomer adds a third device: neither is trusted",
        "After the switch the newcomer signs the roster that adds one more device.",
        on_a3,
        [switch_step, (ah(b4n), [*base, b3, b4n], "moved", b4n, True, [P, L], b3)],
        [(X, False), (N, False), (L, True)],
    )
    tcase(
        "dropped after the switch, added back by an untrusted signer: not trusted",
        "The phone drops the laptop (new letterbox); the newcomer then adds it back.",
        on_a3,
        [
            switch_step,
            (ah(b5r), [*base, b3, b4d, b5r], "moved", b5r, True, [P], b3),
        ],
        [(L, False), (P, True), (N, False)],
    )
    tcase(
        "allowed on this device: trusted",
        "After the switch the person taps Allow for the newcomer (allow_locally).",
        on_a3,
        [(ah(b3), [*base, b3], "switch", b3, True, [P, L, N], b3, [N])],
        [(N, True)],
    )
    tcase(
        "chain order, not dates: a descendant dated before the switch head counts",
        "The phone's roster that adds a device descends from the switch head but is dated "
        "before it (T0+150, the switch head T0+210): it counts.",
        on_a3,
        [switch_step, (ah(b4early), [*base, b3, b4early], "moved", b4early, True, [P, L, X], b3)],
        [(X, True)],
    )
    tcase(
        "chain order, not dates: a later roster that does not descend from it does not",
        "On the branch the anchor left, the phone (trusted) adds the same device, dated later "
        "(T0+500). On the followed chain it is the newcomer that adds it. Only the followed "
        "chain counts: not trusted.",
        on_a3,
        [switch_step, (ah(b4n), [*base, b3, b4n, a4late], "moved", b4n, True, [P, L], b3)],
        [(X, False)],
    )
    tcase(
        "a device the switch left out comes back untrusted",
        "The tablet was in the head held before, but not in the head followed at the switch: "
        "it is not kept as trusted, so the newcomer adding it back gives it nothing. (trusted "
        "at a switch = the devices held before that the new head still has; a reading, flagged "
        "to the contract's author.)",
        on_a3,
        [switch_step, (ah(b4t), [*base, b3, b4t], "moved", b4t, True, [P, L], b3)],
        [(T, False)],
    )
    tcase(
        "trusted, but added past the anchored end: not yet",
        "The phone's roster adding a device is published and not anchored (the anchor still "
        "names the switch head): the device is trusted and in the head, but not on the anchored "
        "end.",
        on_a3,
        [switch_step, (ah(b3), [*base, b3, b4x], "moved", b4x, False, [P, L, X], b3)],
        [(X, False), (P, True)],
    )
    tcase(
        "the request must be newer, and the session live",
        "The laptop was added by roster 2 (T0+100). A request dated then is not newer; one "
        "dated at the last hand-over to it is not newer either; a later one is answered; no "
        "live chat session: not answered.",
        on_a3,
        [(ah(a3), base, "none", a3, True, None, None)],
        [
            (L, False, {"request_created_at": T0 + 100}),
            (L, True, {"request_created_at": T0 + 101}),
            (L, False, {"request_created_at": T0 + 1500, "last_handover_at": T0 + 1500}),
            (L, True, {"request_created_at": T0 + 1501, "last_handover_at": T0 + 1500}),
            (L, False, {"live": [P, T]}),
        ],
    )
    del names
    return follow_cases, trust_cases


# ── the delivery report of a mail (§7) ──────────────────────────────────────────────────────
def reports() -> dict[str, Any]:
    from belfry_onboarding import mail

    cases = []
    lb1 = {"lb": PUB["alice_lb1"], "secret": SECRET["alice_lb1"].hex()}
    lb2 = {"lb": PUB["alice_lb2"], "secret": SECRET["alice_lb2"].hex()}
    lb3 = {"lb": PUB["alice_lb3"], "secret": SECRET["alice_lb3"].hex()}
    now = T0 + 960
    body = {
        "status": "delivered",
        "recipient": "carol@example.org",
        "smtp_code": 250,
        "smtp_response": "OK",
        "attempted_at": now,
        "delivered_at": now,
        "details": "OK",
    }

    def made(name, lb):
        return mail.report_event(
            SECRET["belfry"],
            PUB["alice"],
            PUB[lb] if lb else "",
            "0123456789abcdef0123456789abcdef",
            ["carol@example.org"],
            250,
            "OK",
            now,
            temp=h("once/" + name),
            nonce=h("nonce/report/" + name),
            aux=AUX,
        )

    first = "a report to the sender's head letterbox"
    sealed_report = made(first, "alice_lb2")
    for name, note, report, chain, want in (
        (
            first,
            "§7: Alice's chain is ready, so the report of her mail is encrypted to her head "
            "letterbox (alice_lb2) and says so in an lb tag. Her device has that secret.",
            sealed_report,
            [lb1, lb2],
            {"state": "opened", "body": body},
        ),
        (
            "the wrong letterbox secret",
            "The same report; the key chain pairs alice_lb2 with another secret (alice_lb3's): "
            "it does not open.",
            sealed_report,
            [{"lb": PUB["alice_lb2"], "secret": SECRET["alice_lb3"].hex()}],
            {"state": "error"},
        ),
        (
            "a report without lb is the signer's",
            "The old kind: encrypted to Alice's account key, no lb tag. The app hands it to the "
            "signer as before and never tries a letterbox secret on it.",
            made("a report without lb is the signer's", None),
            [lb1, lb2],
            {"state": "signer"},
        ),
        (
            "a report whose lb is not in the key chain",
            "The same sealed report on a device that holds only alice_lb1 and alice_lb3: kept, "
            "not opened, tried again when keys arrive.",
            sealed_report,
            [lb1, lb3],
            {"state": "locked"},
        ),
    ):
        try:
            state, opened = sealed.open_report(report, PUB["belfry"], chain)
            got = {"state": state} | ({"body": opened} if opened is not None else {})
        except ValueError:
            got = {"state": "error"}
        agree(name, got, want)
        cases.append(
            {"name": name, "note": note, "report": report, "key_chain": chain, "expected": want}
        )
    # the old kind opens with the account's secret (what the signer does), for completeness
    agree(
        "old kind, account key",
        sealed.read_report(SECRET["alice"], cases[2]["report"]),
        body,
    )
    return {
        "note": "The kind 7679 delivery report of a member's outgoing mail, signed by the belfry "
        "key, found by its r tag (the mail's email-id), no p tag. content = NIP-44 with "
        "conv(secret, the key in ephemeral-pubkey) of a JSON object. With an lb tag the secret "
        "is that letterbox's; without, the account's (the signer). 'key_chain' is what the "
        "device holds. expected.state: opened (with the body), signer (no lb: not the "
        "device's to open), locked (lb not in the key chain: kept), error (does not open with "
        "the secret held for its lb). The throwaway key is SHA-256('belfry sealed "
        "vectors/once/' + the first case's name) and the nonce SHA-256('belfry sealed "
        "vectors/nonce/report/' + that name); the third case uses its own name.",
        "belfry": PUB["belfry"],
        "account": PUB["alice"],
        "cases": cases,
    }


def pin_sections() -> dict[str, Any]:
    follow_cases, trust_cases = pins()
    shape = (
        "A case starts from 'pin' (null: none yet) and runs its 'steps' in order: each is one "
        "read (anchor, rosters) given to follow(anchor, rosters, pin) → (chain, pin', event); "
        "'allow_locally' lists devices the person then allows on this device. 'expected' holds "
        "the event, the pin after the step (with what was allowed) and the chain followed "
        "(same shape as in 'chains'; all null when there is none). A pin is plain JSON: root, "
        "head, path (roster ids root → head), anchored (the head was seen as the end of the "
        "anchored path), anchored_head (the furthest roster of the path seen so), trusted (null, "
        "or the sorted device keys answered without a prompt after a switch), switch_head, "
        "prev_root."
    )
    return {
        "follow": {
            "note": shape,
            "keys": {n: {"secret": SECRET[n].hex(), "pubkey": PUB[n]} for n in EXTRA},
            "cases": follow_cases,
        },
        "trust": {
            "note": "Who is answered without a prompt (§5.5), mostly after a switch (§3.3). "
            "Steps as in 'follow' (the keys alice_new and alice_extra are listed there). "
            "After the last step each of 'asks' is a device request: "
            "answer_without_prompt(chain, pin, device, live_chat_devices, request_created_at, "
            "added_at, last_handover_at). added_at is the created_at of the first roster of the "
            "followed chain that has the device (null: not in it).",
            "cases": trust_cases,
        },
    }


def build() -> dict[str, Any]:
    chain_cases, ev = chains()
    return {
        "about": "Vectors of sealed conversations, docs/sealed.md (version 1). Generated by "
        "services/onboarding/tools/sealed_vectors.py; do not edit by hand. Every key here is a "
        "test key made from its name. Compare ids and keys as lower-case hex; compare 'devices' "
        "in the order of the head roster's device tags. An opened rumor is compared on pubkey, "
        "created_at, kind, tags, content and the id the opener computes. Signatures were made "
        "with fixed auxiliary randomness and only need to verify.",
        "serialisation": "A sealed rumor is serialised (docs/sealed.md §4, §8) as compact JSON, no "
        "spaces, its keys in the order id, pubkey, created_at, kind, tags, content, non-ASCII "
        "characters and '/' not escaped. The 40 000-byte ceiling counts that exact string as "
        "UTF-8 bytes. Every rumor inside a seal here was written that way, and so were the "
        'rumors inside a kind 8564\'s content ({"v":1,"rumors":[…]}, compact as well). An '
        "opener never refuses a rumor for its size or its key order.",
        "fixed": {
            "T0": T0,
            "secret key of <name>": "SHA-256('belfry sealed vectors/key/' + name)",
            "one-time key of a wrap": "SHA-256('belfry sealed vectors/once/' + case name)",
            "NIP-44 nonce of the seal": "SHA-256('belfry sealed vectors/nonce/seal/' + case name)",
            "NIP-44 nonce of the wrap": "SHA-256('belfry sealed vectors/nonce/wrap/' + case name)",
            "Schnorr auxiliary randomness": "32 zero bytes",
        },
        "keys": {n: {"secret": SECRET[n].hex(), "pubkey": PUB[n]} for n in NAMES},
        "chains": chain_cases,
        "wraps": wraps(ev),
        "ceiling": ceiling(),
        "keys_handover": keys_handover(ev),
        "link": link(),
        "history": history(),
        "ready": ready(),
        **pin_sections(),
        "reports": reports(),
    }


def text() -> str:
    return json.dumps(build(), indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    made = text()
    if "--check" in argv:
        same = OUT.exists() and OUT.read_text() == made
        print("docs/sealed-vectors.json is current" if same else "docs/sealed-vectors.json differs")
        return 0 if same else 1
    OUT.write_text(made)
    print(f"wrote {OUT} ({len(made)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
