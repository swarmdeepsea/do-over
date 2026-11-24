#!/usr/bin/env python3
"""Do-Over: an agent that lives three futures and keeps the best one.

Runs as a Sprite service ("brain"). It checkpoints its own Sprite, tries one
upgrade strategy per future, writes the result to a GitHub issue (outside the
Sprite, so it survives), rolls the Sprite back, and tries the next one. When
every future is done it restores the winner's checkpoint and opens a PR.

A restore wipes the disk and restarts the Sprite, so this process dies on every
rollback. The issue is the only memory that survives; on each boot the brain
reads it to find out where it is in time.

Python stdlib only. All network calls go through Sprites connectors, so no API
key lives on this Sprite.
"""
import base64
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection

# ---------------------------------------------------------------- config
HOME = os.path.expanduser("~")
ROOT = os.environ.get("DOOVER_ROOT", os.path.join(HOME, "do-over"))
RUN_FILE = os.path.join(ROOT, "run.json")
REPO_DIR = os.path.join(ROOT, "target")
GH = os.environ.get("DOOVER_GH", "https://api.sprites.dev/v1/gateway/github/R5hurByTqtNkFyd1m8q8oQ")
LLM = os.environ.get("DOOVER_LLM", "https://api.sprites.dev/v1/gateway/custom_api/8YeuF_lR33Uimp9gkw87xw")
MODEL = os.environ.get("DOOVER_MODEL", "deepseek-chat")

# Hard caps. These live in code on purpose, not in a prompt.
MAX_FUTURES = 3
FUTURE_TIMEOUT_S = 35 * 60          # one future may run 35 minutes at most
TEST_TIMEOUT_S = 10 * 60            # one test run
LLM_ROUNDS = 5                      # repair rounds in the "llm" future
LLM_MAX_TOKENS = 8000               # per call (DeepSeek chat max)
LLM_TOKEN_BUDGET = 150_000          # whole run, prompt + completion
FLY_BUDGET_USD = 3.00               # stop if estimated Sprite spend passes this
CPU_USD_PER_H = 0.0385              # fly.io/pricing, per CPU-hour
MEM_USD_PER_GBH = 0.021875          # per GB-hour
SPRITE_CPUS = float(os.environ.get("DOOVER_CPUS", "8"))      # assume the big size: overestimates cost
SPRITE_MEM_GB = float(os.environ.get("DOOVER_MEM_GB", "16"))

MARK = "<!-- doover "
STRATEGIES = {
    "naive": "Bump the versions and hope.",
    "codemod": "Bump the versions, then run the official Express codemod.",
    "llm": "Bump the versions, then let DeepSeek patch the code from the failing tests (5 rounds max, a round that makes things worse is rolled back).",
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- sprite self-API
class UnixHTTP(HTTPConnection):
    def __init__(self, path):
        super().__init__("sprite")
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)


def sprite_api(method, path, body=None):
    c = UnixHTTP("/.sprite/api.sock")
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read().decode()
    if r.status >= 400:
        raise RuntimeError(f"sprite api {method} {path} -> {r.status}: {raw[:300]}")
    return raw


def checkpoint(comment):
    t = time.time()
    raw = sprite_api("POST", "/v1/checkpoint", {"comment": comment})
    ms = round((time.time() - t) * 1000)
    m = re.findall(r"Checkpoint (v\d+) created", raw)
    if not m:
        raise RuntimeError("no checkpoint id in: " + raw[-300:])
    return m[-1], ms


def hold(minutes=30):
    """Ask the runtime to keep this Sprite awake. A running service alone does not: without a task
    hold the Sprite pauses ~30 s after the last outside activity, even mid-`npm install`."""
    try:
        sprite_api("PUT", "/v1/tasks/doover", {"expire": f"{minutes}m"})
    except RuntimeError:
        sprite_api("POST", "/v1/tasks", {"name": "doover", "expire": f"{minutes}m"})


def release():
    try:
        sprite_api("DELETE", "/v1/tasks/doover")
    except RuntimeError as e:
        log("release:", e)


