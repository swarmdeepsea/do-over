# Do-Over

**An agent that lives three futures and keeps the best one.**

Live page with the demo: **https://do-over-hacksgiving.fly.dev**

Do-Over runs on a Fly.io Sprite. To do a risky job, it checkpoints its own Sprite, tries one approach, writes down how it went, rolls the whole machine back, and tries the next one. When every future is done, it restores the winner and opens a pull request.

Rolling back wipes the disk and restarts the Sprite, so the agent forgets each future as soon as it leaves it. The only memory that survives is a GitHub issue it writes to through a Sprites connector: **notes from futures that never happened.**

## Result

The job: upgrade [node-express-boilerplate](https://github.com/hagopj13/node-express-boilerplate) (MIT, 113 tests) to Express 5, Mongoose 8 and Jest 29, which are three breaking majors at once.

| Future | Strategy | Tests passing (of 113) |
|---|---|---|
| 1 | Bump the versions | 23 |
| 2 | Bump + the official Express codemod | 23 |
| 3 | Bump + DeepSeek repair loop | **109** |

The result reproduced across two separate runs ([issue #10](https://github.com/swarmdeepsea/node-express-boilerplate/issues/10) → [PR #11](https://github.com/swarmdeepsea/node-express-boilerplate/pull/11), [issue #12](https://github.com/swarmdeepsea/node-express-boilerplate/issues/12) → [PR #13](https://github.com/swarmdeepsea/node-express-boilerplate/pull/13)). Both of the other strategies are honest baselines: one is what you get by typing `npm install`, the other is what the Express team ships for this exact migration.

Measured on the Sprite in those runs:
- Self-checkpoint: 76–144 ms for the base, 0.2–1.8 s after a future
- Rollback → agent awake and reading its notes: 5.1–15.9 s
- One full run (3 futures, 4 time jumps): 4 min 4 s ([issue #12](https://github.com/swarmdeepsea/node-express-boilerplate/issues/12), first note 18:56:38 UTC → PR 19:00:42 UTC)

**What's still broken:** 4 tests on validation error paths return 500 instead of 400. The notes issue shows the error under future 3's "what broke".

## How it works

```
start ─► checkpoint "base" ─► future 1 ─► note ─► restore base
                                   ▲                    │
                                   └──── future 2 ◄─────┘ ─► note ─► restore base ─► future 3 ─► note
                                                                                         │
                          open PR ◄── wake, read notes ◄── restore the winner ◄── pick ◄─┘
```

1. `doover.py start` runs the baseline tests, opens the notes issue, and takes the base checkpoint from inside the Sprite (`POST /v1/checkpoint` on `/.sprite/api.sock`).
2. The agent runs as a Sprite **service**, so the runtime brings it back after every restore. Each boot, it reads the issue to find out where it is in time ([`brain()`](doover.py)).
3. Each future applies one strategy, runs the tests, checkpoints the result, and posts a note. Then the agent restores the base and dies.
4. Rules pick the winner, not the model: all green first, then most tests passing, then fewest files changed ([`pick_winner`](doover.py)).
5. The agent restores the winner's checkpoint, wakes up there, and opens the PR through the GitHub connector using the Git Data API ([`open_pr`](doover.py)).

## Sprites features it uses

| Feature | Where |
|---|---|
| Checkpoint + restore from inside the Sprite | `checkpoint()`, `restore()` |
| Services (auto-restart after restore) | `setup.sh` step 5, MongoDB in step 3 |
| Tasks API (stay awake during a future) | `hold()`, `release()` |
| **GitHub connector**: fork, issue notes, blobs, commit, PR | `gh()` |
| **Custom API connector**: DeepSeek, key never on the Sprite | `llm()` |

There are no API keys on the Sprite. Both connectors authenticate through the Sprites gateway.

## Guardrails (in code, not in a prompt)

- At most 3 futures, 35 minutes each (`SIGALRM`), 10 minutes per test run.
- DeepSeek: at most 5 rounds, 8k tokens per reply, 240 s per call, a 150k-token budget per future.
- The model may edit `src/`, `tests/fixtures/` and `tests/utils/`. Any write to a `*.test.js` file or `package.json` is refused (`strategy_llm`).
- A repair round that doesn't raise the test count is rolled back before the next round: Do-Over, one level down.
- It stops if the estimated Sprite spend for the run passes $3, assuming the largest Sprite size, so the estimate runs high.
- When the run is done it releases its wake hold and stops its own service, so the Sprite can sleep.

## Run it

```bash
sprite create do-over                       # name must match the connectors' prefix rule
# add a GitHub connector and a Custom API connector (https://api.deepseek.com, Bearer) with prefix do-over
tar cz doover.py setup.sh | sprite exec --stdin -s do-over -- bash -c 'mkdir -p ~/do-over && tar xz -C ~/do-over && bash ~/do-over/setup.sh'
sprite exec -s do-over -- python3 ~/do-over/doover.py start <you>/node-express-boilerplate express@5,mongoose@8,jest@29 naive,codemod,llm
```

Then watch the issue it prints.

## Built during Hacksgiving

All of Do-Over was written Nov 24, 2026, during Fly.io Hacksgiving. The project it upgrades, hagopj13/node-express-boilerplate (MIT), is the test subject, not part of the agent. The migration notes in `MIGRATION_NOTES` summarize the public Express 5 and Mongoose 7/8 migration guides.

Getting here took eight runs, and the failed ones are kept: [issues #1–#8](https://github.com/swarmdeepsea/node-express-boilerplate/issues?q=is%3Aissue). Along the way it found that the old Jest can't load Express 5, that a Sprite pauses during a long job unless it holds a task, and that the project's own fixtures break on Mongoose 7.
