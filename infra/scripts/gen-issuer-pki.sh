#!/usr/bin/env bash
# Belfry's credential issuer keys (Phase 3): a small private CA and a P-256 leaf for the
# SD-JWT VC signer, plus the keys walt.id Issuer2 needs at runtime, written as the files
# docker-compose.yml mounts. Called by gen-secrets.sh; safe to re-run (existing keys are kept:
# rotating the issuer key invalidates every pass certificate signed with it, see
# infra/issuer/README.md).
#
# Output (infra/issuer/keys/, gitignored):
#   ca.key.pem, ca.crt.pem            Belfry Issuer CA (10 years)
#   issuer.key.pem, issuer.crt.pem    pass signer, SAN = ISSUER_SAN (default issuer.belfry.local)
#   issuer.jwk.json                   private JWK of the signer (kid = RFC 7638 thumbprint)
#   issuer.x5c.json                   ["<leaf b64 DER>", "<ca b64 DER>"], the x5c header
#   bridge.jwk.json                   private JWK that signs the pass-bridge ID tokens
#   verifier.key.pem, verifier.crt.pem, verifier.jwk.json, verifier.x5c.json
#                                     Verifier2's request-signing key, SAN = VERIFIER_SAN (the
#                                     x509_san_dns client id; default verifier.belfry.local)
#   ci-token.jwk.json                 Issuer2's access-token key (ciTokenKey)
# Rendered (gitignored, mounted with the rest of the config):
#   infra/issuer/config/issuer2-profiles.conf   from issuer2-profiles.conf.tmpl (issuer key + x5c chain)
#   infra/issuer/config/issuer-service.conf     from issuer-service.conf.tmpl (ciTokenKey)
#   infra/verifier/config/verifier-service.conf from verifier-service.conf.tmpl (signing key, x5c, client id)
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYS="$INFRA_DIR/issuer/keys"
# The SANs come from the environment or else from .env (prod-env.sh writes the public names there).
env_value() { [[ -f "$INFRA_DIR/.env" ]] && sed -n "s/^$1=\([^#[:space:]]*\).*/\1/p" "$INFRA_DIR/.env" | tail -1; }
ISSUER_SAN=${ISSUER_SAN:-$(env_value ISSUER_SAN)}
VERIFIER_SAN=${VERIFIER_SAN:-$(env_value VERIFIER_SAN)}
SAN=${ISSUER_SAN:-issuer.belfry.local}
# The verifier's x509_san_dns client id. walt.id requires a dotted DNS name (no "localhost");
# in production this is the verifier's public hostname.
VERIFIER_SAN=${VERIFIER_SAN:-verifier.belfry.local}
mkdir -p "$KEYS"
cd "$KEYS"

if [[ -f issuer.key.pem ]]; then
  echo "$KEYS/issuer.key.pem exists; keeping the issuer PKI (delete the directory to start over)."
else
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key.pem
  openssl req -x509 -new -key ca.key.pem -sha256 -days 3650 -subj "/O=Belfry/CN=Belfry Issuer CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt.pem
  openssl ecparam -name prime256v1 -genkey -noout -out issuer.key.pem
  openssl req -new -key issuer.key.pem -subj "/O=Belfry/CN=Belfry Village Pass Issuer" -out issuer.csr.pem
  cat > leaf.ext <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