def restore(cp_id):
    sprite_api("POST", f"/v1/checkpoints/{cp_id}/restore")
    # The Sprite restarts under us. Wait to be killed.
    time.sleep(600)
    raise RuntimeError("restore did not restart the Sprite")


# ---------------------------------------------------------------- connectors
def http(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:500]
            if e.code >= 500 and attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError(f"{method} {url} -> {e.code}: {msg}")
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(3)
                continue
            raise


def gh(method, path, body=None):
    return http(method, GH + path, body)


def llm(messages, run):
    used = run.get("llm_tokens_seen", 0)
    if used >= LLM_TOKEN_BUDGET:
        raise RuntimeError("LLM token budget spent")
    r = http("POST", LLM + "/chat/completions",
             {"model": MODEL, "max_tokens": LLM_MAX_TOKENS, "temperature": 0, "messages": messages}, timeout=240)
    run["llm_tokens_seen"] = used + r.get("usage", {}).get("total_tokens", 0)
    return r["choices"][0]["message"]["content"]


# ---------------------------------------------------------------- notes from futures (GitHub issue)
def post(run, event, text):
    """Write one event outside time. Returns the event dict."""
    event = {**event, "t": round(time.time(), 3)}
    body = f"{text}\n\n{MARK}{json.dumps(event)} -->"
    gh("POST", f"/repos/{run['repo']}/issues/{run['issue']}/comments", {"body": body})
    log("note:", event.get("event"), event.get("k", ""))
    return event


def events(run):
    out, page = [], 1
    while True:
        cs = gh("GET", f"/repos/{run['repo']}/issues/{run['issue']}/comments?per_page=100&page={page}")
        for c in cs:
            i = c["body"].find(MARK)
            if i >= 0:
                out.append(json.loads(c["body"][i + len(MARK):].rsplit("-->", 1)[0]))
        if len(cs) < 100:
            return out
        page += 1


# ---------------------------------------------------------------- the work
def sh(cmd, timeout=600, cwd=REPO_DIR):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def run_tests():
    t = time.time()
    try:
        code, out = sh("npx jest -i --testTimeout=4000 --json --outputFile=/tmp/jest.json --detectOpenHandles --forceExit",
                       timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 0, "total": 0, "ok": False, "secs": TEST_TIMEOUT_S, "tail": "timeout"}
    secs = round(time.time() - t, 1)
    try:
        j = json.load(open("/tmp/jest.json"))
        res = {"passed": j["numPassedTests"], "failed": j["numFailedTests"], "total": j["numTotalTests"],
               "suites_failed": j["numFailedTestSuites"] + j.get("numRuntimeErrorTestSuites", 0)}
        crashes, fails, seen = [], [], set()  # noqa
        for s in j["testResults"]:
            if s.get("message") and not s.get("assertionResults"):   # whole suite failed to load: list these first
                crashes.append("SUITE FAILED TO LOAD " + s["name"].split("/tests/")[-1] + ": " + s["message"][:2000])
            for a in s.get("assertionResults", []):
                if a["status"] == "failed":
                    msg = " ".join(a["failureMessages"])
                    key = msg.splitlines()[0][:160] if msg else a["fullName"]
                    if key in seen:            # same error in 40 tests is one error
                        continue
                    seen.add(key)
                    fails.append(a["fullName"] + ": " + msg[:900])
                    if "got 500" in msg and "probe" not in res:
                        res["probe"] = (s["name"], a["fullName"])
        res["tail"] = "\n---\n".join(crashes + fails)[:16000]
    except Exception:
        res = {"passed": 0, "failed": 0, "total": 0, "suites_failed": 1, "tail": out[-6000:]}
    res["ok"] = res["total"] > 0 and res["failed"] == 0 and res.get("suites_failed", 0) == 0
    res["secs"] = secs
    return res


def bump(run):
    pkgs = " ".join(run["upgrade"])
    code, out = sh(f"npm install --no-audit --no-fund {pkgs}", timeout=600)
    if code:
        raise RuntimeError("npm install failed:\n" + out[-2000:])


