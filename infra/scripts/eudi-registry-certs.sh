#!/usr/bin/env bash
# Import Belfry's certificates from the EUDI Wallet relying-party registrar
# (registry.serviceproviders.eudiw.dev, or itb.registry.serviceproviders.eudiw.dev for the
# Launchpad tests; see infra/issuer/README.md "EUDI reference wallet"):
#
#   eudi-registry-certs.sh <wrpac.p12> <wrprc.jwt> [<wrprc-issuer.jwt>]
#
# wrpac.p12 is the access certificate with its key (password from WRPAC_P12_PASSWORD, else
# asked for); wrprc.jwt the registration certificate the door and the pass sign-in send as
# verifier_info; wrprc-issuer.jwt, if the issuer was registered separately, the one for the
# signed issuer metadata (issuer_info), else wrprc.jwt serves both.
#
# Writes infra/issuer/keys/eudi/ (gitignored, mounted into the onboarding service):
#   wrpac.key.pem, wrpac.crt.pem   the key and the leaf
#   wrpac.jwk.json                 the key as a private JWK (signs metadata and request objects)
#   wrpac.x5c.json                 leaf + intermediates (self-signed roots left out)
#   wrprc.jwt, wrprc-issuer.jwt    the registration certificates as given
# then re-renders the verifier config (gen-issuer-pki.sh: x509_hash client id). Afterwards:
#   docker compose up -d --force-recreate perms-init verifier onboarding
# To go back to Belfry's own certificates: delete infra/issuer/keys/eudi/*, run
# gen-issuer-pki.sh and recreate the same containers.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$INFRA_DIR/issuer/keys/eudi"

if [[ $# -lt 2 || $# -gt 3 ]]; then
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
fi
P12=$1 WRPRC=$2 WRPRC_ISSUER=${3:-}
for f in "$P12" "$WRPRC" ${WRPRC_ISSUER:+"$WRPRC_ISSUER"}; do
  [[ -f $f ]] || { echo "no such file: $f" >&2; exit 1; }
done
if [[ -z ${WRPAC_P12_PASSWORD+x} ]]; then
  read -rsp "Password of $P12: " WRPAC_P12_PASSWORD; echo
fi
export WRPAC_P12_PASSWORD

umask 077
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# OpenSSL 3 refuses the older PKCS#12 ciphers unless asked for the legacy provider.
p12() {
  openssl pkcs12 -in "$P12" -passin env:WRPAC_P12_PASSWORD "$@" 2>"$WORK/err" ||
    openssl pkcs12 -legacy -in "$P12" -passin env:WRPAC_P12_PASSWORD "$@" 2>"$WORK/err" ||
    { cat "$WORK/err" >&2; echo "cannot read $P12 (wrong password?)" >&2; exit 1; }
}
p12 -nocerts -nodes -out "$WORK/key.pem"
p12 -clcerts -nokeys -out "$WORK/leaf.pem"
p12 -cacerts -nokeys -out "$WORK/ca.pem"

openssl pkey -in "$WORK/key.pem" -text -noout | grep -q "prime256v1\|P-256" ||
  { echo "the access certificate's key is not P-256; Belfry signs with ES256 only" >&2; exit 1; }
[[ "$(openssl pkey -in "$WORK/key.pem" -pubout)" == "$(openssl x509 -in "$WORK/leaf.pem" -noout -pubkey)" ]] ||
  { echo "the key in $P12 does not belong to its certificate" >&2; exit 1; }

for jwt in "$WRPRC" ${WRPRC_ISSUER:+"$WRPRC_ISSUER"}; do
  [[ "$(tr -d '[:space:]' <"$jwt" | tr -cd '.' | wc -c)" == 2 ]] ||
    { echo "$jwt is not a JWT (use the wrprc.jwt, not the .cose)" >&2; exit 1; }
done

mkdir -p "$OUT"
# Compose creates the mounted directory as root when it did not exist before the containers
# started (deployments from before this script).
[[ -w "$OUT" ]] || { echo "$OUT is not writable: sudo chown $(id -un) $OUT" >&2; exit 1; }
cp "$WORK/key.pem" "$OUT/wrpac.key.pem"
openssl x509 -in "$WORK/leaf.pem" -out "$OUT/wrpac.crt.pem"
tr -d '[:space:]' <"$WRPRC" >"$OUT/wrprc.jwt"
if [[ -n $WRPRC_ISSUER ]]; then
  tr -d '[:space:]' <"$WRPRC_ISSUER" >"$OUT/wrprc-issuer.jwt"
else
  rm -f "$OUT/wrprc-issuer.jwt"
fi

python3 - "$WORK" "$OUT" <<'PY'
import base64, hashlib, json, re, subprocess, sys
work, out = sys.argv[1:3]

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def jwk_from_pem(path):
    text = subprocess.check_output(["openssl", "ec", "-in", path, "-text", "-noout"], stderr=subprocess.DEVNULL).decode()
    def hexblock(label):
        lines = text.split("\n"); i = next(k for k, l in enumerate(lines) if l.strip().startswith(label))
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

def pems(path):
    return re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", open(path).read(), re.S)

def x509(pem, *args):
    return subprocess.run(["openssl", "x509", *args], input=pem.encode(), capture_output=True, check=True).stdout

def self_signed(pem):
    return x509(pem, "-noout", "-subject", "-nameopt", "RFC2253").split(b"=", 1)[1] == \
        x509(pem, "-noout", "-issuer", "-nameopt", "RFC2253").split(b"=", 1)[1]

leaf = pems(f"{work}/leaf.pem")[0]
chain = [leaf] + [c for c in pems(f"{work}/ca.pem") if not self_signed(c)]
x5c = [base64.b64encode(x509(c, "-outform", "DER")).decode() for c in chain]
json.dump(jwk_from_pem(f"{work}/key.pem"), open(f"{out}/wrpac.jwk.json", "w"), indent=1)
json.dump(x5c, open(f"{out}/wrpac.x5c.json", "w"))

subject = x509(leaf, "-noout", "-subject", "-nameopt", "RFC2253").decode().strip()
digest = b64url(hashlib.sha256(base64.b64decode(x5c[0])).digest())
print(f"access certificate: {subject}")
print(f"  {len(x5c)} certificate(s) in x5c, client id x509_hash:{digest}")
# The wallet binds the registration certificate to the access certificate by the organisation
# identifier (legal person) or serial number (natural person): show both to compare.
for name in ("wrprc.jwt", "wrprc-issuer.jwt"):
    try:
        token = open(f"{out}/{name}").read()
    except FileNotFoundError:
        continue
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    attest = [a.get("meta") for a in claims.get("provides_attestations") or []]
    print(f"{name}: sub {json.dumps(claims.get('sub'))}")
    print(f"  entitlements {claims.get('entitlements')}")
    print(f"  provides_attestations {attest}")
PY
chmod 600 "$OUT/wrpac.key.pem" "$OUT/wrpac.jwk.json"

echo "written to $OUT; re-rendering the verifier config"
"$INFRA_DIR/scripts/gen-issuer-pki.sh"
echo "next: docker compose up -d --force-recreate perms-init verifier onboarding"
