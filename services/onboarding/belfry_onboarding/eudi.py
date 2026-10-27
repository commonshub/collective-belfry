"""Certificates from the EUDI Wallet relying-party registrar (registry.serviceproviders.eudiw.dev,
or the Launchpad instance), so the EUDI reference wallet can tell who Belfry is.

The registrar issues two things for one registered relying party:

- a WRPAC (access certificate, ETSI TS 119 411-8): an X.509 certificate with its key, under a
  CA on the EU test trusted list (``WRPACProviders``). It signs the issuer metadata (signed
  metadata, OpenID4VCI 1.0 §12.2.2) and the verifier's request objects (``x509_hash`` client id).
- a WRPRC (registration certificate, ETSI TS 119 475): a signed JWT naming Belfry's entitlements
  (``Non_Q_EAA_Provider``, ``Service_Provider``), what it issues and what it asks for. It rides
  in the signed metadata as ``issuer_info`` (ETSI TS 119 472-3) and in every OpenID4VP request as
  ``verifier_info``; the wallet checks that it is bound to the WRPAC that signed the message.

``infra/scripts/eudi-registry-certs.sh`` converts the registrar's ``wrpac.p12`` and ``wrprc.jwt``
into the files read here (``EUDI_DIR``, ``infra/issuer/keys/eudi/`` mounted at ``/keys/eudi``):

- ``wrpac.jwk.json``: the WRPAC's private key as a JWK (P-256)
- ``wrpac.x5c.json``: the x5c chain, leaf first
- ``wrprc.jwt``: the registration certificate the door and the pass sign-in send
- ``wrprc-issuer.jwt`` (optional): the one for the issuer metadata, when registered separately

Without these files the issuer metadata is signed with Belfry's own issuer key and x5c (a
wallet that does not trust Belfry's CA refuses or warns), and the verifier keeps its
``x509_san_dns`` client id under Belfry's CA."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

log = logging.getLogger(__name__)

SIGNED_METADATA_TYP = "openidvci-issuer-metadata+jwt"
SIGNED_METADATA_TTL = 24 * 3600
REGISTRATION_CERT = "registration_cert"


@dataclass(frozen=True)
class RegistryCerts:
    jwk: dict[str, Any]  # the WRPAC's private key
    x5c: list[str]  # leaf first
    wrprc: str | None = None  # registration certificate for presentations (verifier_info)
    wrprc_issuer: str | None = None  # for the issuer metadata; falls back to wrprc

    @property
    def issuer_registration(self) -> str | None:
        return self.wrprc_issuer or self.wrprc


def load(directory: str) -> RegistryCerts | None:
    """The registrar's files from ``directory``; None when the WRPAC is not there."""
    if not directory:
        return None
    d = Path(directory)
    key, chain = d / "wrpac.jwk.json", d / "wrpac.x5c.json"
    if not (key.is_file() and chain.is_file()):
        return None

    def text(name: str) -> str | None:
        p = d / name
        return p.read_text().strip() or None if p.is_file() else None

    certs = RegistryCerts(
        jwk=json.loads(key.read_text()),
        x5c=json.loads(chain.read_text()),
        wrprc=text("wrprc.jwt"),
        wrprc_issuer=text("wrprc-issuer.jwt"),
    )
    log.info(
        "EUDI registrar certificates loaded: WRPAC %s, registration certificate %s",
        x509_hash(certs.x5c),
        "yes" if certs.wrprc or certs.wrprc_issuer else "no",
    )
    return certs


def x509_hash(x5c: list[str]) -> str:
    """OpenID4VP 1.0 §5.9.3: base64url SHA-256 of the leaf certificate's DER."""
    der = base64.b64decode(x5c[0])
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def x509_hash_client_id(x5c: list[str]) -> str:
    return "x509_hash:" + x509_hash(x5c)


def registration_info(cert: str | None) -> list[dict[str, str]] | None:
    """The ``issuer_info`` / ``verifier_info`` array: one registration certificate, no
    ``credential_ids`` (the reference wallet ignores an entry that names credentials)."""
    return [{"format": REGISTRATION_CERT, "data": cert}] if cert else None


def with_issuer_info(doc: dict[str, Any], certs: RegistryCerts | None) -> dict[str, Any]:
    info = registration_info(certs.issuer_registration) if certs else None
    return {**doc, "issuer_info": info} if info else doc


def sign_metadata(
    doc: dict[str, Any], jwk: dict[str, Any], x5c: list[str], now: int | None = None
) -> str:
    """Signed credential issuer metadata (OpenID4VCI 1.0 §12.2.2, served as
    ``application/jwt``): the metadata parameters as claims, ``sub`` = the credential issuer
    identifier, ``iat``, ``exp``, ES256 with the signing certificate chain in ``x5c``. A wallet
    that reads it takes these values instead of the JSON ones."""
    now = int(time.time()) if now is None else now
    issuer = doc["credential_issuer"]
    payload = {**doc, "iss": issuer, "sub": issuer, "iat": now, "exp": now + SIGNED_METADATA_TTL}
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    headers: dict[str, Any] = {"typ": SIGNED_METADATA_TYP, "x5c": x5c}
    if jwk.get("kid"):
        headers["kid"] = jwk["kid"]
    return jwt.encode(payload, key, algorithm="ES256", headers=headers)


def wants_jwt(accept: str | None) -> bool:
    """True when the client lists ``application/jwt`` in Accept (the EUDI wallet's
    RequireSigned sends only that, PreferSigned sends it first)."""
    return any(
        part.split(";", 1)[0].strip().lower() == "application/jwt"
        for part in (accept or "").split(",")
    )