def strategy_naive(run):
    bump(run)


def strategy_codemod(run):
    bump(run)
    code, out = sh("npx --yes @expressjs/codemod@latest upgrade . 2>&1 || true", timeout=600)
    log("codemod:", out[-500:])


def changed_files():
    _, out = sh("git status --porcelain")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


MIGRATION_NOTES = """Known breaking changes (from the Express 5 and Mongoose 7/8 migration guides):
- Express 5: path strings use path-to-regexp v8. A bare '*' is invalid: use '/{*splat}' or a RegExp.
- Express 5: req.query is a read-only getter. Middleware that assigns req.query (old express-mongo-sanitize,
  xss-clean) throws. Sanitize req.body/req.params in place, or copy query into a new object, instead.
- Express 5: res.send(status, body), res.json(status, obj), req.param() and app.del() are removed.
- Express 5: rejected promises from async handlers are passed to next(err) automatically.
- Mongoose 6+: useNewUrlParser, useUnifiedTopology, useCreateIndex, useFindAndModify options are removed.
- Mongoose 7+: callbacks removed everywhere; doc.remove() removed (use deleteOne()); Model.count() -> countDocuments().
- Mongoose 7+: mongoose.Types.ObjectId() must be called with new.
- Mongoose 7+: strictQuery defaults to false.
Keep require() paths exactly as they are unless a file really moved."""

EDITABLE = ("src", "tests/fixtures", "tests/utils")


def editable_files():
    files = {}
    for top in EDITABLE:                      # ~60 KB: give the model all of it
        for d, _, fs in os.walk(os.path.join(REPO_DIR, top)):
            for f in fs:
                if f.endswith(".js"):
                    full = os.path.join(d, f)
                    files[os.path.relpath(full, REPO_DIR)] = open(full).read()
    return files


def restore_files(snapshot):
    for path, body in snapshot.items():
        open(os.path.join(REPO_DIR, path), "w").write(body)


def probe_500(best):
    """A test got a bare 500: the app hides the real error outside development mode. Re-run just that
    test with NODE_ENV=development, where the app logs the error and its stack, and keep the useful lines."""
    if "probe" not in best:
        return ""
    path, name = best["probe"]
    pattern = re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", name)
    try:
        _, out = sh(f"NODE_ENV=development npx jest {shlex.quote(path)} -t {shlex.quote(pattern)} --testTimeout=4000 --forceExit 2>&1",
                    timeout=180)
    except subprocess.TimeoutExpired:
        return ""
    keep = [l for l in out.splitlines()
            if re.search(r"Error|error:|at .*src/|Cannot|undefined|not a function", l) and "node_modules" not in l]
    return ("SERVER-SIDE LOG for the 500 in '" + name + "' (re-run in development mode):\n" +
            "\n".join(keep[:40])[:3000] + "\n\n") if keep else ""


