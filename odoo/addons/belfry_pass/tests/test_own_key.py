"""Joining with a Nostr key of one's own: the proof check and the invite page."""

import json
import re
import secrets
import time

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import nostr_proof
from .common import ensure_chart_of_accounts


def make_key():
    """A secret key (int) and its x-only public key (hex)."""
    d = secrets.randbelow(nostr_proof._N - 1) + 1
    return d, '%064x' % nostr_proof._mul(nostr_proof._G, d)[0]


def schnorr_sign(d, msg):
    """BIP-340 signing (the tests' signer extension)."""
    n, g, tagged_hash = nostr_proof._N, nostr_proof._G, nostr_proof._tagged_hash
    point = nostr_proof._mul(g, d)
    d = d if point[1] % 2 == 0 else n - d
    aux = tagged_hash("BIP0340/aux", secrets.token_bytes(32))
    t = (d ^ int.from_bytes(aux, 'big')).to_bytes(32, 'big')
    px = point[0].to_bytes(32, 'big')
    k = int.from_bytes(tagged_hash("BIP0340/nonce", t + px + msg), 'big') % n
    r = nostr_proof._mul(g, k)
    k = k if r[1] % 2 == 0 else n - k
    rx = r[0].to_bytes(32, 'big')
    e = int.from_bytes(tagged_hash("BIP0340/challenge", rx + px + msg), 'big') % n
    return rx + ((k + e * d) % n).to_bytes(32, 'big')


def proof_for(d, pubkey, path, created_at=None, kind=nostr_proof.PROOF_KIND):
    event = {
        'pubkey': pubkey,
        'created_at': int(created_at or time.time()),
        'kind': kind,
        'tags': [['u', 'https://shop.example%s' % path], ['method', 'POST']],
        'content': "Join Open Source Village with this key",
    }
    event['id'] = nostr_proof.event_id(event)
    event['sig'] = schnorr_sign(d, bytes.fromhex(event['id'])).hex()
    return json.dumps(event)


@tagged('post_install', '-at_install', 'belfry')
class TestNostrProof(TransactionCase):

    def setUp(self):
        super().setUp()
        self.d, self.pubkey = make_key()
        self.path = '/belfry/invite/abc'

    def test_valid_proof(self):
        proof = proof_for(self.d, self.pubkey, self.path)
        self.assertEqual(nostr_proof.check_proof(proof, self.pubkey.upper(), self.path), self.pubkey)

    def test_refusals(self):
        other_d, other = make_key()
        cases = {
            'another page': (proof_for(self.d, self.pubkey, '/belfry/invite/xyz'), self.pubkey),
            'another key': (proof_for(other_d, other, self.path), self.pubkey),
            'too old': (proof_for(self.d, self.pubkey, self.path, time.time() - 3600), self.pubkey),
            'another kind': (proof_for(self.d, self.pubkey, self.path, kind=1), self.pubkey),
            'not json': ('{', self.pubkey),
            'not a key': (proof_for(self.d, self.pubkey, self.path), 'npub1xyz'),
        }
        for name, (proof, pubkey) in cases.items():
            with self.subTest(name), self.assertRaises(nostr_proof.ProofError):
                nostr_proof.check_proof(proof, pubkey, self.path)
        forged = json.loads(proof_for(self.d, self.pubkey, self.path))
        forged['sig'] = schnorr_sign(other_d, bytes.fromhex(forged['id'])).hex()
        with self.assertRaises(nostr_proof.ProofError):
            nostr_proof.check_proof(json.dumps(forged), self.pubkey, self.path)

    def test_npub(self):
        self.assertEqual(
            nostr_proof.npub('3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d'),
            'npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6',
        )


@tagged('post_install', '-at_install', 'belfry')
class TestInviteOwnKey(HttpCase):

    def setUp(self):
        super().setUp()
        ensure_chart_of_accounts(self.env)
        self.invite = self.env['belfry.invite'].create({'note': "own key"})
        self.path = '/belfry/invite/%s' % self.invite.token
        self.product = self.env.ref('belfry_pass.product_week')
        self.d, self.pubkey = make_key()

    def _post(self, **form):
        page = self.url_open(self.path)
        self.assertIn('belfry_own_key', page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        data = {
            'csrf_token': csrf, 'product_id': self.product.id,
            'name': "Nora Nostr", 'email': "nora@example.org", **form,
        }
        return self.url_open(self.path, data=data, allow_redirects=False)

    def test_own_key_is_stored_on_the_pass(self):
        proof = proof_for(self.d, self.pubkey, self.path)
        response = self._post(own_key='1', nostr_pubkey=self.pubkey, nostr_proof=proof)
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.invite.invalidate_recordset()
        belfry_pass = self.invite.pass_id
        self.assertEqual(belfry_pass.own_pubkey, self.pubkey)
        self.assertEqual(json.loads(belfry_pass.own_pubkey_proof)['pubkey'], self.pubkey)

    def test_without_own_key_nothing_is_stored(self):
        response = self._post(nostr_pubkey=self.pubkey, nostr_proof="ignored without the box")
        self.assertEqual(response.status_code, 303)
        self.invite.invalidate_recordset()
        self.assertFalse(self.invite.pass_id.own_pubkey)

    def test_a_bad_proof_is_refused_and_creates_nothing(self):
        other_d, _other = make_key()
        proof = proof_for(other_d, self.pubkey, self.path)
        response = self._post(own_key='1', nostr_pubkey=self.pubkey, nostr_proof=proof)
        self.assertEqual(response.status_code, 200)
        self.assertIn('belfry_form_error', response.text)
        self.assertFalse(self.env['belfry.pass'].search([('invite_id', '=', self.invite.id)]))
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')

    def test_a_key_of_another_participant_is_refused(self):
        self.env['res.partner'].create({
            'name': "Someone Else", 'email': "else@example.org",
            'belfry_npub': nostr_proof.npub(self.pubkey),
        })
        proof = proof_for(self.d, self.pubkey, self.path)
        response = self._post(own_key='1', nostr_pubkey=self.pubkey, nostr_proof=proof)
        self.assertEqual(response.status_code, 200)
        self.assertIn('belongs to another participant', response.text)
