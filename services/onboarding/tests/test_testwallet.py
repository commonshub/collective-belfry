"""The test wallet's encrypted OpenID4VP response (direct_post.jwt), decrypted here the way the
verifier does it: ECDH-ES with its own key and the JWE's epk, Concat KDF, AES-GCM."""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from belfry_onboarding.testwallet import _concat_kdf, b64url, b64url_decode, encrypt_response


def verifier_key() -> tuple[ec.EllipticCurvePrivateKey, dict]:
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
        "alg": "ECDH-ES",
        "use": "enc",
        "kid": "response-encryption-1",
    }
    return key, jwk


def decrypt(jwe: str, key: ec.EllipticCurvePrivateKey) -> tuple[dict, dict]:
    protected, encrypted_key, iv, ciphertext, tag = jwe.split(".")
    assert encrypted_key == ""
    header = json.loads(b64url_decode(protected))
    epk = header["epk"]
    sender = ec.EllipticCurvePublicNumbers(
        int.from_bytes(b64url_decode(epk["x"]), "big"),
        int.from_bytes(b64url_decode(epk["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    bits = {"A128GCM": 128, "A256GCM": 256}[header["enc"]]
    cek = _concat_kdf(key.exchange(ec.ECDH(), sender), header["enc"], bits)
    plain = AESGCM(cek).decrypt(
        b64url_decode(iv), b64url_decode(ciphertext) + b64url_decode(tag), protected.encode()
    )
    return header, json.loads(plain)


@pytest.mark.parametrize(
    ("supported", "enc"), [(["A128GCM", "A256GCM"], "A256GCM"), (["A128GCM"], "A128GCM")]
)
def test_encrypted_response_decrypts_with_the_verifier_key(supported, enc):
    key, jwk = verifier_key()
    payload = {"vp_token": {"village_pass": ["a~b~kb"]}, "state": "s1"}
    jwe = encrypt_response(
        payload, {"jwks": {"keys": [jwk]}, "encrypted_response_enc_values_supported": supported}
    )
    header, plain = decrypt(jwe, key)
    assert header["alg"] == "ECDH-ES" and header["enc"] == enc
    assert header["kid"] == "response-encryption-1"
    assert plain == payload


def test_encrypt_response_needs_a_p256_key():
    with pytest.raises(ValueError):
        encrypt_response({"vp_token": {}}, {"jwks": {"keys": []}})
