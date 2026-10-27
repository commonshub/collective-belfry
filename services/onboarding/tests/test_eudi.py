"""The EUDI registrar's certificates: signed issuer metadata with issuer_info, verifier_info and
the x509_hash client id (eudi.py)."""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
import pytest

from belfry_onboarding import eudi
from belfry_onboarding.clients.verifier import VerifierClient

from .conftest import VERIFIER_URL, make_jwk

ISSUER = "http://localhost:7005/openid4vci"
LEAF = base64.b64encode(b"wrpac leaf der").decode()


def _issuer_meta(router):
    router.get("http://issuer.test/.well-known/openid-credential-issuer/openid4vci").respond(
        json={"credential_issuer": ISSUER, "credential_configurations_supported": {}}
    )


def _public(jwk):
    return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk)).public_key()


def _verify(token, jwk):
    claims = jwt.decode(token, _public(jwk), algorithms=["ES256"], options={"verify_aud": False})
    return jwt.get_unverified_header(token), claims


def test_load_reads_the_registrar_files(tmp_path):
    assert eudi.load("") is None
    assert eudi.load(str(tmp_path)) is None  # no WRPAC: Belfry's own certificates
    jwk = make_jwk()
    (tmp_path / "wrpac.jwk.json").write_text(json.dumps(jwk))
    (tmp_path / "wrpac.x5c.json").write_text(json.dumps([LEAF, "MIIBca"]))
    certs = eudi.load(str(tmp_path))
    assert certs is not None and certs.jwk == jwk and certs.x5c == [LEAF, "MIIBca"]
    assert certs.wrprc is None and certs.issuer_registration is None
    (tmp_path / "wrprc.jwt").write_text("a.b.c\n")
    assert eudi.load(str(tmp_path)).issuer_registration == "a.b.c"
    (tmp_path / "wrprc-issuer.jwt").write_text("i.s.s")
    certs = eudi.load(str(tmp_path))
    assert certs.wrprc == "a.b.c" and certs.issuer_registration == "i.s.s"


def test_x509_hash_is_the_sha256_of_the_leaf():
    digest = hashlib.sha256(b"wrpac leaf der").digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert eudi.x509_hash_client_id([LEAF, "MIIBca"]) == "x509_hash:" + expected


@pytest.mark.parametrize(
    "accept,signed",
    [
        ("application/jwt", True),
        ("application/jwt, application/json", True),
        ("application/json;q=1, Application/JWT;q=0.5", True),
        ("application/json", False),
        ("*/*", False),
        (None, False),
    ],
)
def test_wants_jwt(accept, signed):
    assert eudi.wants_jwt(accept) is signed


def test_metadata_is_signed_with_the_issuer_key_without_registrar_certificates(
    client, router, issuer_jwk
):
    _issuer_meta(router)
    plain = client.get("/issuer-metadata")
    assert plain.headers["content-type"].startswith("application/json")
    assert "issuer_info" not in plain.json() and plain.headers["vary"] == "Accept"

    r = client.get("/issuer-metadata", headers={"Accept": "application/jwt"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/jwt")
    header, claims = _verify(r.text, issuer_jwk)
    assert header["typ"] == "openidvci-issuer-metadata+jwt" and header["x5c"] == ["MIIBfake"]
    assert claims["sub"] == claims["iss"] == ISSUER
    assert claims["authorization_servers"] == [ISSUER]  # the JSON fix-ups are signed too
    assert claims["exp"] > claims["iat"]
    assert "issuer_info" not in claims


def test_metadata_with_registrar_certificates(client, router, services):
    wrpac = make_jwk()
    services.eudi = eudi.RegistryCerts(
        jwk=wrpac, x5c=[LEAF, "MIIBca"], wrprc="door.rc.jwt", wrprc_issuer="issuer.rc.jwt"
    )
    _issuer_meta(router)
    info = [{"format": "registration_cert", "data": "issuer.rc.jwt"}]
    assert client.get("/issuer-metadata").json()["issuer_info"] == info

    r = client.get("/issuer-metadata", headers={"Accept": "application/jwt, application/json"})
    header, claims = _verify(r.text, wrpac)
    assert header["x5c"] == [LEAF, "MIIBca"] and header["kid"] == wrpac["kid"]
    assert claims["issuer_info"] == info and claims["sub"] == ISSUER


def test_verifier_sessions_carry_the_registration_certificate(client, services, verifier):
    client.post("/door/sessions")
    assert "verifier_info" not in verifier.created[-1]["core_flow"]

    services.verifier = VerifierClient(
        VERIFIER_URL, verifier_info=eudi.registration_info("door.rc.jwt")
    )
    client.post("/door/sessions")
    core = verifier.created[-1]["core_flow"]
    assert core["verifier_info"] == [{"format": "registration_cert", "data": "door.rc.jwt"}]
    assert core["signed_request"] is True


def _cert_and_jwk():
    """A self-signed P-256 certificate (x5c entry) and its private JWK."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Belfry WRPAC test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    nums = key.private_numbers()

    def b(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b(nums.public_numbers.x),
        "y": b(nums.public_numbers.y),
        "d": b(nums.private_value),
    }
    return base64.b64encode(der).decode(), jwk


def test_test_wallet_verifies_signed_metadata_like_the_eudi_library(router):
    from belfry_onboarding.testwallet import HostMap, signed_metadata

    leaf, jwk = _cert_and_jwk()
    issuer = "https://issuer.example/openid4vci"
    doc = {"credential_issuer": issuer, "credential_configurations_supported": {"village_pass": {}}}
    doc = eudi.with_issuer_info(doc, eudi.RegistryCerts(jwk=jwk, x5c=[leaf], wrprc="r.c.jwt"))
    url = "https://issuer.example/.well-known/openid-credential-issuer/openid4vci"
    route = router.get(url)
    route.respond(
        headers={"Content-Type": "application/jwt"}, text=eudi.sign_metadata(doc, jwk, [leaf])
    )
    out = signed_metadata(issuer, HostMap(None))
    assert out["signed"] and out["sub"] == issuer and out["issuer_info"] == ["registration_cert"]
    assert "x509_hash:" + out["x509_hash"] == eudi.x509_hash_client_id([leaf])
    assert out["credential_configurations"] == ["village_pass"]
    # another issuer's metadata, or metadata signed with a key that is not the leaf's: refused
    with pytest.raises(SystemExit, match="is not the issuer"):
        signed_metadata("https://other.example/openid4vci", HostMap(None), url=url)
    _other_leaf, other_jwk = _cert_and_jwk()
    route.respond(
        headers={"Content-Type": "application/jwt"},
        text=eudi.sign_metadata(doc, other_jwk, [leaf]),
    )
    with pytest.raises(jwt.InvalidSignatureError):
        signed_metadata(issuer, HostMap(None))
    route.respond(json=doc)
    with pytest.raises(SystemExit, match="not signed"):
        signed_metadata(issuer, HostMap(None))