def strategy_llm(run):
    bump(run)
    best = run_tests()
    snapshot = editable_files()
    note = ""
    for rnd in range(1, LLM_ROUNDS + 1):
        log(f"llm round {rnd}: best so far {best['passed']} passing")
        if best["ok"]:
            return
        hold()
        prompt = (
            f"A Node project was upgraded with: npm install {' '.join(run['upgrade'])}.\n"
            "Fix the code so the existing tests pass. You may edit src/, tests/fixtures/ and tests/utils/. "
            "Never edit *.test.js files or package.json. Fix every breaking change you can see, not only the first error. "
            "Return ONLY the files you change, each as:\n=== FILE: <path> ===\n<entire file>\n=== END ===\n\n"
            f"{MIGRATION_NOTES}\n\n{note}{probe_500(best)}"
            f"FAILING TESTS ({best['passed']} passing now):\n{best['tail']}\n\nFILES:\n" +
            "\n".join(f"=== FILE: {n} ===\n{c}\n=== END ===" for n, c in sorted(snapshot.items())))
        reply = llm([{"role": "system", "content": "You are a careful Node.js upgrade engineer."},
                     {"role": "user", "content": prompt}], run)
        wrote = []
        for path, body in re.findall(r"=== FILE: (\S+) ===\n(.*?)\n=== END ===", reply, re.S):
            # Guardrail in code: the model may touch app code, fixtures and test helpers, never assertions.
            allowed = path.startswith(tuple(t + "/" for t in EDITABLE))
            if not allowed or ".." in path or path.endswith(".test.js"):
                log("refused write:", path)
                continue
            full = os.path.join(REPO_DIR, path)
            if not os.path.exists(full) or open(full).read().strip() == body.strip():
                continue                      # no new files, no no-op rewrites
            open(full, "w").write(body.rstrip("\n") + "\n")
            wrote.append(path)
        log(f"llm round {rnd}: rewrote {len(wrote)} files")
        if not wrote:
            return
        res = run_tests()
        if res["passed"] < best["passed"] or (res["passed"] == best["passed"] and not res["ok"]):
            # Do-Over, one level down: a round that doesn't help is rolled back before the next one.
            restore_files(snapshot)
            first = res["tail"].split("\n---\n")[0][:600]
            note = (f"YOUR LAST ATTEMPT was rolled back: it changed {', '.join(wrote)} and left "
                    f"{res['passed']} passing (best is {best['passed']}). Its first error:\n{first}\n\n")
            log(f"llm round {rnd}: {res['passed']} passing, worse or equal, rolled back")
        else:
            best, snapshot, note = res, editable_files(), ""
            log(f"llm round {rnd}: {res['passed']} passing, kept")


# ---------------------------------------------------------------- brain
def estimate_usd(run):
    hours = (time.time() - run["started"]) / 3600
    return round(hours * (SPRITE_CPUS * CPU_USD_PER_H + SPRITE_MEM_GB * MEM_USD_PER_GBH), 3)


