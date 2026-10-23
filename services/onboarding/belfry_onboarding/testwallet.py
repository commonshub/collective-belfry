"""A minimal SD-JWT VC holder for tests: receives a village pass over OpenID4VCI (pre-authorized
code, JWT proof with a fresh P-256 key) and presents it over OpenID4VP (DCQL, ``direct_post``,
KB-JWT). It stands in for an EUDI wallet in the smoke test; it is not a wallet.

    python -m belfry_onboarding.testwallet receive '<openid-credential-offer://...>' --out w.json
    python -m belfry_onboarding.testwallet present '<openid4vp://authorize?...>' --wallet w.json \\
        --disclose pass_tier,npub

``--map localhost:7005=issuer:7005`` rewrites hosts in URLs before connecting (the public URLs in
offers and requests stay as they are in signed values such as ``aud``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.parse
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key() -> tuple[str, dict[str, str]]:
    """(private key PEM, public JWK)."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    nums = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }
    return pem, jwk


class HostMap:
    def __init__(self, spec: str | None):
        self.map: dict[str, str] = {}
        for pair in (spec or "").split(","):
            if "=" in pair:
                src, dst = pair.split("=", 1)
                self.map[src.strip()] = dst.strip()

    def __call__(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.netloc in self.map:
            parts = parts._replace(netloc=self.map[parts.netloc])
        return urllib.parse.urlunsplit(parts)


def parse_offer(offer: str) -> dict[str, Any]:
    query = urllib.parse.urlsplit(offer).query
    params = urllib.parse.parse_qs(query)
    if "credential_offer" in params:
        return json.loads(params["credential_offer"][0])
    raise SystemExit("offer by reference (credential_offer_uri) is not supported by this holder")


def receive(offer: str, client_id: str, hostmap: HostMap) -> dict[str, Any]:
    data = parse_offer(offer)
    issuer = data["credential_issuer"]
    config_id = data["credential_configuration_ids"][0]
    grant = data["grants"]["urn:ietf:params:oauth:grant-type:pre-authorized_code"]
    code = grant["pre-authorized_code"]
    with httpx.Client(timeout=30) as http:
        meta = http.get(hostmap(f"{issuer}/.well-known/openid-credential-issuer"))
        if meta.status_code != 200:  # walt.id serves it under the openid4vci path segment
            base, _, tail = issuer.rpartition("/")
            meta = http.get(hostmap(f"{base}/.well-known/openid-credential-issuer/{tail}"))
        meta.raise_for_status()
        metadata = meta.json()
        token_endpoint = metadata.get("token_endpoint") or f"{issuer}/token"
        credential_endpoint = metadata["credential_endpoint"]
        nonce_endpoint = metadata.get("nonce_endpoint") or f"{issuer}/nonce"

        tok = http.post(
            hostmap(token_endpoint),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:pre-authorized_code",
                "pre-authorized_code": code,
                "client_id": client_id,
            },
        )
        tok.raise_for_status()
        access_token = tok.json()["access_token"]
        nonce = http.post(hostmap(nonce_endpoint)).json()["c_nonce"]

        pem, jwk = new_key()
        proof = jwt.encode(
            {"aud": issuer, "iat": int(time.time()), "nonce": nonce, "iss": client_id},
            pem,
            algorithm="ES256",
            headers={"typ": "openid4vci-proof+jwt", "jwk": jwk},
        )
        cred = http.post(
            hostmap(credential_endpoint),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"credential_configuration_id": config_id, "proofs": {"jwt": [proof]}},
        )
        if cred.status_code != 200:
            raise SystemExit(f"credential endpoint {cred.status_code}: {cred.text[:300]}")
        sd_jwt = cred.json()["credentials"][0]["credential"]
    return {"credential": sd_jwt, "holder_key_pem": pem, "holder_jwk": jwk, "issuer": issuer}


def split_sd_jwt(compact: str) -> tuple[str, list[str]]:
    parts = compact.split("~")
    return parts[0], [d for d in parts[1:] if d]


def decode_credential(compact: str) -> dict[str, Any]:
    """Issuer JWT header, payload and the disclosed claims (unverified; for display and tests)."""
    issuer_jwt, disclosures = split_sd_jwt(compact)
    header = json.loads(b64url_decode(issuer_jwt.split(".")[0]))
    payload = json.loads(b64url_decode(issuer_jwt.split(".")[1]))
    claims = {}
    for d in disclosures:
        salt, name, value = json.loads(b64url_decode(d))
        claims[name] = value
    return {"header": header, "payload": payload, "disclosed": claims}


def present(
    request_url: str, wallet: dict[str, Any], disclose: list[str], hostmap: HostMap
) -> dict[str, Any]:
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(request_url).query)
    with httpx.Client(timeout=30) as http:
        if "request_uri" in params:
            req = http.get(hostmap(params["request_uri"][0]))
            req.raise_for_status()
            body = req.text
            if body.count(".") == 2 and not body.startswith("{"):
                request = jwt.decode(body, options={"verify_signature": False})
            else:
                request = req.json()
        else:
            request = {k: v[0] for k, v in params.items()}
            if "dcql_query" in request:
                request["dcql_query"] = json.loads(request["dcql_query"])
        response_uri = request["response_uri"]
        nonce = request["nonce"]
        client_id = request["client_id"]
        state = request.get("state")
        dcql = request["dcql_query"]
        query_id = dcql["credentials"][0]["id"]
        requested = [
            c["path"][0] for c in dcql["credentials"][0].get("claims", []) if c.get("path")
        ]

        issuer_jwt, disclosures = split_sd_jwt(wallet["credential"])
        keep = []
        wanted = set(disclose) | set(requested)
        for d in disclosures:
            _salt, name, _value = json.loads(b64url_decode(d))
            if name in wanted:
                keep.append(d)
        presentation = "~".join([issuer_jwt, *keep]) + "~"
        sd_hash = b64url(hashlib.sha256(presentation.encode()).digest())
        kb = jwt.encode(
            {"iat": int(time.time()), "aud": client_id, "nonce": nonce, "sd_hash": sd_hash},
            wallet["holder_key_pem"],
            algorithm="ES256",
            headers={"typ": "kb+jwt"},
        )
        vp_token = json.dumps({query_id: [presentation + kb]})
        form = {"vp_token": vp_token}
        if state:
            form["state"] = state
        resp = http.post(hostmap(response_uri), data=form)
    return {"status": resp.status_code, "body": resp.text[:500], "disclosed": [d for d in keep]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="belfry_onboarding.testwallet")
    parser.add_argument(
        "--map", default=None, help="host rewrites, e.g. localhost:7005=issuer:7005"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("receive")
    r.add_argument("offer")
    r.add_argument("--out", required=True)
    r.add_argument("--client-id", default="belfry-test-wallet")
    p = sub.add_parser("present")
    p.add_argument("request_url")
    p.add_argument("--wallet", required=True)
    p.add_argument("--disclose", default="")
    s = sub.add_parser("show")
    s.add_argument("--wallet", required=True)
    args = parser.parse_args(argv)
    hostmap = HostMap(args.map)
    if args.command == "receive":
        wallet = receive(args.offer, args.client_id, hostmap)
        json.dump(wallet, open(args.out, "w"))
        json.dump(decode_credential(wallet["credential"]), sys.stdout)
    elif args.command == "present":
        wallet = json.load(open(args.wallet))
        disclose = [d for d in args.disclose.split(",") if d]
        json.dump(present(args.request_url, wallet, disclose, hostmap), sys.stdout)
    else:
        json.dump(decode_credential(json.load(open(args.wallet))["credential"]), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
