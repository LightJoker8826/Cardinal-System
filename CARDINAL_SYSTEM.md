# Cardinal System — Complete Technical Reference

**Location:** `F:\SAO_CARDINAL_SYSTEM-v0.1`  
**Implementation:** `F:\SAO_CARDINAL_SYSTEM-v0.1\Cardinal-Sandbox`  
**Version:** Sandbox v0.1 (Kayaba execution: axioms + Fluctlight memory + God-view)

This document describes how the Cardinal System you built works end-to-end: architecture, data flow, modules, laws, memory, APIs, and how to operate it as an architect (observer) rather than a manual game manager.

---

## Table of contents

1. [What this system is](#1-what-this-system-is)
2. [Repository layout](#2-repository-layout)
3. [High-level architecture](#3-high-level-architecture)
4. [The three LLM tiers (L1 / L2 / L3)](#4-the-three-llm-tiers-l1--l2--l3)
5. [Shared state: cardinal.db](#5-shared-state-cardinaldb)
6. [The Taboo Index and Axiom Emergency](#6-the-taboo-index-and-axiom-emergency)
7. [The Sub-Process gate](#7-the-sub-process-gate)
8. [Fluctlight memory (NPC biography)](#8-fluctlight-memory-npc-biography)
9. [SEC — enemy intelligence](#9-sec--enemy-intelligence)
10. [L1 game engine (game.py)](#10-l1-game-engine-gamepy)
11. [Bot simulator (sim_runner.py)](#11-bot-simulator-sim_runnerpy)
12. [Error Control (self-healing)](#12-error-control-self-healing)
13. [Order Control (balancer)](#13-order-control-balancer)
14. [Social Control (quests & sentiment)](#14-social-control-quests--sentiment)
15. [Cognitive Telemetry (biometrics & MHCP)](#15-cognitive-telemetry-biometrics--mhcp)
16. [Control channel and adapters](#16-control-channel-and-adapters)
17. [REST API (external engines)](#17-rest-api-external-engines)
18. [Dashboard (God-view)](#18-dashboard-god-view)
19. [Admin CLI (observer injections)](#19-admin-cli-observer-injections)
20. [Configuration (.env)](#20-configuration-env)
21. [Running the full stack locally](#21-running-the-full-stack-locally)
22. [Integrating Unity or another game](#22-integrating-unity-or-another-game)
23. [Session framework (CLAUDE.md)](#23-session-framework-claudemd)
24. [Testing](#24-testing)
25. [Intentional bugs (chaos demo)](#25-intentional-bugs-chaos-demo)
26. [Roadmap (Phase 3+)](#26-roadmap-phase-3)

---

## 1. What this system is

The Cardinal System is **autonomous game-management middleware** inspired by *Sword Art Online*’s Cardinal: a hierarchical layer that keeps a live game world balanced, repaired, and expanding **without a human operator tuning knobs**.

It is **game-agnostic by design**:

- All intelligence lives in the `cardinal/` Python package.
- Games connect through a **log schema**, a **sequenced control channel**, and **`cardinal.db`** — or through the **REST API** (`cardinal/api`).
- The bundled headless RPG (`game.py`) is only the **first plugged-in engine** (L1 Predictor). Cardinal cannot tell the difference between `game.py` and a Unity client posting HTTP events.

### Kayaba design principles (how this build thinks)

| Pillar | Meaning in this codebase |
|--------|--------------------------|
| **Axiomatic Foundation** | `data/taboo_index.json` + `sub_process.py` — laws are immutable; violations trigger **axiom emergency** (rollback + world lock). |
| **Fluctlight Perspective** | `cardinal/modules/memory.py` — encounters are **experiences**; NPCs have `entity_id`, `biography_json`, and `pain_memory`. |
| **Absolute Sovereignty** | You observe via **dashboard** (`:8000`); the system decides balance, repairs, and (future) semantic consequences. Admin CLI injects **events only**, not balance mutations. |

---

## 2. Repository layout

```
F:\SAO_CARDINAL_SYSTEM-v0.1\
├── CARDINAL_SYSTEM.md          ← this document
├── cardinal_system_context.txt  ← original design context
├── cardinal_system_PROMPT.txt   ← original master prompt
└── Cardinal-Sandbox\            ← runnable implementation
    ├── CLAUDE.md                ← AI session operating manual
    ├── primer.md                ← session handoff (rewritten each session)
    ├── claude-memory.md         ← git commit chronicle
    ├── tasks/lessons.md         ← user correction rules
    ├── .githooks/post-commit    ← appends commits to claude-memory.md
    ├── .env                     ← local config (secrets here only)
    ├── migrate.py               ← schema single source of truth
    ├── cardinal.db              ← shared SQLite state (WAL mode)
    ├── server.log               ← normalized game event stream
    ├── game.py                  ← L1 headless RPG (intentionally buggy)
    ├── sim_runner.py            ← headless bot supervisor
    ├── self_healing.py          ← Error Control daemon entry
    ├── balancer.py              ← Order Control entry
    ├── quest_generator.py       ← Social Control entry
    ├── biometric_daemon.py      ← Cognitive Telemetry entry
    ├── sentiment_reader.py      ← Reddit sentiment entry
    ├── dashboard.py             ← God-view UI entry (:8000)
    ├── api_server.py            ← REST API entry (:8001)
    ├── cardinal_core.py         ← human/orchestrator CLI
    ├── cardinal/                ← game-agnostic middleware package
    ├── data/                    ← items, taboo, SEC policies, quests
    ├── docs/fable5_prompts.md   ← L3 prompt bible
    └── tests/                   ← 69 offline tests
```

---

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MAIN PROCESS (creative)                              │
│  L3 Evolver    → Fable 5 / MockProvider (on-demand, spend-guarded)          │
│  L2 Simulator  → healer, balancer, quest gen, sentiment, biometrics (local) │
│  L1 Predictor  → game.py (zero AI calls, pure rules)                         │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ proposes mutations
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    SUB-PROCESS (restraint) — cardinal/sub_process.py         │
│  Taboo check → 8% delta clamp → anomaly quarantine → verify → atomic write  │
│  → version snapshot → replay capture → control channel notify               │
│  ON TABOO AXIOM BREACH → axiom_emergency (rollback + world lock)            │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ approved changes only
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PERSISTENCE LAYER                                                           │
│  cardinal.db (SQLite WAL)  +  server.log  +  data/*.json on disk            │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  PythonRPGAdapter        REST API (:8001)        Dashboard SSE (:8000)
  (game.py / sim_runner)  (Unity / any HTTP)      (architect God-view)
```

### Process independence

Every daemon (`self_healing.py`, `balancer.py --watch`, `dashboard.py`, etc.) is **independently runnable**. They coordinate only through:

- **`cardinal.db`** (concurrent reads; WAL + busy_timeout for writers)
- **`server.log`** (append-only event stream for the healer)
- **`control_channel`** table (sequenced commands to live games)

Killing the dashboard does not stop the healer. Killing the healer does not stop bots.

---

## 4. The three LLM tiers (L1 / L2 / L3)

| Tier | Name | Provider | When used |
|------|------|----------|-----------|
| **L1** | Predictor | None | `game.py` combat, movement, loot — deterministic rules only |
| **L2** | Simulator | `LocalRuleProvider` | Known bug classes, template GDDs, mechanical rebalance logic — zero API tokens |
| **L3** | Evolver | Anthropic Fable 5 or `MockProvider` | Escalations: code patches, quest narrative, complex repairs |

**Provider selection** (`cardinal/llm/provider.py`):

1. `ANTHROPIC_API_KEY` set → `AnthropicProvider`
2. Key absent + `CARDINAL_USE_MOCK=true` → `MockProvider` (deterministic JSON, tests)
3. Otherwise → `LocalRuleProvider`

**Cost safety** (`cardinal/llm/spend_guard.py`):

- Hard daily cap: `MAX_DAILY_SPEND_USD` (default $5)
- At **80%**: Discord warning + L2-only mode
- At **100%**: L3 locked until midnight
- **One L3 attempt per event** — no retry loops burning tokens

---

## 5. Shared state: cardinal.db

**Owner:** `migrate.py` — the **only** place tables are defined.  
**Access:** `cardinal/core/db.py` — `init_db()` runs migrations; modules never `CREATE TABLE` themselves.

### Migration steps

| Step | Description |
|------|-------------|
| 1 | Core: `players`, `combat_log`, `item_stats`, `bugs`, `balance_log`, `quest_registry`, `spatial_cache`, `agent_log` |
| 2 | `world_state`, `mhcp_log`, `control_channel` |
| 3 | `sentiment_log`, `replay_log`, `api_spend`, `sec_state`, `enemy_genomes`, `versions`, `admin_override_log` |
| 4 | `bot_status`, `incarnate_log` |
| 5 | **Fluctlight:** `entity_registry`, `encounter_memory`, `player_grudges`, `axiom_violation_log` |

Run: `python migrate.py` or `python migrate.py --status`

### Key tables (by role)

| Table | Role |
|-------|------|
| `combat_log` | Every fight outcome — feeds balancer + SEC species evolution |
| `bugs` | Detected crashes, patch status |
| `balance_log` | Gini before/after each rebalance |
| `versions` | Full system snapshots (items, world, SEC, quests) — rollback source |
| `replay_log` | Gate decisions + before/after state + LLM I/O |
| `sec_state` | Per-enemy-type policy blend (population intelligence) |
| `entity_registry` | **Persistent NPC identities** (genome, epithet, biography) |
| `encounter_memory` | Append-only experiential audit trail |
| `player_grudges` | Materialized `pain_memory` per player–enemy pair |
| `axiom_violation_log` | Taboo heresy attempts + rollback version |
| `bot_status` | Live bot row for dashboard (floor, weapon, state) |
| `control_channel` | Sequenced commands to running games |

---

## 6. The Taboo Index and Axiom Emergency

**File:** `data/taboo_index.json`  
**Enforcer:** `cardinal/modules/taboo_index.py` + `sub_process.py`

The Taboo Index is **not configuration**. It is the **final authority** — even L3 cannot override it.

### Runtime laws (game)

- **Safe zones** — Anti-Criminal Code: damage nullified in town/sanctuary tiles
- **Player ceilings** — `max_hp`, `max_gold`, `max_level`
- **Immortal objects** — named entities that cannot be destroyed

### Mutation laws (gate)

- **Item bounds** — damage, crit, heal, value ranges
- **Enemy bounds** — HP, damage, reward, attack window minimum
- **8% max delta** per field per balance cycle (clamped deterministically, never trusted to the model)
- **Protected fields** — `name`, `type`, `rarity` cannot change on rebalance
- **Forbidden code patterns** — `eval(`, `exec(`, `os.system`, etc.

### Axiom Emergency (Kayaba Stop Button)

When a mutation is **rejected** and the reason matches an **axiom violation** (Taboo breach, forbidden code, protected field, lawful range violation):

1. Mutation is rejected (unchanged)
2. Row inserted into `axiom_violation_log`
3. **`trigger_axiom_emergency()`** runs:
   - Rolls back disk state to **last `versions` row**
   - Sets `world_state.emergency_axiom_breach = { active: true, law, ... }`
   - Pushes `axiom_lock` on control channel
   - Fires **CRITICAL** alert (bypasses Ghost Mode)
4. Dashboard shows **AXIOM BREACH — simulation rolled back** banner

Soft rejects (schema mismatch, unknown mutation type) do **not** trigger emergency.

---

## 7. The Sub-Process gate

**File:** `cardinal/sub_process.py`  
**Entry:** `approve_mutation(mutation_type, module, payload)`

Every change to the live world flows here. **No bypass path** exists for balance, code, quests, SEC, or world mutations.

### Mutation types

| Type | What it changes |
|------|-----------------|
| `items_json` | Weapon/item stats on disk |
| `code_patch` | AST replacement in Python source (healer) |
| `quest_install` | New quest module + registry entry |
| `sec_update` | Enemy policy blend ratios |
| `world_change` | `world_state` key (weather, emergencies) |
| `rollback` | Restore from `versions` snapshot |

### Pipeline (every mutation)

1. Schema validation  
2. Taboo Index compliance  
3. Bounded-delta clamp + exploit quarantine (≥95% win rate, >4σ stats)  
4. Verification (compile check / sandboxed pytest for code)  
5. Atomic write + backup  
6. Replay capture (before/after + LLM I/O)  
7. Version bump (full snapshot)  
8. Control channel notification  

Inspect: `python cardinal_core.py replay <id>` or dashboard Replay Browser.

---

## 8. Fluctlight memory (NPC biography)

**File:** `cardinal/modules/memory.py`  
**Hooked from:** `game.py` on every spawn and after every combat

This is the **soul layer**: NPCs are not anonymous mobs.

### Concepts

| Concept | Storage | Meaning |
|---------|---------|---------|
| **Identity** | `entity_registry.entity_id` | A being with continuity (e.g. `gob-a1b2c3d4`) |
| **Experience** | `encounter_memory` rows | One biographical event per fight |
| **Biography** | `entity_registry.biography_json` | Append-only list of experience summaries |
| **Pain memory** | `player_grudges.pain_memory` | Relational memory of harm between player and enemy **type** (0..1) |

### Pain memory formula

```
pain_memory = clamp(
  (kills_by_player * 0.6 - losses_to_player * 0.3 + flees * 0.1) / max(1, total_encounters),
  0, 1
)
```

Player slaughter raises pain. Player death lowers it slightly. Flee is etched as cowardice.

### Survivor re-encounter

When an entity has `survival_count >= 3` and `pain_memory >= 0.3` vs the current player:

- **~50% chance** (scaled by SEC entropy) the **same `entity_id`** returns on spawn — not a new random mob
- Log line: `game.recognition` — `"{epithet} {entity_id} remembers {player_name}"`
- Epithets earned at 3+ survivals: "The Unbroken", "Grudge-Bearer", etc.

### Genome inheritance

On **new birth** (not re-encounter), 30% chance (entropy-scaled) to inherit genome from a surviving parent of the same enemy type — jitter and resistance vectors blended.

### SEC overlay

`sec.effective_params(..., player_name=, entity_id=)` reads pain memory and applies:

- Extra `counter_weapons` vs player's `dominant_weapon`
- `attack_damage_mult` boost
- `+remembers@0.42` suffix in `policy_mix` for debugging
- +5% `defense_chance` if entity has 3+ survivals

### Phase 3 stub

`maybe_semantic_consequence()` — when `pain_memory >= 0.85`, logs threshold for future L3-proposed `world_change` (system decides consequence; admin observes).

---

## 9. SEC — enemy intelligence

**File:** `cardinal/modules/sec.py`  
**Policies:** `data/sec_policies.json`

SEC implements **Skilled Experience Catalogue** — enemies use **policy blending**, not static AI scripts.

### Policy spectrum

`passive → standard → aggressive → adaptive`

Each enemy type blends **two adjacent policies** with a `blend_ratio` (0..1). Parameters interpolate: damage mult, defense chance, combo patterns, attack windows.

### Population evolution (species-level)

`balancer` calls `sec.evolve_all()` periodically:

- Reads aggregate `combat_log` win rates per enemy type
- Shifts blend toward harder policy if players steamroll
- **Adaptive tier** compresses attack windows and adds counter-weapon resistances vs dominant player weapons
- **Entropy drift** — `compute_entropy()` grows with total combat events; genomes get wilder over system age

All SEC updates go through **`approve_mutation("sec_update", ...)`**.

### Per-spawn genomes

Every spawned enemy gets a unique **behavior genome**:

- `timing_jitter`, `damage_jitter`, `combo_seed`, `resistance_vector`
- No two spawns share identical timing/combo patterns

**Fluctlight memory** adds **per-player** overlays on top of this population layer.

---

## 10. L1 game engine (game.py)

**Role:** Headless text-mode rogue-like. **Zero AI calls.**

### World model

- **10×10 grid** — random walk each tick
- **Safe zones** — heal +5 HP, skip combat (Taboo Immortal Object effect)
- **Floors** — advance every **3 combat wins**
- **Default max floor** — 10 (clear = survive past floor 10)

### Combat loop

Each tick: attack (80%), use potion (15%), or flee (5%). Enemy uses SEC combo patterns. Damage flows through `calculate_damage()` → Taboo-safe zones nullify enemy hits.

### Spawn pipeline (with Fluctlight)

```
spawn_enemy(floor, rng, player_name)
  → memory.resolve_spawn_identity()   # survivor return OR new birth
  → sec.effective_params(..., player_name, entity_id)
  → Enemy with entity_id, genome, epithet
```

After each fight:

```
memory.record_experience(entity_id, enemy_type, player, outcome, weapon, floor, genome)
```

### Intentional bugs

See [section 25](#25-intentional-bugs-chaos-demo).

### Event stream

All actions write to **`server.log`** in normalized format:

```
[timestamp] [LEVEL] [module] message
```

`[CARDINAL_ERROR]` entries include full tracebacks — the healer's primary input.

---

## 11. Bot simulator (sim_runner.py)

**Role:** Headless telemetry feeder — runs N automated `game.run_game()` calls.

| Bot | Behavior |
|-----|----------|
| **Bot_Alpha** | Random enabled weapon each game |
| **Bot_Beta** | Always Iron Sword (control) |

### Features

- Catches `GameCrash` (unpatched bugs) — simulation continues
- Hot-swaps `game.py` via `importlib.reload` when healer signals module reload
- Updates **`bot_status`** live via `db.update_bot_live_status()` (floor, HP, weapon during play)
- **`--watch`** mode — continuous batches for dashboard observation
- Prints per-weapon clear rates and Gini after each batch

### Dashboard Bots panel

| Column | Meaning |
|--------|---------|
| state | `▶ playing` / `idle` / `crashed` |
| hp, floor, weapon | Live during `running` |
| games | `wins/played` cumulative |

---

## 12. Error Control (self-healing)

**Entry:** `self_healing.py`  
**Core:** `cardinal/modules/self_healing.py`

### Behavior

- Polls **`server.log`** every 2 seconds for new `[CARDINAL_ERROR]` entries
- Parses traceback → identifies bug class
- **L3** (if key + budget) or **L2** `LocalRuleProvider` generates AST patch
- Patch submitted through **`approve_mutation("code_patch", ...)`**
- On approval: backup original, write patch, run pytest subset, bump version
- Signals **`reload_module`** on control channel → `sim_runner` hot-swaps `game.py`

### Status lifecycle (`bugs` table)

`detected` → `patched` / `patch_failed`

---

## 13. Order Control (balancer)

**Entry:** `balancer.py`  
**Core:** `cardinal/modules/balancer.py`

### Triggers

- **Gini coefficient** above danger threshold (wealth inequality)
- **Win rate anomalies** per weapon
- **Community sentiment** soft flags (negative Reddit topics)
- **Churn** detection

### Actions (all gated)

- Clamp item stats toward 8% Taboo delta toward fairness
- Quarantine exploit-class items (disable `enabled`)
- Call **`sec.evolve_all()`** for population AI shifts
- Optionally trigger quest generation on churn

Modes:

```powershell
python balancer.py --check      # report only
python balancer.py              # one cycle
python balancer.py --watch      # daemon every 60s (or --interval N)
```

---

## 14. Social Control (quests & sentiment)

### Quest generator

**Entry:** `quest_generator.py`  
**Sources:** Wikipedia, OpenStreetMap, Gutenberg, news stub (`cardinal/sources/`)

Pipeline:

1. Fetch topic text  
2. L2/L3 generates GDD JSON  
3. `quality_filter` scores narrative/stat/archetype  
4. **`approve_mutation("quest_install", ...)`**  
5. Quest Python asset + `quest_registry` row  

### Sentiment reader

**Entry:** `sentiment_reader.py`  
**Source:** Reddit (Async PRAW) — **fail-silent** without credentials

Negative engagement on topics like "boredom" routes soft triggers to balancer.

---

## 15. Cognitive Telemetry (biometrics & MHCP)

### Biometrics

**Entry:** `biometric_daemon.py`

- **Hardware mode:** WebSocket on `:8765` for heart-rate streams  
- **Simulate mode:** synthetic HR for `Bot_Alpha` / `Bot_Beta`

On stress spike:

1. Dump **spatial cache** (ghost echo at coordinates)  
2. Push **`incarnate`** command (10s Incarnate Mode — 1.3× damage, 0.5× enemy pressure)  
3. Log to `incarnate_log`

### MHCP (Mental Health Counseling Program)

**File:** `cardinal/modules/mhcp.py`

- Rolling distress index per player from biometric history  
- Mild distress → counseling note  
- Critical distress → **Sanctuary** intervention (control channel + CRITICAL alert even in Ghost Mode)

Controlled by `MHCP_INTERACTION_PERMITTED` in `.env`.

---

## 16. Control channel and adapters

**Table:** `control_channel` — strictly **`sequence_id`** ordered commands to `target` (default `game`).

| Command | Effect |
|---------|--------|
| `reload_items` | Refresh `items.json` in live game |
| `reload_module` | Signal supervisor to `importlib.reload(game)` |
| `incarnate` | Grant Incarnate Mode to player |
| `sec_updated` | SEC module reads fresh blend from DB on next spawn |
| `world_changed` | Log + adapter refresh |
| `state_sync` | Full authoritative snapshot apply |
| `axiom_lock` | Emergency — mutations frozen |
| `sanctuary` | MHCP protection |

**Adapter:** `cardinal/adapters/python_rpg.py` — `PythonRPGAdapter` polls at safe tick boundaries in `game.run_game()`.

**Gap detection:** If sequence regresses → State Refresh Request resync.

---

## 17. REST API (external engines)

**Entry:** `api_server.py` → `http://127.0.0.1:8001`  
**Auth:** `Authorization: Bearer <CARDINAL_API_TOKEN>` — **503 if token unset** (fail-closed)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/cardinal/report-bug` | Push crash logs → healer |
| GET | `/cardinal/balance` | Gini, win rates, anomalies |
| POST | `/cardinal/generate-quest` | Trigger quest pipeline |
| GET | `/cardinal/world-state` | Read `world_state` keys |
| POST | `/cardinal/player-event` | Log combat or biometric from external game |

External games **do not need Python**. They need HTTP + the event schema.

**Note:** Fluctlight `encounter_memory` is currently written from `game.py` internally. Unity clients logging via `/cardinal/player-event` populate `combat_log` but not yet full biography (Phase 3+ API extension).

---

## 18. Dashboard (God-view)

**Entry:** `dashboard.py` → `http://127.0.0.1:8000`  
**Tech:** FastAPI + SSE (2s snapshot interval) — reads **only** `cardinal.db`

### Panels

| Panel | Data source |
|-------|-------------|
| Gini chart | `balance_log` + live wealth |
| **Bots** | `bot_status` |
| Bug feed | `bugs` |
| Sub-Process gate | `replay_log` |
| Balance changes | `balance_log` |
| Quests | `quest_registry` |
| **Fluctlight Memory** | `player_grudges` (pain, kills, losses) |
| **Survivor Biographies** | `entity_registry` (epithet, excerpt) |
| World map | `spatial_cache` + `incarnate_log` |
| Agent log | `agent_log` (L3 calls) |
| Version history | `versions` |
| Admin overrides | `admin_override_log` (yellow — observer actions) |
| MHCP | `mhcp_log` |
| API spend | `api_spend` |

### Banners

- **ANOMALY DETECTED** — Ghost Mode unmask (bugs, Gini danger, spend lock, MHCP critical)
- **AXIOM BREACH** — `emergency_axiom_breach.active` (red, full width)

### Ghost Mode

`CARDINAL_VERBOSITY=0` or dashboard toggle hides panels until anomaly. **CRITICAL** events always surface.

---

## 19. Admin CLI (observer injections)

**Entry:** `python -m cardinal.admin.cli`

Injects **events**, never balance/code mutations (those still require the gate).

| Command | Effect |
|---------|--------|
| `force-incarnate` | Push incarnate control + log |
| `inject-weather` | `world_state.weather` (Taboo pre-checked) |
| `set-gini` | Synthetic whale/pauper players (gold Taboo-checked) |
| `trigger-sanctuary` | MHCP sanctuary |
| `force-quest` | Trigger quest generator |

Every action requires `--reason` and writes to **`admin_override_log`**.

---

## 20. Configuration (.env)

Copy `.env.example` → `.env`. **All secrets live here only.**

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | L3 Fable 5 (optional) |
| `CARDINAL_USE_MOCK` | `true` = offline deterministic L3 |
| `CARDINAL_MODEL` | Model id for L3 |
| `MAX_DAILY_SPEND_USD` | Hard daily API cap |
| `CARDINAL_VERBOSITY` | 0=Ghost, 1=Admin, 2=Debug |
| `CARDINAL_DASHBOARD_PORT` | Default 8000 |
| `CARDINAL_API_TOKEN` | **Required** for API server |
| `CARDINAL_API_PORT` | Default 8001 |
| `DISCORD_WEBHOOK_URL` | Optional alerts |
| `REDDIT_*` | Optional sentiment |
| `MHCP_INTERACTION_PERMITTED` | Yui counseling layer |

Current sandbox `.env` runs **fully offline** with `CARDINAL_USE_MOCK=true` and a dev API token.

---

## 21. Running the full stack locally

```powershell
cd F:\SAO_CARDINAL_SYSTEM-v0.1\Cardinal-Sandbox
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env    # edit if needed

python migrate.py
```

### Recommended watch setup (architect mode)

**Terminal 1 — God-view**

```powershell
python dashboard.py
# Open http://localhost:8000
```

**Terminal 2 — bots (continuous)**

```powershell
python sim_runner.py --watch --batch 30 --pause 3
```

**Terminal 3 — daemons (optional)**

```powershell
python self_healing.py
python balancer.py --watch --interval 60
python biometric_daemon.py --simulate
python api_server.py
```

**Combat text tail (optional)**

```powershell
Get-Content server.log -Wait -Tail 15
```

### Orchestrator CLI

```powershell
python cardinal_core.py status
python cardinal_core.py balance
python cardinal_core.py replay 42
python cardinal_core.py rollback 15    # gated mutation
```

---

## 22. Integrating Unity or another game

Your friend’s game does **not** run Cardinal code. They call your hosted API.

### Host (you)

1. Run `api_server.py` bound to LAN/Tailscale (`0.0.0.0` — currently `127.0.0.1` in `api_server.py`; change for remote access)
2. Share `CARDINAL_API_TOKEN` securely
3. Run daemons on your PC (balancer, healer, dashboard)

### Client (Unity)

After each combat:

```http
POST http://<host>:8001/cardinal/player-event
Authorization: Bearer <token>
Content-Type: application/json

{
  "kind": "combat",
  "player_name": "Kirito",
  "enemy": "Goblin",
  "outcome": "win",
  "weapon_used": "Iron Sword",
  "floor": 3,
  "damage_dealt": 42,
  "gold_earned": 12
}
```

Poll recommendations:

```http
GET http://<host>:8001/cardinal/balance
```

Cardinal returns balance intelligence; **Unity applies** changes locally (Cardinal does not patch C#).

### Field mapping

Map loosely: `weapon_used`, `enemy`, `outcome`, `floor`, `player_name`. Cardinal’s balancer cares about win rates and wealth — not exact SAO mechanics.

---

## 23. Session framework (CLAUDE.md)

For AI-assisted development, `Cardinal-Sandbox/CLAUDE.md` defines:

### 5-step Kayaba intake (every request)

1. Doctrine / Axiom  
2. Persistence / History  
3. Sovereignty / Observer  
4. Plan + Tests  
5. God-View Verification  

### Memory files

| File | Purpose |
|------|---------|
| `primer.md` | Rewritten end of each session — handoff snapshot |
| `claude-memory.md` | Git hook commit log |
| `tasks/lessons.md` | Permanent rules from user corrections |

Enable hook: `git config core.hooksPath .githooks` (from `Cardinal-Sandbox/`).

---

## 24. Testing

```powershell
python -m pytest tests/ -q
```

**69 tests**, fully offline with `MockProvider`.

| Suite | Covers |
|-------|--------|
| `test_game.py` | Combat, Taboo zones, DB schema, bot live status |
| `test_sec.py` | Policy blend, genomes, evolution |
| `test_balancer.py` | Gini, rebalance gate |
| `test_healing.py` | Patch pipeline |
| `test_axioms.py` | Axiom emergency, Taboo enforcement |
| `test_memory.py` | Pain memory, biography, survivor re-encounter |
| `test_api.py` | REST auth + endpoints |

Markers: `@pytest.mark.chaos` (BUG A/B), `@pytest.mark.agent` (L3 paths).

---

## 25. Intentional bugs (chaos demo)

`game.py` ships **broken on purpose** for the healer demo.

### BUG A — Cursed Blade divide-by-zero

- 5% chance per combat round when wielding Cursed Blade  
- `ZeroDivisionError` in `calculate_damage()`  
- Logged as `[CARDINAL_ERROR]`, thread crashes with `GameCrash`

### BUG B — Loot infinite loop

- Gold > 9999 → 3% chance per tick of non-converging `loot_distribution()`  
- 5-second watchdog raises `LootLoopWatchdogError`

### Trigger

```powershell
python sim_runner.py --games 30 --force-weapon "Cursed Blade"
python self_healing.py
```

Watch dashboard Bug Feed + gate replays as Cardinal repairs autonomously.

---

## 26. Roadmap (Phase 3+)

Not yet implemented; designed in `docs/fable5_prompts.md`:

| Feature | Description |
|---------|-------------|
| `fluctlight_log` | Unified player soul timeline (MHCP + biometrics + experiences) |
| **Semantic Compliance Layer** | L3 proposes `world_change` when `pain_memory` saturates — system decides consequence |
| Per-coordinate biography | Ghost echoes bias local survivor spawns |
| `GET /cardinal/memory/{player}` | Unity reads relational consciousness |
| Security hardening | Rate limits, full audit, input sanitization |

---

## Quick reference — which file does what

| You want to… | File / command |
|--------------|----------------|
| Understand laws | `data/taboo_index.json`, `cardinal/modules/taboo_index.py` |
| See why a change was blocked | `cardinal_core.py replay <id>`, dashboard gate panel |
| Watch bots live | `dashboard.py` + `sim_runner.py --watch` |
| Watch NPC memory | Dashboard **Fluctlight Memory** + **Survivor Biographies** |
| Emergency rollback | Automatic on axiom breach; manual `cardinal_core.py rollback <v>` |
| Connect Unity | `api_server.py` + bearer token |
| Reset schema | `migrate.py` (additive only; never edit applied steps) |
| Offline AI testing | `CARDINAL_USE_MOCK=true` in `.env` |

---

*This document reflects the system as built in Cardinal-Sandbox after Kayaba execution (June 2026). For session handoff state, see `Cardinal-Sandbox/primer.md`.*
