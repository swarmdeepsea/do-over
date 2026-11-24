#!/usr/bin/env bash
# Runs inside the do-over Sprite. Creates github.com/<connector account>/do-over and uploads the agent,
# all through the GitHub connector (no token on the Sprite).
set -euo pipefail
GH="https://api.sprites.dev/v1/gateway/github/R5hurByTqtNkFyd1m8q8oQ"
cd "$HOME/do-over"
ME=$(curl -sf "$GH/user" | python3 -c "import json,sys;print(json.load(sys.stdin)['login'])")
curl -sf -X POST "$GH/user/repos" -H 'Content-Type: application/json' \
  -d '{"name":"do-over","description":"An agent that lives three futures on a Fly.io Sprite and keeps the best one. Fly.io Hacksgiving 2026.","has_issues":true}' >/dev/null || echo "(repo exists)"
for f in README.md doover.py setup.sh publish.sh; do
  SHA=$(curl -s "$GH/repos/$ME/do-over/contents/$f" | python3 -c "import json,sys;print(json.load(sys.stdin).get('sha',''))" 2>/dev/null || true)
  python3 - "$f" "$SHA" > /tmp/body.json <<'PY'
import base64, json, sys
f, sha = sys.argv[1], sys.argv[2]
body = {"message": f"Add {f}", "content": base64.b64encode(open(f, "rb").read()).decode()}
if sha: body.update(sha=sha, message=f"Update {f}")
print(json.dumps(body))
PY
  curl -sf -X PUT "$GH/repos/$ME/do-over/contents/$f" -H 'Content-Type: application/json' --data @/tmp/body.json >/dev/null
  echo "uploaded $f"
done
echo "https://github.com/$ME/do-over"