subjectAltName=DNS:$SAN
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EXT
  openssl x509 -req -in issuer.csr.pem -CA ca.crt.pem -CAkey ca.key.pem -CAcreateserial -days 1095 -sha256 \
    -extfile leaf.ext -out issuer.crt.pem
  rm -f issuer.csr.pem leaf.ext ca.srl ca.crt.srl
  openssl ecparam -name prime256v1 -genkey -noout -out bridge.key.pem
  openssl ecparam -name prime256v1 -genkey -noout -out ci-token.key.pem
  chmod 600 ./*.key.pem
  echo "issuer PKI written to $KEYS (CA + leaf, SAN $SAN)"
fi

# JWKs and the x5c array (pure Python, no extra packages).
python3 - "$KEYS" <<'PY'
import base64, hashlib, json, subprocess, sys
keys = sys.argv[1]

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def jwk_from_pem(path):
    # openssl prints the public point (04 || X || Y) and the private scalar as hex
    out = subprocess.check_output(["openssl", "ec", "-in", path, "-text", "-noout"], stderr=subprocess.DEVNULL).decode()
    def hexblock(label):
        lines = out.split("\n"); i = next(k for k, l in enumerate(lines) if l.strip().startswith(label))
        hexs = ""
        for l in lines[i + 1:]:
            if not l.startswith("    "): break
            hexs += l.strip().replace(":", "")
        return bytes.fromhex(hexs)
    priv = hexblock("priv:").rjust(32, b"\0")[-32:]
    pub = hexblock("pub:")
    assert pub[0] == 4 and len(pub) == 65
    jwk = {"kty": "EC", "crv": "P-256", "x": b64url(pub[1:33]), "y": b64url(pub[33:65])}
    thumb = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True).encode()
    jwk["kid"] = b64url(hashlib.sha256(thumb).digest())
    jwk["d"] = b64url(priv)
    return jwk

def der_b64(pem_path):
    der = subprocess.check_output(["openssl", "x509", "-in", pem_path, "-outform", "DER"])
    return base64.b64encode(der).decode()

issuer = jwk_from_pem(f"{keys}/issuer.key.pem")
json.dump(issuer, open(f"{keys}/issuer.jwk.json", "w"), indent=1)
json.dump(jwk_from_pem(f"{keys}/bridge.key.pem"), open(f"{keys}/bridge.jwk.json", "w"), indent=1)
ci_token = jwk_from_pem(f"{keys}/ci-token.key.pem")
json.dump(ci_token, open(f"{keys}/ci-token.jwk.json", "w"), indent=1)
x5c = [der_b64(f"{keys}/issuer.crt.pem"), der_b64(f"{keys}/ca.crt.pem")]
json.dump(x5c, open(f"{keys}/issuer.x5c.json", "w"))

tmpl = open(f"{keys}/../config/issuer2-profiles.conf.tmpl").read()
hocon_jwk = "{ " + ", ".join(f'{k} = "{v}"' for k, v in issuer.items() if k != "kid") + " }"
pem_chain = ",\n  ".join('"""' + open(f"{keys}/{c}").read().strip() + '"""' for c in ("issuer.crt.pem", "ca.crt.pem"))
rendered = tmpl.replace("@@ISSUER_JWK@@", hocon_jwk).replace("@@X5CHAIN_PEM@@", pem_chain)
open(f"{keys}/../config/issuer2-profiles.conf", "w").write(rendered)
svc = open(f"{keys}/../config/issuer-service.conf.tmpl").read()
open(f"{keys}/../config/issuer-service.conf", "w").write(svc.replace("@@CI_TOKEN_JWK@@", json.dumps({k: ci_token[k] for k in ("kty", "crv", "x", "y", "d")})))
print("issuer kid", issuer["kid"], "- wrote keys/*.jwk.json, keys/issuer.x5c.json, config/issuer2-profiles.conf, config/issuer-service.conf")
PY
# Verifier request-signing certificate (same CA), created on demand so older key sets gain it.
if [[ ! -f verifier.key.pem ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out verifier.key.pem
  openssl req -new -key verifier.key.pem -subj "/O=Belfry/CN=Belfry Verifier" -out verifier.csr.pem
  cat > verifier.ext <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
subjectAltName=DNS:$VERIFIER_SAN
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EXT
  openssl x509 -req -in verifier.csr.pem -CA ca.crt.pem -CAkey ca.key.pem -CAcreateserial -days 1095 -sha256 \
    -extfile verifier.ext -out verifier.crt.pem 2>/dev/null
  rm -f verifier.csr.pem verifier.ext ca.crt.srl ca.srl
  chmod 600 verifier.key.pem
  echo "verifier request-signing certificate written (SAN $VERIFIER_SAN)"
fi
python3 - "$KEYS" "$VERIFIER_SAN" <<'PY2'
import base64, hashlib, json, subprocess, sys
keys, san = sys.argv[1:3]
def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwk_from_pem(path):
    out = subprocess.check_output(["openssl", "ec", "-in", path, "-text", "-noout"], stderr=subprocess.DEVNULL).decode()
    def hexblock(label):
        lines = out.split("\n"); i = next(k for k, l in enumerate(lines) if l.strip().startswith(label))
        hexs = ""
        for l in lines[i + 1:]:
            if not l.startswith("    "): break
            hexs += l.strip().replace(":", "")
        return bytes.fromhex(hexs)
    priv = hexblock("priv:").rjust(32, b"\0")[-32:]; pub = hexblock("pub:")
    jwk = {"kty": "EC", "crv": "P-256", "x": b64url(pub[1:33]), "y": b64url(pub[33:65])}
    thumb = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True).encode()
    jwk["kid"] = b64url(hashlib.sha256(thumb).digest()); jwk["d"] = b64url(priv)
    return jwk
def der_b64(p): return base64.b64encode(subprocess.check_output(["openssl", "x509", "-in", p, "-outform", "DER"])).decode()
jwk = jwk_from_pem(f"{keys}/verifier.key.pem")
json.dump(jwk, open(f"{keys}/verifier.jwk.json", "w"), indent=1)
x5c = [der_b64(f"{keys}/verifier.crt.pem"), der_b64(f"{keys}/ca.crt.pem")]
json.dump(x5c, open(f"{keys}/verifier.x5c.json", "w"))
tmpl = open(f"{keys}/../../verifier/config/verifier-service.conf.tmpl").read()
out = (tmpl.replace("@@VERIFIER_KEY@@", json.dumps({"type": "jwk", "jwk": jwk}))
           .replace("@@VERIFIER_X5C@@", json.dumps(x5c)).replace("@@VERIFIER_SAN@@", san))
open(f"{keys}/../../verifier/config/verifier-service.conf", "w").write(out)
print("verifier kid", jwk["kid"], "- wrote keys/verifier.jwk.json, keys/verifier.x5c.json, verifier/config/verifier-service.conf")
PY2
chmod 600 "$KEYS"/*.jwk.json