def live_future(run, k, name):
    post(run, {"event": "future_started", "k": k, "strategy": name},
         f"**Future {k}/{len(run['futures'])} · `{name}` begins.** {STRATEGIES[name]}")
    t = time.time()
    err = None

    def out_of_time(*_):
        raise TimeoutError(f"future ran past its {FUTURE_TIMEOUT_S // 60}-minute cap")
    signal.signal(signal.SIGALRM, out_of_time)
    signal.alarm(FUTURE_TIMEOUT_S)   # hard wall-clock cap, enforced in code
    hold(FUTURE_TIMEOUT_S // 60 + 5)
    try:
        globals()["strategy_" + name](run)
        res = run_tests()
    except Exception as e:  # a crashed future is still a future
        err = str(e)[:1500]
        res = {"passed": 0, "failed": 0, "total": 0, "ok": False, "secs": 0, "tail": err}
    finally:
        signal.alarm(0)
    files = changed_files()
    base_n = run["baseline"]
    res["ok"] = res["ok"] and res["passed"] >= base_n
    lost = base_n - res["passed"]
    cp, cp_ms = checkpoint(f"doover:{run['id']}:future{k}")
    ev = {"event": "future_done", "k": k, "strategy": name, "checkpoint": cp, "checkpoint_ms": cp_ms,
          "passed": res["passed"], "failed": res["failed"], "total": res["total"], "ok": res["ok"],
          "test_secs": res["secs"], "future_secs": round(time.time() - t, 1), "files_changed": len(files),
          "llm_tokens": run.get("llm_tokens_seen", 0), "usd_est": estimate_usd(run), "error": err}
    verdict = ("all tests green" if res["ok"] else
               f"{lost} of the baseline's tests fail or never load" if res["total"] else "never got to run its tests")
    tail = res.get("tail", "")[:900]
    post(run, ev,
         f"**Future {k} · `{name}`: {res['passed']}/{base_n} tests passing, {verdict}.**\n"
         f"{len(files)} files changed in {ev['future_secs']} s. Saved as checkpoint `{cp}` ({cp_ms} ms).\n"
         + (f"<details><summary>what broke</summary>\n\n```\n{tail}\n```\n</details>\n" if tail and not res["ok"] else "")
         + ("\nThis timeline is about to be erased." if k < len(run["futures"]) else ""))
    return ev


def pick_winner(done):
    # Rules decide, not the model: all-green beats everything, then most tests passed, then fewest files touched.
    return sorted(done, key=lambda e: (not e["ok"], -e["passed"], e["files_changed"], e["k"]))[0]


def open_pr(run, win):
    repo, base = run["repo"], run["base_branch"]
    head_sha = gh("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    base_tree = gh("GET", f"/repos/{repo}/git/commits/{head_sha}")["tree"]["sha"]
    tree = []
    for path in changed_files():
        full = os.path.join(REPO_DIR, path)
        if os.path.isdir(full) or path.startswith("node_modules"):
            continue
        if not os.path.exists(full):
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = gh("POST", f"/repos/{repo}/git/blobs",
                  {"content": base64.b64encode(open(full, "rb").read()).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    t = gh("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})
    msg = f"Upgrade {' '.join(run['upgrade'])} (Do-Over future {win['k']}: {win['strategy']})"
    c = gh("POST", f"/repos/{repo}/git/commits", {"message": msg, "tree": t["sha"], "parents": [head_sha]})
    branch = f"do-over/{run['id']}"
    gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": c["sha"]})
    pr = gh("POST", f"/repos/{repo}/pulls", {
        "title": msg, "head": branch, "base": base,
        "body": f"Do-Over lived {len(run['futures'])} futures of this upgrade and kept this one.\n\n"
                f"Full notes from the futures that never happened: #{run['issue']}"})
    return pr["html_url"]


def brain():
    if not os.path.exists(RUN_FILE):
        log("no run.json")
        return finish()
    run = json.load(open(RUN_FILE))
    hold()
    ev = events(run)
    for _ in range(60):  # just armed by start(): wait for the base checkpoint note
        if any(e["event"] == "base" for e in ev):
            break
        time.sleep(3)
        ev = events(run)
    else:
        log("no base note after 3 minutes")
        return finish()
    done = [e for e in ev if e["event"] == "future_done"]
    log(f"boot: run {run['id']}, {len(done)} futures done, last event {ev[-1]['event'] if ev else None}")
    last = ev[-1] if ev else {}

    if last.get("event") == "restoring":
        post(run, {"event": "woke", "from": last["target"], "restore_s": round(time.time() - last["t"], 1)},
             f"Woke up in checkpoint `{last['target']}` {round(time.time() - last['t'], 1)} s after the rollback. "
             "My disk forgot everything since then. Reading my notes.")

    if any(e["event"] in ("pr_opened", "gave_up") for e in ev):
        return finish()

    chosen = [e for e in ev if e["event"] == "chose"]
    if chosen:  # we are standing in the winner's timeline
        win = chosen[-1]
        url = open_pr(run, win)
        post(run, {"event": "pr_opened", "url": url},
             f"**Back in future {win['k']} (`{win['strategy']}`), the one that worked best. PR: {url}**")
        return finish()

    k = len(done) + 1
    if k <= len(run["futures"]) and estimate_usd(run) < FLY_BUDGET_USD:
        started = [e for e in ev if e["event"] == "future_started" and e["k"] == k]
        if started:  # we died mid-future last boot: record it as a failed future, not a silent retry
            post(run, {"event": "future_done", "k": k, "strategy": started[-1]["strategy"], "checkpoint": None,
                       "passed": 0, "failed": 0, "total": 0, "ok": False, "files_changed": 0, "error": "crashed"},
                 f"Future {k} crashed before it finished. Counting it as lost.")
        else:
            live_future(run, k, run["futures"][k - 1])
        return go_next(run)
    return go_next(run)


def finish():
    # Stop our own service so the Sprite can go to sleep and stop billing.
    log("done; releasing the wake hold and stopping brain service")
    release()
    subprocess.run(["sprite-env", "services", "stop", "brain"])
    time.sleep(30)


def base_cp(ev):
    return [e for e in ev if e["event"] == "base"][-1]["checkpoint"]


def go_next(run):
    ev = events(run)
    done = [e for e in ev if e["event"] == "future_done"]
    if len(done) < len(run["futures"]) and estimate_usd(run) < FLY_BUDGET_USD:
        b = base_cp(ev)
        post(run, {"event": "restoring", "target": b, "next": len(done) + 1},
             f"Rolling back to `{b}` to try future {len(done) + 1}.")
        restore(b)
    real = [e for e in done if e.get("checkpoint")]
    if not real or not any(e["passed"] for e in real):
        post(run, {"event": "gave_up"}, "No future got any tests passing. Keeping the original timeline. No PR.")
        return finish()
    win = pick_winner(real)
    board = "\n".join(f"| {e['k']} | `{e['strategy']}` | {e['passed']}/{run['baseline']} | {e['files_changed']} | `{e['checkpoint']}` |"
                      for e in done)
    post(run, {**{k: win[k] for k in ("k", "strategy", "checkpoint")}, "event": "chose"},
         "**All futures lived.**\n\n| Future | Strategy | Tests passed | Files changed | Checkpoint |\n|---|---|---|---|---|\n"
         f"{board}\n\nKeeping future {win['k']}. Restoring `{win['checkpoint']}`.")
    post(run, {"event": "restoring", "target": win["checkpoint"], "next": "pr"},
         f"Rolling forward into `{win['checkpoint']}`.")
    restore(win["checkpoint"])


# ---------------------------------------------------------------- start a run
def start(repo, upgrade, futures):
    futures = futures[:MAX_FUTURES]
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        sys.exit(f"clone {repo} into {REPO_DIR} first (setup.sh does this)")
    base = run_tests()
    if not base["ok"]:
        sys.exit(f"baseline is not green ({base['passed']}/{base['total']}); fix setup first:\n{base['tail'][:2000]}")
    rid = time.strftime("%m%d-%H%M")
    repo_info = gh("GET", f"/repos/{repo}")
    issue = gh("POST", f"/repos/{repo}/issues", {
        "title": f"Do-Over {rid}: notes from futures that never happened",
        "body": f"Upgrading `{' '.join(upgrade)}`. Baseline: {base['passed']}/{base['total']} tests green.\n\n"
                f"I will live {len(futures)} futures: {', '.join(futures)}. Each one is erased from my disk when I roll back. "
                "These comments are the only memory that survives."})
    run = {"id": rid, "repo": repo, "base_branch": repo_info["default_branch"], "issue": issue["number"],
           "upgrade": upgrade, "futures": futures, "started": time.time(), "baseline": base["passed"]}
    json.dump(run, open(RUN_FILE, "w"))
    # The brain must be RUNNING inside the base checkpoint: a stopped service stays stopped after a restore.
    # It waits for the "base" note below before doing anything.
    subprocess.run(["sprite-env", "services", "start", "brain"], check=True)
    cp, ms = checkpoint(f"doover:{rid}:base")
    # The checkpoint id can't live in run.json (it is taken after the file is written), so it lives in the notes.
    post(run, {"event": "base", "checkpoint": cp, "checkpoint_ms": ms, "baseline": base["passed"]},
         f"Base timeline saved as `{cp}` ({ms} ms). {base['passed']}/{base['total']} tests green before anything changes.")
    print(f"run {rid} started: https://github.com/{repo}/issues/{issue['number']}")
    print("the brain is running; watch the issue")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brain"
    if cmd == "brain":
        brain()
    elif cmd == "start":
        start(sys.argv[2], sys.argv[3].split(","), sys.argv[4].split(","))
    elif cmd == "tests":
        print(json.dumps(run_tests(), indent=1))
    else:
        sys.exit("usage: doover.py brain | start <owner/repo> <pkg@ver,...> <naive,codemod,llm> | tests")
