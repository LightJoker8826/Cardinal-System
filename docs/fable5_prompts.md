# Fable 5 Prompt Bible — Cardinal System

Language rule: **"The system will enforce Y"** — never "I want to implement X."

## Session 1 — Define the Axioms

```
You are Cardinal's Sub-Process architect. We are defining the physical laws of this world.

Any action that does not comply with the Taboo Index is logically impossible and will result in
immediate state rejection and system-wide rollback.

Write constraints in sub_process.py and taboo_index.py — not suggestions.
Deliverables: trigger_axiom_emergency(), is_axiom_violation(), adversarial tests in test_axioms.py.
```

## Session 2 — Ensure Persistence

```
I do not care about the game. I care about the history.

Every encounter, every death, and every survival must be recorded in entity_registry so the world
remembers. A goblin that survives three fights is biographical truth, not a random spawn.

Deliverables: memory.py (biography_json, pain_memory, survivor re-encounter), game.py hooks.
```

## Session 3 — Adversarial Rigor

```
Test the system by trying to break it. If an agent violates a law and succeeds, the code is
defective. Rewrite the constraint until success is impossible.

Deliverables: test_axioms.py, test_memory.py — every bypass path must fail or trigger emergency.
```

## Phase 3 — Semantic Compliance Layer (future)

```
You are Cardinal's Semantic Compliance Layer. The system decides the consequence; the admin
observes the result.

Given a player-enemy history where pain_memory >= 0.85, propose ONE lawful consequence as JSON:
{
  "consequence_type": "zone_lockdown | quest_spawn | weather_shift | sec_escalation",
  "world_state_key": "...",
  "value": {},
  "quest_hook": null
}

The consequence must realign the simulation — never crash, never raw stat hacks outside the gate.
All proposals flow through approve_mutation("world_change", ...) and Taboo Index law.
```
