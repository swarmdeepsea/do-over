#!/usr/bin/env bash
# One-time setup inside the do-over Sprite. Safe to re-run.
set -euo pipefail
ROOT="$HOME/do-over"
GH="https://api.sprites.dev/v1/gateway/github/R5hurByTqtNkFyd1m8q8oQ"
UPSTREAM="hagopj13/node-express-boilerplate"
cd "$ROOT"

echo "== 1. fork $UPSTREAM to the connector's account"
ME=$(curl -sf "$GH/user" | python3 -c "import json,sys;print(json.load(sys.stdin)['login'])")
curl -sf -X POST "$GH/repos/$UPSTREAM/forks" -H 'Content-Type: application/json' -d '{"default_branch_only":true}' >/dev/null || true
FORK="$ME/node-express-boilerplate"
for i in $(seq 1 30); do curl -sf "$GH/repos/$FORK" >/dev/null && break; sleep 2; done
# Issues are off on forks by default; Do-Over keeps its notes in one.
curl -sf -X PATCH "$GH/repos/$FORK" -H 'Content-Type: application/json' -d '{"has_issues":true}' >/dev/null
echo "$FORK" > fork.txt
echo "fork: https://github.com/$FORK"

echo "== 2. clone (public, read-only)"
[ -d target/.git ] || git clone -q --depth 1 "https://github.com/$FORK.git" target
cd target && git config user.email do-over@sprite && git config user.name do-over
cp -n .env.example .env

echo "== 3. MongoDB 7 as a Sprite service (comes back after every restore)"
if [ ! -x "$ROOT/mongo/bin/mongod" ]; then
  ARCH=$(uname -m); [ "$ARCH" = aarch64 ] && MARCH=aarch64 || MARCH=x86_64
  curl -sfL "https://fastdl.mongodb.org/linux/mongodb-linux-$MARCH-ubuntu2204-7.0.14.tgz" -o /tmp/mongo.tgz
  mkdir -p "$ROOT/mongo" && tar xzf /tmp/mongo.tgz -C "$ROOT/mongo" --strip-components=1
fi
mkdir -p "$ROOT/mongo-data"
sprite-env services get mongo >/dev/null 2>&1 || sprite-env services create mongo --cmd "$ROOT/mongo/bin/mongod" \
  --args "--dbpath,$ROOT/mongo-data,--bind_ip,127.0.0.1,--port,27017"
for i in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/27017) 2>/dev/null && break; sleep 1; done
echo "mongo up"

echo "== 4. install deps (npm, ignoring yarn.lock) and run the baseline"
node --version
npm install --no-audit --no-fund >/dev/null 2>&1
git add -A >/dev/null 2>&1 || true
git commit -qm "do-over: npm lockfile baseline" >/dev/null 2>&1 || true
python3 "$ROOT/doover.py" tests | python3 -c "import json,sys;r=json.load(sys.stdin);print('baseline', r['passed'],'/',r['total'],'ok' if r['ok'] else 'NOT GREEN');print(r['tail'][:1500] if not r['ok'] else '')"

echo "== 5. brain service (created stopped; doover.py start arms it)"
sprite-env services get brain >/dev/null 2>&1 && sprite-env services delete brain
sprite-env services create brain --cmd /usr/bin/python3 --args "$ROOT/doover.py,brain" --dir "$ROOT"
sprite-env services stop brain || true
echo "setup done"
