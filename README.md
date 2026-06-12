# Cardinal System — Autonomous Game Management Middleware

A working, real-world replication of the **Sword Art Online Cardinal System**:
a hierarchical management layer that keeps a game alive, balanced, and
expanding with **zero human intervention** — self-healing code, autonomous
economy balancing, internet-sourced quest generation, biometric-driven
events, and a verification Sub-Process that gates every mutation.

Built game-agnostic from the first line: all intelligence lives in the
`cardinal/` package and talks to games through adapters (log schema +
sequenced control channel + stats DB) or plain HTTP (`cardinal/api`).
The bundled Python RPG (`game.py`) is just the first engine plugged in.

## Architecture

```
                    MAIN PROCESS (creative)            SUB-PROCESS (restraint)
                 Fable 5 via cardinal/llm  ──────►  cardinal/sub_process.py
                                                    every mutation gated:
  L3 Evolver   ── on-demand cloud calls             taboo check, 8% clamp,
  L2 Simulator ── local daemons (healer,            sandboxed tests, atomic
                  balancer, sentiment, bio)         write, version bump,
  L1 Predictor ── game.py (zero AI calls)           replay capture, rollback
                        ▲
                        │ server.log + cardinal.db + sequenced control channel
                        ▼
            cardinal/adapters (Python sandbox) / cardinal/api (any engine)
```

## Quick start

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then edit; the bundled .env runs offline (mock)

python migrate.py               # step 0 — schema (single source of truth)
python cardinal_core.py status  # orchestrator CLI
python sim_runner.py --games 50 # headless bots populate cardinal.db
python self_healing.py          # Error Control daemon (repairs crashes)
python balancer.py --check      # Order Control report
python quest_generator.py --topic "Ragnarok"   # Social Control (needs network)
python biometric_daemon.py --simulate          # Cognitive Telemetry
python dashboard.py             # http://localhost:8000 — live operations
python api_server.py            # REST API for external engines (bearer token)
python sentiment_reader.py      # Reddit sentiment (fail-silent w/o creds)
python -m cardinal.admin.cli force-incarnate Bot_Alpha 15 --reason "demo"
python -m pytest tests/ -q      # 57 tests, fully offline via MockProvider
```

Every module is independently runnable; none requires the others to be up.

## The two intentional bugs

`game.py` ships broken **on purpose** — Cardinal repairs it autonomously:

- **BUG A** — equipping the *Cursed Blade* has a 5% chance per combat round
  of a `ZeroDivisionError` inside `calculate_damage()`.
- **BUG B** — gold > 9999 has a 3% chance per tick of a non-converging loop
  in `loot_distribution()` (5s watchdog deadline, Windows-safe).

Trigger them with `python sim_runner.py --games 30 --force-weapon "Cursed Blade"`,
then watch `python self_healing.py` detect, patch, verify, and hot-swap.

## LLM tiers & cost safety

| Tier | Provider | When |
|------|----------|------|
| L3 | Anthropic (model from `CARDINAL_MODEL`) | `ANTHROPIC_API_KEY` set; spend guard allows |
| Mock | `cardinal/llm/mock_provider.py` | key absent + `CARDINAL_USE_MOCK=true` |
| L2 | `LocalRuleProvider` (deterministic rules) | fallback, 80%-spend mode, no key |

Hard daily limit (`MAX_DAILY_SPEND_USD`): 80% → Discord warning + L2-only;
100% → L3 lockout until midnight. One L3 attempt per event, ever.

## Key guarantees

- **No gate bypass**: every mutation (patch, rebalance, quest, SEC update,
  world change, rollback) flows through `cardinal/sub_process.py`.
- **8% law**: per-field balance deltas are clamped deterministically;
  exploit-class anomalies (≥95% win rate, >4σ stats) are quarantined instead.
- **Append-only history**: every approval writes a full system snapshot to
  `versions`; `python cardinal_core.py rollback <v>` restores instantly.
- **Total auditability**: replay log captures before/after state + exact
  LLM I/O for every event (`cardinal_core.py replay <id>` or the dashboard).
- **Temporal drift compensation**: control channel updates carry strict
  `sequence_id`s; gaps trigger a State Refresh Request resync.
- **Ghost Protocol**: `CARDINAL_VERBOSITY` 0/1/2 masks all operator output
  (critical alerts always fire); dashboard has a Ghost Mode toggle.
- **Secrets**: only from `.env`. Missing creds = graceful degradation,
  except the REST API which *refuses to serve* without `CARDINAL_API_TOKEN`.

## Project layout

```
cardinal/                game-agnostic middleware package
  core/                  config (verbosity mask), DB layer, event schema
  adapters/              GameAdapter ABC + Python RPG adapter
  sources/               TopicSource ABC + wikipedia/osm/gutenberg/news-stub
  llm/                   provider tiers, mock provider, spend guard
  modules/               healer, balancer, quest gen, quality filter, SEC,
                         taboo index, biometrics, MHCP, sentiment, notifier, replay
  api/                   REST layer (bearer auth)
  dashboard/             FastAPI + SSE live dashboard
  admin/                 audited gate-bypassing scenario injection CLI
  sub_process.py         THE approval gate + versioning + replay capture
game.py                  L1 sandbox RPG (intentionally buggy)
sim_runner.py            headless bot supervisor
migrate.py               single source of truth for the cardinal.db schema
tests/                   57 offline tests (chaos / agent markers)
```
