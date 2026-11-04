#!/usr/bin/env bash
# The belfry's changelog, shown at /changelog (services/onboarding/belfry_onboarding/community.py):
# the deployed code's commits (author and co-authors) and the operations run on this server, each
# with who started it. Settings are named, never their values.
#
#   infra/scripts/changelog.sh record "<summary>" [--by NAME] [--proposal EVENT_ID]
#                                   add an operation (belfry config/apply/update call this)
#   infra/scripts/changelog.sh build   write infra/data/changelog/changelog.json
#
# Who: --by, else BELFRY_BY (an agent passes "Ana via Claude"), else the sudo user, else the user.
set -euo pipefail
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$INFRA_DIR/.." && pwd)"
DIR="$INFRA_DIR/data/changelog"
OPS="$DIR/operations.jsonl"
mkdir -p "$DIR"

cmd=${1:-build}; shift || true
case "$cmd" in
  record)
    summary=${1:?usage: record "<summary>" [--by NAME] [--proposal EVENT_ID]}; shift
    by=${BELFRY_BY:-${SUDO_USER:-$(id -un)}} proposal=''
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --by) by=$2; shift 2 ;;
        --proposal) proposal=$2; shift 2 ;;
        *) echo "changelog: unknown option $1" >&2; exit 1 ;;
      esac
    done
    python3 - "$OPS" "$summary" "$by" "$proposal" <<'PY'
import datetime, json, sys
path, summary, by, proposal = sys.argv[1:]
entry = {"at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
         "type": "operation", "summary": summary, "by": [by]}
if proposal:
    entry["proposal"] = proposal
with open(path, "a") as f:
    f.write(json.dumps(entry) + "\n")
PY
    "$0" build
    ;;
  build)
    repo=$(git -C "$ROOT" config --get remote.origin.url || true)
    log=$(mktemp)
    trap 'rm -f "$log"' EXIT
    git -C "$ROOT" log -n 300 --format='%H%x1f%aI%x1f%an%x1f%s%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1e)%x1d' > "$log"
    python3 - "$OPS" "$DIR/changelog.json" "$repo" "$log" <<'PY'
import datetime, json, os, re, sys
ops_path, out, repo, log = sys.argv[1:]
entries = []
for rec in open(log, encoding="utf-8").read().split("\x1d"):
    rec = rec.strip("\n")
    if not rec:
        continue
    sha, at, author, subject, coauthors = (rec.split("\x1f") + [""] * 5)[:5]
    by = [author] + [re.sub(r"\s*<[^>]*>", "", c).strip() for c in coauthors.split("\x1e") if c.strip()]
    entries.append({"at": at, "type": "code", "summary": subject, "by": by, "ref": sha})
if os.path.exists(ops_path):
    for line in open(ops_path):
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
def key(e):
    try:
        return datetime.datetime.fromisoformat(e["at"]).timestamp()
    except Exception:
        return 0
entries.sort(key=key, reverse=True)
data = {"generated": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "repo": repo if repo.startswith("https://") else "", "entries": entries}
tmp = out + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.replace(tmp, out)
PY
    ;;
  *) echo "usage: changelog.sh record|build" >&2; exit 1 ;;
esac
