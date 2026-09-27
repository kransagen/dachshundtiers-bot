# PostgreSQL Migration — Architecture & Schema Design

**Status:** Design (no code implemented)
**Repo:** `kransagen/dachshundtiers-bot` @ `8373e21`
**Date:** 2026-09-25

This document is the design deliverable for migrating the bot from JSON/file
storage to a normalized PostgreSQL backend. It resolves the critical constraint
up front:

> **Discord is the only authority for CURRENT tier state. The database is a
> mirror that records what Discord has confirmed. Nothing in the system may
> silently accept "DB says new tier, Discord still has old tier" as a final
> state.**

---

## 1. CURRENT ARCHITECTURE

### 1.1 Stores

| Store | Contents | Authority? |
|---|---|---|
| Discord guild | members + tier roles | **YES — current tier** |
| `data/players.json` | `[{username, discordId?, modes: {kit: tier}, history: {kit: [{date, tier}]}}]` | No (mirror + history, drifted) |
| `data/kit_roles.json` | `{kit_key: {tier: role_id}}` (gitignored runtime config) | No (config) |
| `data/*.json` runtime | queue.json, active_queues.json, queue_messages.json, pulled_players.json, ht_tickets.json, ht_ticket_logs.json, ht_results.json, ht3_cooldowns.json, cooldowns.json, evals.json, kits.json, testers.json, testers_stats.json, tournaments.json | No |
| `data/*_log.json` | playersync_log, playersync_rollback_log, websync_log, datacheck_log, checkweb_log, edituser_log | No (audit) |
| PostgreSQL JSONB (optional) | table `dachshundtiers_data` (key → JSONB), one row per "file" | No (simulated JSON) |
| GitHub `players.json` | exported copy | No (presentation artifact) |

### 1.2 Writers / readers

| Component | Reads | Writes |
|---|---|---|
| `storage.py` | all `data/*.json` (or PG JSONB when `DATABASE_URL` set) | all of the above |
| `services/store.py` | per-file, under loop-scoped `asyncio.Lock` | transactional saves (strict mode, `DataCorruptionError`) |
| `/sync discord` (`playersync`) | players.json + kit_roles.json vs guild members | **Discord roles** (apply = destructive add/remove) |
| `/sync discord-rollback` | playersync_rollback_log.json | **Discord roles** (inverse ops) |
| `/sync web` (`websync` → `github_sync`) | players.json | GitHub players.json |
| `/sync check` (`checkweb`) | Discord roles, players.json, GitHub players.json | checkweb_log.json (per-record decisions: use_discord / keep_database / ignore) |
| `/sync data` (`datacheck`) | all local databases | safe repairs only (tier normalization, closing orphan tickets) + datacheck_log.json |
| `/result` | players.json, ht_results.json, ht_tickets.json, cooldowns, evals | players.json (new tier + history), ht_results.json, ht_tickets.json (close), ht3_cooldowns.json, ht_ticket_logs.json, **Discord role via `auto_grant_kit_role`** |
| `/topresult` (HT Fight) | same | same; win promotes (`next_ticket_tier` / bridge), loss closes fight ticket + cooldown |
| `/edituser` | players.json, cooldowns, tickets, results, queue, pulled_players | players.json + **identity key remap across 7 files** + edituser_log.json + web push (best effort) + Discord role plan (best effort) |
| queues / tickets / cooldowns / evals | their JSON files via `store` | via `store.transaction` |
| views.py / panel.py / cogs | various data files directly (`load_data`/`save_data` without store) | queue_messages, ht3_panel_message, tournaments, cooldowns |
| `/setkitrole`, `/unsetkitrole`, `/kitrole` | kit_roles.json | kit_roles.json (config) |

### 1.3 Critical current defects

1. **Two "databases"**: JSON files and the PG JSONB store are parallel sims —
   no schema, no FKs, no querying.
2. **DB→Discord drift is a silent final state**: `/result` writes players.json
   *then* grants the role; a failed grant leaves DB≠Discord with no automatic
   reconciliation (only manual `/sync discord` / `/checkweb`).
3. **No observation path**: the DB never *learns* Discord state unless an
   operator runs manual sync commands.
4. **Identity gap**: most `players.json` records have **no `discordId`**; IGN
   (mutable) is the only key for legacy records.
5. **Time discipline**: ms-epoch timestamps and `DD.MM.YYYY` strings; no
   timezone-aware datetimes.
6. **Config on disk**: `kit_roles.json` is gitignored runtime state — unaudited,
   no fail-fast startup validation, no FK to kits/tiers.
7. **Concurrency**: some cogs read/write via raw `load_data/save_data` without
   `store` locks.

---

## 2. TARGET ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────┐
│ AUTHORITATIVE CURRENT STATE:  Discord (roles per guild member) │
│   - the ONLY thing that decides what a player's current tier IS│
└──────────────┬──────────────────────────────────────────────────┘
               │ DISCORD → PG only (observation, sync, promotion confirm)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│ PERSISTED MIRROR:  PostgreSQL (SQLAlchemy 2.x async + asyncpg)  │
│   player_current_tiers  = last confirmed Discord observation    │
│   results / tickets / cooldowns / queues / evals (operational)  │
│   audit_logs / sync_runs / sync_actions / outbox_events         │
└───────┬──────────────────────────────┬──────────────────────────┘
        │ read                        │ read (export/render only)
        ▼                             ▼
┌──────────────────────────┐  ┌───────────────────────────────┐
│ HISTORICAL DATA:         │  │ PRESENTATION: Web / API       │
│ tier_history (append-only)│  │   reads PG directly, or the  │
│ results (append-only)     │  │   generated GitHub artifact  │
│ cooldown/ticket events    │  │   (non-authoritative)        │
└──────────────────────────┘  └───────────────────────────────┘

LEGACY: data/players.json + GitHub players.json
   - frozen input for migration (Phase C)
   - archived read-only after Phase H
   - NEVER written at runtime after Phase G
   - NEVER consulted for current-tier authority after Phase D
```

**Direction rules (enforced):**

| Direction | Allowed? | Where |
|---|---|---|
| Discord → PG | ✅ always | sync, reconciliation, promotion confirm, event observation |
| PG → Discord | ❌ normal sync | forbidden (report anomalies only) |
| PG → Discord | ⚠️ explicit actions only | promotion role grant (step 1 of `/result`), `/sync discord-rollback` (disaster recovery), admin manual tools |
| Web → tier state | ❌ never | web is read-only |
| GitHub → PG | ❌ runtime | import path only via explicit migration tooling |

---

## 3. DATA FLOW DIAGRAMS

### A) Discord role change → DB (observation / reconciliation)

```
guild member roles (Discord)
   │  read via kit_roles config (role_id → kit+tier)
   ▼
classify per (member, kit):
   0 tier roles                 → anomaly: missing_tier
   >1 tier roles (same kit)     → anomaly: multiple_tier_roles
   role not in kit_roles        → anomaly: unknown_role
   role maps to retired tier    → anomaly: retired_role   (reported, preserved)
   config: role_id missing from
     guild / maps to 2+ kits    → anomaly: missing_configured_role / conflicting
   ▼
sync_actions rows (read-only classification, NO Discord mutation)
   ▼
PG: player_current_tiers  (observed_at, source='discord_sync', sync_run_id)
      └─ tier changed vs current? → tier_history row (previous_tier_id)
   audit_logs
```

Does **not** use `add_roles`/`remove_roles` anywhere.

### B) Explicit promotion `/result` → Discord → DB (the resolved flow)

```
/result (tester)
   │  validate: ticket ownership/kit/status, ladder, cooldown, dedup,
   │            eval gate, bridge rules
   ▼
result row: promotion_status = 'discord_pending'      (audit trail)
   │
   ▼  STEP 1 — DISCORD FIRST (await, confirm)
member.edit(roles = desired_kit_roles)   # single atomic call
   │
   ├─ FAIL ────────────────► promotion_status='discord_failed'
   │                         NO tier change in PG. Respond to tester.
   │                         audit_logs: promotion_discord_failed
   │
   ▼  STEP 2 — DISCORD CONFIRMED
BEGIN;                                                     (one DB tx)
   results:      promotion_status = 'committed'
   player_current_tiers  ← tier, observed_at=now, source='promotion',
                           discord_role_id=target role id, result_id
   tier_history  ← row (only if tier actually changed; previous_tier_id)
   cooldowns     ← HT3+/waitlist per ticket semantics
   tickets       ← close if applicable (HT3 eval ticket / fight loss)
   audit_logs    ← promotion_committed
COMMIT;
   │
   ├─ COMMIT OK ───────────► announcement embed → result channel (best effort)
   │                         dedup: result_key unique, ticket=1 result
   │
   └─ DB FAIL ─────────────► outbox_events row 'promotion_commit'
                             (discord_role_confirmed = true)
                             if PG unreachable: append data/local_outbox.json
                             reconciler aligns PG to Discord. NEVER revert Discord.
```

### C) Periodic reconciliation

```
startup / timer (configurable) / on_member_update events
   │
   ▼
sync_run (mode='automatic')
   │
   ├─ drain outbox_events (promotion_commit)   → verify Discord state → apply → done
   ├─ scan members ↔ kit_roles ↔ current_tiers → update mirror + anomalies
   │     (idempotent: same observation → no history row, "No changes.")
   └─ audit_logs
```

### D) Web read

```
Web/API handler
   │  look up by discord_id (or IGN)   ─── presentation only
   ▼
PG: player_current_tiers + tier_history + cooldowns/results summaries
   ├─ direct read (web has read-only PG access)   [recommended]
   └─ or generated artifact: exporter builds players.json shape from PG
        → optional GitHub push (legacy, non-authoritative)
```

### E) Rollback of historical bad `/sync discord`

```
/sync discord-rollback (admin, preview/apply)
   │  reads playersync_rollback_log.json (+ rows migrated to audit_logs)
   │  each entry: original op (ADD/REMOVE) + member + role
   ▼
reverse op on Discord   ← THE ONLY time audit history drives Discord
   │  idempotent: already_correct ⇒ no API call
   ▼
append to rollback log + audit_logs
   │
   ⚠ COMPLETELY separate code path from normal sync. Never triggered by
     reconciliation. Exists to undo pre-migration destructive applies;
     frozen once migration completes.
```

---

## 4. NORMALIZED DATABASE SCHEMA

Conventions: snake_case; snowflake IDs as `BIGINT`; internal PKs `BIGINT
IDENTITY`; timestamps `TIMESTAMPTZ` **UTC** (Europe/Prague only for
presentation); legacy ms-epochs converted with `to_timestamp(ms/1000.0)`.
Alembic manages migrations. JSONB used only where a schema honestly cannot
help (opaque payloads, config values).

### 4.1 `players`

```sql
CREATE TABLE players (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    discord_id    BIGINT,                    -- NULL = legacy/unclaimed record
    ign           TEXT NOT NULL,             -- mutable IGN (Minecraft name)
    source        TEXT NOT NULL DEFAULT 'discord',  -- 'migration' | 'discord'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_players_discord ON players (discord_id) WHERE discord_id IS NOT NULL;
CREATE UNIQUE INDEX uq_players_ign      ON players (lower(ign));
```

- **PK:** `id`. **FKs:** none (referenced by everything else).
- **Indexes:** above; plus `(discord_id)` for lookups.
- **Uniques:** partial `discord_id`; `lower(ign)`.
- **Nullable:** `discord_id` (legacy rows).
- **Authoritative source:** identity claimed via explicit actions (user
  interactions / `/edituser` / migration) — Discord ID is the stable key.
- **Lifecycle:** created on first claim/result/observation; IGN may be renamed
  (`claim_ign` semantics, never merges players); never hard-deleted. A record
  that fails identity resolution during migration lands in
  `migration_import_issues` instead of being dropped.

### 4.2 `kits`

```sql
CREATE TABLE kits (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,   -- display "MolePVP"
    key        TEXT NOT NULL UNIQUE,   -- lowercase "molepvp"
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- **Authoritative:** seeded from `kits.json`; admin-managed (`/addkit`).
  **Lifecycle:** immutable name/key; `active` flag deactivates.

### 4.3 `tier_definitions`

```sql
CREATE TABLE tier_definitions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,           -- 'HT3', 'LT3E', 'S', 'RHT3'
    kind          TEXT NOT NULL,                  -- 'ladder'|'tournament'|'retired'|'virtual'
    rank          INT,                            -- ladder order (NULL for retired/tournament)
    display_name  TEXT NOT NULL,
    is_retired    BOOLEAN NOT NULL DEFAULT FALSE,
    retired_of_id BIGINT REFERENCES tier_definitions(id),  -- RHT3 → HT3
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- **Seeded from:** `HT3_TIER_LADDER` (+ virtual `LT3E`), tournament `S/A/B/C/D/E`,
  retired `R`-prefix variants. **Lifecycle:** append-only; new tiers via migration.

### 4.4 `kit_roles`  ← replaces `kit_roles.json`

```sql
CREATE TABLE kit_roles (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kit_id          BIGINT NOT NULL REFERENCES kits(id),
    tier_id         BIGINT NOT NULL REFERENCES tier_definitions(id),
    discord_role_id BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_kit_roles_kit_tier UNIQUE (kit_id, tier_id),
    CONSTRAINT uq_kit_roles_role    UNIQUE (discord_role_id)
);
CREATE INDEX ix_kit_roles_role ON kit_roles (discord_role_id);
```

- **Authoritative:** admin config (`/setkitrole`, `/unsetkitrole`) writing the
  DB. A role may map to exactly one (kit, tier) — duplicate-role conflicts
  (today's `check_kit_roles_conflicts`) become impossible by constraint.
- **Lifecycle:** rows added/updated/removed by admins only; never auto-created;
  deleted mapping ⇒ that role observed on a member becomes `unknown_role`.
- **Startup validation (fail-fast):** see §7. Role values are **IDs, never names**.

### 4.5 `player_current_tiers`  ← the Discord mirror (no independent authority)

```sql
CREATE TABLE player_current_tiers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id       BIGINT NOT NULL REFERENCES players(id),
    kit_id          BIGINT NOT NULL REFERENCES kits(id),
    tier_id         BIGINT NOT NULL REFERENCES tier_definitions(id),
    discord_role_id BIGINT,                       -- role that confirmed this tier
    observed_at     TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,                -- 'discord_sync'|'promotion'|'manual'
    sync_run_id     BIGINT REFERENCES sync_runs(id),
    result_id       BIGINT REFERENCES results(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cur_tier_player_kit UNIQUE (player_id, kit_id)
);
CREATE INDEX ix_cur_tier_kit  ON player_current_tiers (kit_id, tier_id);
CREATE INDEX ix_cur_tier_role ON player_current_tiers (discord_role_id);
```

- **PK/FKs/indexes/uniques:** above. **Nullable:** `discord_role_id` (role
  deleted or tier not role-mapped), `sync_run_id`, `result_id`.
- **Authoritative source:** *not* authoritative — it is the **last successfully
  observed/confirmed Discord state** (see §5).
- **Lifecycle:** row update (never vertical delete) on every confirmation;
  same-tier re-observation refreshes `observed_at` only (no history spam).

### 4.6 `tier_history`  ← append-only

```sql
CREATE TABLE tier_history (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id        BIGINT NOT NULL REFERENCES players(id),
    kit_id           BIGINT NOT NULL REFERENCES kits(id),
    tier_id          BIGINT NOT NULL REFERENCES tier_definitions(id),
    previous_tier_id BIGINT REFERENCES tier_definitions(id),
    changed_at       TIMESTAMPTZ NOT NULL,
    source           TEXT NOT NULL,          -- 'promotion'|'discord_sync'|'migration'|'manual'|'rollback'
    result_id        BIGINT REFERENCES results(id),
    sync_run_id      BIGINT REFERENCES sync_runs(id),
    actor_id         BIGINT,                 -- Discord snowflake of actor
    actor_name       TEXT,
    reason           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_th_player_kit_time ON tier_history (player_id, kit_id, changed_at DESC);
CREATE INDEX ix_th_kit ON tier_history (kit_id);
```

- **Append-only.** No UPDATE/DELETE (enforced at repository layer).
- **Transition detection:** on every observation/promotion, compare incoming
  tier to the committed row in `player_current_tiers`; insert history **only**
  when they differ (`previous_tier_id` = old). Repeated reconciliation of the
  same tier produces **no** history row (§6).

### 4.7 `results`  ← replaces `ht_results.json`

```sql
CREATE TABLE results (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_key             TEXT NOT NULL UNIQUE,   -- legacy dict key (ticketId, {ticketId}:ht_fight, queue-…)
    kind                   TEXT NOT NULL,          -- 'ticket'|'queue'|'ht_fight'
    subtype                TEXT,                   -- 'ht_fight_ticket'|'ht_fight_free'|NULL
    player_id              BIGINT NOT NULL REFERENCES players(id),
    evaluator_id           BIGINT REFERENCES players(id),
    kit_id                 BIGINT NOT NULL REFERENCES kits(id),
    ticket_channel_id      BIGINT,                 -- legacy ticketId (channel snowflake)
    previous_tier_id       BIGINT REFERENCES tier_definitions(id),
    new_tier_id            BIGINT REFERENCES tier_definitions(id),
    bridge_tier_id         BIGINT REFERENCES tier_definitions(id),
    tier_status            TEXT,                   -- display string
    score                  TEXT,
    outcome                TEXT,                   -- 'Won'|'Lost'
    opponent_id            BIGINT,
    opponent_name          TEXT,
    notes                  TEXT,
    eval_flag              BOOLEAN NOT NULL DEFAULT FALSE,
    date                   TEXT,                   -- legacy display date
    recorded_at            TIMESTAMPTZ NOT NULL,
    promotion_status       TEXT,                   -- 'discord_pending'|'discord_failed'|'committed'|'db_failed_outboxed'
    announcement_status    TEXT,                   -- 'pending'|'sent'|'failed'
    announcement_message_id BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_results_player_kit_time ON results (player_id, kit_id, recorded_at DESC);
CREATE INDEX ix_results_ticket ON results (ticket_channel_id);
CREATE INDEX ix_results_promo  ON results (promotion_status) WHERE promotion_status IS NOT NULL;
```

- **Idempotency:** `result_key` UNIQUE (ticket = one result; fight ticket key
  `{ticketId}:ht_fight`; fight dedup window handled at service layer).
- **Lifecycle:** insert once; `promotion_status` transitions (state machine §10);
  `announcement_status` updated; never hard-deleted.

### 4.8 `tickets` + `ticket_members`  ← replaces `ht_tickets.json` / member lists

```sql
CREATE TABLE tickets (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id      BIGINT NOT NULL UNIQUE,   -- legacy "id"
    player_id       BIGINT NOT NULL REFERENCES players(id),
    owner_name      TEXT,
    ign             TEXT NOT NULL,            -- snapshot at open
    kit_id          BIGINT NOT NULL REFERENCES kits(id),
    target_tier_id  BIGINT REFERENCES tier_definitions(id),
    current_tier_id BIGINT REFERENCES tier_definitions(id),  -- snapshot at open
    eval            BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT NOT NULL DEFAULT 'open',   -- 'open'|'closed'
    claimer_id      BIGINT REFERENCES players(id),
    claimer_name    TEXT,
    category_id     BIGINT,
    panel_message_id BIGINT,
    ticket_type     TEXT NOT NULL DEFAULT 'eval',   -- 'eval'|'fight'
    created_at      TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX uq_tickets_open_player_kit ON tickets (player_id, kit_id) WHERE status = 'open';
CREATE INDEX ix_tickets_claimer ON tickets (claimer_id);

CREATE TABLE ticket_members (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id  BIGINT NOT NULL REFERENCES tickets(id),
    player_id  BIGINT NOT NULL REFERENCES players(id),
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at TIMESTAMPTZ,
    CONSTRAINT uq_ticket_member UNIQUE (ticket_id, player_id, added_at)
);
```

- **Lifecycle:** created at ticket open; claim/unclaim/close/reopen mutate the
  row; closed rows are kept (channel retained for Reopen/log); channel deleted
  by Discord → `orphaned` anomaly (datacheck), never auto-delete.

### 4.9 `cooldowns`

```sql
CREATE TABLE cooldowns (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id     BIGINT NOT NULL REFERENCES players(id),
    cooldown_type TEXT NOT NULL,          -- 'waitlist'|'ht3'
    kit_id        BIGINT REFERENCES kits(id),   -- NULL for waitlist
    expires_at    TIMESTAMPTZ NOT NULL,
    source        TEXT NOT NULL DEFAULT 'auto', -- 'ht3_close'|'edituser'|'migration'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cooldown UNIQUE (player_id, cooldown_type, kit_id)
);
CREATE INDEX ix_cooldowns_expiry ON cooldowns (expires_at);
```

- **Lifecycle:** upsert per (player, type, kit). Waitlist type with NULL kit
  preserves the per-player 4-day rule; `ht3` per kit. Expiry is computed, not
  cleaned (rows are history when queried with `expires_at > now()`).

### 4.10 `queues` + `queue_entries`

```sql
CREATE TABLE queues (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kit_id     BIGINT NOT NULL REFERENCES kits(id),
    name       TEXT NOT NULL,                  -- display name
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ,
    panel_channel_id BIGINT,
    panel_message_id BIGINT
);
CREATE UNIQUE INDEX uq_queue_active_kit ON queues (kit_id) WHERE closed_at IS NULL;

CREATE TABLE queue_entries (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    queue_id       BIGINT NOT NULL REFERENCES queues(id),
    player_id      BIGINT NOT NULL REFERENCES players(id),
    ign            TEXT NOT NULL,              -- snapshot at join
    kit_id         BIGINT NOT NULL REFERENCES kits(id),
    position       INT NOT NULL,               -- order within queue
    status         TEXT NOT NULL DEFAULT 'waiting',  -- 'waiting'|'pulled'|'left'|'tested'
    joined_at      TIMESTAMPTZ NOT NULL,
    pulled_at      TIMESTAMPTZ,
    room_channel_id BIGINT,
    removed_at     TIMESTAMPTZ,
    removed_reason TEXT
);
CREATE UNIQUE INDEX uq_queue_waiting_player ON queue_entries (queue_id, player_id) WHERE status = 'waiting';
CREATE INDEX ix_queue_position ON queue_entries (queue_id, position);
```

- **Lifecycle:** opening a queue = new `queues` row (or reopening); join/leave/
  pull/test transition entry `status`; pulled players tracked here
  (replaces `pulled_players.json`). Active queue per kit enforced by partial
  unique. Closed queues are preserved history.

### 4.11 `evaluations`  ← replaces `evals.json`

```sql
CREATE TABLE evaluations (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id  BIGINT NOT NULL REFERENCES players(id),
    kit_id     BIGINT NOT NULL REFERENCES kits(id),
    granted_by BIGINT,                        -- tester snowflake
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX uq_eval_active ON evaluations (player_id, kit_id) WHERE revoked_at IS NULL;
```

- **Lifecycle:** grant = insert; revoke = set `revoked_at` (history retained).

### 4.12 `testers`

```sql
CREATE TABLE testers (
    player_id  BIGINT PRIMARY KEY REFERENCES players(id),
    granted_by BIGINT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- Replaces `data/testers.json` ID list.

### 4.13 `outbox_events`  ← recoverable reconciliation (DB-failure wedge)

```sql
CREATE TABLE outbox_events (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type            TEXT NOT NULL,        -- 'promotion_commit'
    aggregate_type        TEXT NOT NULL,        -- 'result'|'player_tier'
    aggregate_id          TEXT NOT NULL,        -- result_key | player_id:kit_id
    payload               JSONB NOT NULL,       -- full data needed to replay
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|done|failed|dead_letter
    discord_role_confirmed BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE: Discord won, DB must catch up
    attempts              INT NOT NULL DEFAULT 0,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at          TIMESTAMPTZ
);
CREATE INDEX ix_outbox_pending ON outbox_events (status, created_at) WHERE status IN ('pending','in_progress');
```

- **Lifecycle:** created when a DB write fails *after* Discord confirmed a
  change; drained by the reconciler (verify actual Discord state → apply →
  `done`; honours `dead_letter` after N attempts). At-least-once processing
  (guarded by event id + status).
- **Fallback when PG is unreachable:** append to `data/local_outbox.json`
  (bounded, logged, drained when PG returns). This is the ONLY file-write
  exception and is an explicit wedge, not silent divergence.

### 4.14 `sync_runs`

```sql
CREATE TABLE sync_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command         TEXT NOT NULL,       -- 'sync_discord'|'sync_discord_rollback'|'sync_web'|'sync_check'|'sync_data'|'reconcile'
    mode            TEXT NOT NULL,       -- 'preview'|'apply'|'automatic'|'rollback'
    triggered_by    BIGINT,
    triggered_by_name TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',  -- running|success|failed|partial
    summary         JSONB NOT NULL DEFAULT '{}'
);
```

### 4.15 `sync_actions`

```sql
CREATE TABLE sync_actions (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sync_run_id      BIGINT NOT NULL REFERENCES sync_runs(id),
    action_type      TEXT NOT NULL,      -- 'observe_tier'|'reverse_add_role'|'reverse_remove_role'|...
    player_id        BIGINT REFERENCES players(id),
    kit_id           BIGINT REFERENCES kits(id),
    tier_id          BIGINT REFERENCES tier_definitions(id),
    discord_role_id  BIGINT,
    member_id        BIGINT,
    anomaly_category TEXT,               -- missing_tier|multiple_tier_roles|unknown_role|retired_role|invalid_combination|missing_configured_role|member_left
    status           TEXT NOT NULL,      -- 'applied'|'skipped'|'failed'|'anomaly'
    details          JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_sync_actions_run   ON sync_actions (sync_run_id);
CREATE INDEX ix_sync_actions_anom  ON sync_actions (anomaly_category);
```

- Replaces the JSON audit logs for sync. `anomaly_category` rows are the
  **report**, never automatic corrections.

### 4.16 `audit_logs`  ← consolidates edituser/playersync/websync/datacheck/checkweb/ticket logs

```sql
CREATE TABLE audit_logs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id    BIGINT,
    actor_name  TEXT,
    action      TEXT NOT NULL,      -- 'promotion_committed','promotion_discord_failed','ticket_claimed','sync_rollback',...
    entity_type TEXT,               -- 'player'|'ticket'|'result'|'sync'|'config'|...
    entity_id   TEXT,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_created  ON audit_logs (created_at);
CREATE INDEX ix_audit_entity   ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_actor    ON audit_logs (actor_id);
```

- Append-only. Each legacy `*_log.json` file maps to `action` taxonomy with the
  original payload preserved in `details` (for byte-level history migration).

### 4.17 `bot_config`  ← replaces misc runtime JSON (tournaments, queue_messages, panel meta)

```sql
CREATE TABLE bot_config (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.18 `migration_import_issues`

```sql
CREATE TABLE migration_import_issues (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category    TEXT NOT NULL,      -- 'unmatched'|'ambiguous_ign'|'duplicate_discord_id'|'conflict_modes_vs_discord'|'unresolved_fk'
    source_file TEXT NOT NULL,
    source_key  TEXT,               -- record identifier in source JSON
    reason      TEXT NOT NULL,
    payload     JSONB NOT NULL,     -- full original record (nothing dropped)
    status      TEXT NOT NULL DEFAULT 'open',   -- open|resolved|ignored
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- The guarantee: **nothing is ever silently discarded**; every unresolved
  record is preserved here until manually resolved.

---

## 5. CURRENT TIER MODEL

`player_current_tiers` is **NOT an independent authority**. Semantics:

> **"At `observed_at`, Discord confirmed this player holds `tier` for `kit`
> via `discord_role_id` (source: `sync_run_id` / `result_id`)."**

- A row exists only because Discord was observed (sync, promotion confirm, or
  explicit manual confirmation post-migration) — never because
  `players.json` said so (§11).
- The row is never propagated back to Discord by normal sync (§8).
- A player with **zero** tier roles has **no row** — that is "no current tier",
  not a corrected tier.
- If the currently configured role for a tier is deleted from Discord, the row
  keeps `discord_role_id` but reconciliation flags `missing_configured_role`;
  the tier stays as *last observed* until a new observation supersedes it.

---

## 6. TIER HISTORY

- `tier_history` is append-only, one tuple per **transition**:
  `(player, kit, old→new, changed_at, source, result_id?, sync_run_id?)`.
- **Transition detection:** compare incoming observation/promotion tier to the
  committed `player_current_tiers` row. Same tier ⇒ update `observed_at` on the
  current row only — **no history row** (repeated reconciliation does not
  fabricate history).
- Missing `previous_tier_id` = first recorded transition for that (player, kit).
- History is immutable input to presentation (web timeline), never to
  current-tier logic.

---

## 7. ROLE CONFIGURATION

**Move `kit_roles.json` → `kit_roles` table** (§4.4); `/setkitrole`,
`/unsetkitrole`, `/kitrole` become DB operations.

- **IDs only.** Role names are never used for matching (Discord names are
  presentation).
- **Startup validation (fail-fast, resolved decision §15):**
  1. `kit_roles` has **zero rows** → bot refuses to start sync/reconcile
     ("incomplete role mapping").
  2. Any `kit_roles.discord_role_id` **not found in guild roles** → startup
     error listing every missing role (config is broken; do not guess).
  3. Any **non-numeric** role id → startup error (invalid config).
  4. A kit with **no mapped roles** → warning (kit may be intentionally
     inactive) unless `STRICT_KIT_ROLES=1`, then error.
- **Detection after startup:** role deleted while running →
  `missing_configured_role` anomaly in sync; admin pinged; the mirror keeps
  last-observed state.
- Migration of `kit_roles.json`: dry-run report of mappings + conflicts
  (duplicate role → multiple kit/tier) before insert; conflicts become
  `migration_import_issues`.

---

## 8. DISCORD SYNC (new semantics)

`/sync discord` = **Discord → PostgreSQL only.** It may:

- read guild members + roles;
- classify per (member, kit) into `sync_actions` anomalies:
  - `missing_tier` — DB has a current tier but Discord shows no tier role
  - `multiple_tier_roles` — >1 tier role for the same kit
  - `unknown_role` — member role not in `kit_roles` config
  - `retired_role` — member holds a retired-tier mapping (reported, preserved)
  - `invalid_combination` — config conflict (role maps to 2+ kit/tier)
  - `missing_configured_role` — configured role id absent from guild
  - `member_left` — player in DB not found in guild (tier unobservable)
- **apply** = update `player_current_tiers` (+ `tier_history` on transition)
  + `audit_logs`. That's it.

It must **never** call `add_roles` / `remove_roles` / `edit(roles=...)`.
Idempotent: no changes ⇒ `No changes.` and no history rows.

---

## 9. ROLLBACK

- **Normal sync:** Discord → DB (mirror), non-destructive, automatic,
  anomaly-reporting.
- **`/sync discord-rollback`:** a **disaster-recovery inverse**, not a sync. It
  replays the historical rollback log (add → remove, remove → add) against
  **Discord** for ops applied by the legacy destructive `/sync discord apply`.
- Preview-first; explicit admin apply; idempotent states (`already_correct`)
  skip the Discord API; fully audited.
- After migration, normal sync is non-destructive, so this path only serves to
  unwind **pre-migration** destructive applies. It remains, frozen, as the
  safety net (explicit constraint: *keep `/sync discord-rollback`*).

---

## 10. PROMOTION FLOW (resolved `/result` sequence)

**State machine on `results.promotion_status`:**

```
discord_pending → committed ────────────────► announcement sent/failed
      │                                        (best effort)
      ├─► discord_failed      (Discord API error — NO DB tier change)
      └─► db_failed_outboxed  (Discord OK, DB tx failed → outbox wedge)
```

1. **Request** — tester submits `/result` (or `/topresult`).
2. **Validate** — ticket ownership/kit/status, ladder membership, eval gate,
   cooldown, dedup (`result_key` unique / fight window / ticket=1-result).
   Compute target tier (or bridge) from ladder.
3. **Discord first** — compute desired role set for (kit, new tier):
   `add target tier role + remove other tier roles of same kit` via a single
   `member.edit(roles=desired)` (one API call ⇒ no partial role state).
   Await and confirm success.
4. **Discord failure** ⇒ set `promotion_status='discord_failed'`, write audit,
   reply error to tester. **No commit of the new current tier.** No attempt to
   "fix" Discord.
5. **Discord success ⇒ one DB transaction:**
   - insert `results` (`promotion_status='committed'`, `new_tier_id`),
   - upsert `player_current_tiers` (observed_at=now, source='promotion',
     `discord_role_id`=granted role, `result_id`),
   - insert `tier_history` if tier actually changed,
   - apply cooldowns / close ticket per semantics (HT3 eval ticket close +
     HT3 cooldown; fight win: neither; fight loss: close + cooldown),
   - insert `audit_logs`; COMMIT.
6. **DB failure after Discord success** ⇒ do **not** revert Discord (explicit
   rule). Write `outbox_events` row `promotion_commit` with
   `discord_role_confirmed=true` (or append `data/local_outbox.json` if PG
   itself is unreachable). Reply: "role granted; state will reconcile".
7. **Reconciliation** — reconciler drains outbox: re-reads actual Discord
   roles, applies the mirror to match **what Discord shows now**, marks event
   `done` (or `dead_letter` after N attempts). Idempotent.
8. **Announcement** — embed to result channel; `announcement_status`
   pending→sent/failed; never blocks the commit.

Idempotency matrix:

| Duplicate input | Guard |
|---|---|
| same ticket re-submitted | `result_key` UNIQUE + ticket one-result rule |
| same fight re-submitted (free) | 2h dedup window (service) |
| outbox processed twice | event id + status CAS |
| Discord role already present | `edit(roles=desired)` idempotent (role set equal) |
| reconciliation re-observed same tier | no history row (§6) |

---

## 11. JSON MIGRATION

Replaces `migrate_json_to_postgres.py` (naive JSONB copy) with a staged,
report-first importer.

1. **Freeze + backup** `data/` → `data/backup-pre-migration-{ts}/`.
2. **Seed dimensions:** `kits` (kits.json), `tier_definitions` (ladder +
   virtual + tournament + retired), `bot_config` (misc), `kit_roles` (report
   conflicts → `migration_import_issues`).
3. **Identity resolution (critical):**
   - record has `discordId` → resolve to `players.discord_id`;
   - duplicate `discordId` across records → both to `migration_import_issues`
     (`duplicate_discord_id`, payload preserved) — never merged;
   - record without `discordId` → `players` row with `discord_id NULL`, keyed
     by `lower(ign)`;
   - duplicate IGN → `migration_import_issues` (`ambiguous_ign`) — no auto-merge.
4. **Must NOT happen:** seeding `player_current_tiers` from `players.json`
   `modes`. Current tiers are populated **only from Discord observation**
   (first sync / promotion). `modes` become `legacy_tier_snapshots`
   (staging table, dropped in Phase G) → reported as **current-tier conflicts**
   when Discord disagrees (Discord wins, conflict listed in the report).
5. **`history` arrays → `tier_history`** (source='migration', `changed_at` from
   `DD.MM.YYYY` parsed in Europe/Prague; missing date ⇒ record timestamp;
   `previous_tier_id` from array order).
6. **Operational JSON** (results, tickets, cooldowns, queues, evals, testers,
   logs) → target tables via the identity map; unresolved player references →
   `migration_import_issues` (`unresolved_fk`) with payload preserved.
7. **Report** (machine + human readable):
   - matched players (by discordId),
   - unmatched players (no id — remain claimable, listed),
   - ambiguous matches (dup IGN / dup discordId),
   - historical records migrated (counts per table),
   - current-tier conflicts (modes vs first Discord observation — resolved
     Discord-wins, each listed).
8. **Idempotent** — dry-run first, re-runnable, nothing deleted.

---

## 12. LEGACY CUTOVER — PHASES A–H

| Phase | Reads | Writes | Authoritative | Rollback |
|---|---|---|---|---|
| **A. PG infrastructure** | — | PG provisioned (Alembic env), DATABASE_URL, pool | — | none (bot untouched, still JSON) |
| **B. schema/migrations** | — | tables §4, seeds, new repository layer (unused) | — | drop schema branch (bot untouched) |
| **C. JSON migration** | data/*.json | PG load (§11), report | mirror build | truncate migrated tables; JSON intact |
| **D. Discord reconciliation** | Discord roles, kit_roles table | `player_current_tiers` from Discord ONLY; outbox live | **Discord** | stop reconciler; JSON serving restored |
| **E. repo/service migration** | PG | PG + **dual-write JSON (grace)** | **Discord → PG** | config flag flips back to JSON reads; watchdog checks divergence |
| **F. web migration** | PG | generated players.json artifact → GitHub; web page by discordId | PG mirror | exporter reverts to JSON snapshot |
| **G. remove JSON writes** | — | runtime JSON writes banned (assertion in storage layer) | PG | frozen JSON read-only baseline kept |
| **H. archive legacy JSON** | — | data/ → data/archive-{date}/; PG JSONB table dropped; final report | PG | restore from backup if catastrophic |

Notes: dual-write in E is **optional, time-boxed** (recommended for the
rollback window; risk = divergence, mitigated by integrity checks and the
watchdog). Phase D precedes E so current-tier reads can switch to the mirror
first without rewiring every service.

---

## 13. FAILURE MODES

| Scenario | Behavior |
|---|---|
| Discord unavailable | sync/reconcile: fail the run cleanly (`sync_runs.status='failed'`), retry with backoff; promotions: reject (do not guess); existing mirror untouched |
| PostgreSQL unavailable | fail clearly, log, **no JSON fallback**; promotions after Discord success → `local_outbox.json` wedge; reads fail with explicit error; bot reports degraded |
| role deleted on Discord | anomaly `missing_configured_role`; mirror keeps last-observed tier; admin ping |
| member left Discord | `member_left` anomaly; current tier stays as last-observed (flagged unobservable), not deleted |
| multiple tier roles | `multiple_tier_roles` anomaly; **no auto-correction**; mirror not updated from ambiguous state |
| unknown role | `unknown_role` anomaly; ignored for tier interpretation |
| DB transaction failure | promotion → outbox `promotion_commit` (or local wedge file); reconciler aligns to Discord; never reverts Discord |
| Discord API failure | promotion → `discord_failed`, no tier commit, audit entry |
| bot restart | startup: fail-fast config validation + reconciliation run; persistent views/panels rebound (existing mechanism) |
| missed Discord event | covered by periodic reconciliation (idempotent) |
| duplicate Discord event | reconciliation idempotent (no history spam, `observed_at` bump only) |
| duplicate promotion request | `result_key` UNIQUE / ticket one-result / fight dedup window |
| outbox replay | at-least-once, CAS on event id, dead-letter after N attempts |
| web outage | GitHub push best-effort; PG unaffected |
| GitHub push conflict (409) | existing retry/re-merge (`MAX_PUSH_ATTEMPTS=3`) retained, from PG export |

---

## 14. TEST PLAN

Existing 24 test files mapped to the new architecture (adapted, not
rewritten — they exercise the same pure logic via fakes):

| Existing file | Target |
|---|---|
| test_storage / test_store | repository layer integration (PG + JSON backends), transaction semantics |
| test_playersync / test_sync_rollback / test_command_sync | Discord→DB sync service + rollback service |
| test_role_sync / test_roles / test_kits | role config (kit_roles table) + startup validation |
| test_checkweb | sync classification (anomaly categories) |
| test_websync / test_github_sync | web export path (PG read model) |
| test_results / test_topresult | promotion flow (Discord-first mock, outbox wedge) |
| test_tickets / test_queue_service / test_tournaments | repo-level operational tables |
| test_edituser / test_player_identity | identity + repository ops |
| test_datacheck | integrity checks against PG |
| test_permissions / test_bot / test_ht_ticket_view | unchanged (discord-layer) |

**New tests required:**

- Discord→DB observation (role add on Discord ⇒ mirror update, no Discord write)
- idempotency (same observation twice ⇒ no history row; "No changes.")
- role anomalies (missing/multiple/unknown/retired/invalid → report-only)
- promotion transaction semantics (role first, DB second, single tx)
- DB failure after Discord success (outbox wedge + reconciler alignment; Discord never reverted)
- Discord failure (no tier commit)
- rollback (inverse ops idempotent, never triggered by reconcile)
- JSON migration (identity buckets: matched/unmatched/ambiguous/conflict; conflict ⇒ Discord wins)
- startup fail-fast (empty kit_roles / missing role id / non-numeric id)

All service-layer tests run against both JSON-fake and real PostgreSQL
(embedded `embedded-postgres` for CI — Unix-socket based, no TCP) via the
repository interface.

---

## 15. FINAL DECISION RECORD

### Architectural invariants (non-negotiable)

1. Discord is authoritative for current tier.
2. The DB never automatically changes Discord during normal sync.
3. Web never directly changes tier state.
4. players.json is not authoritative.
5. GitHub JSON is not authoritative.
6. IGN is mutable.
7. Discord ID is the stable identity.
8. Tier history is append-only.
9. Normal sync is non-destructive and reports anomalies.
10. Rollback is a separate disaster-recovery mechanism.
11. No silent fallback from PostgreSQL to JSON.
12. "DB says new tier, Discord still has old tier" is **never** a silently
    accepted final state (outbox + reconciliation guarantee).
13. Nothing is ever silently discarded (migration_import_issues).

### Resolved decisions (confirmed 2026-09-25)

1. **Web read path** — both: Web/API reads PG directly for pages by `discord_id`; keep generated players.json → GitHub artifact as legacy presentation.
2. **Startup validation strictness** — fail-fast: bot refuses sync/reconcile on empty `kit_roles`, any configured role ID missing from the guild, or non-numeric IDs; kits with no mappings = warning unless `STRICT_KIT_ROLES=1` (then error).
3. **Identity linking** — add explicit `/linkdiscord` (admin-verified claim by IGN); unmatched rows remain in `migration_import_issues` until claimed; no auto-matching by IGN.
4. **Reconciliation cadence** — startup + hourly sweep + `on_member_update` event capture (drains outbox in the same pass).
5. **Production PostgreSQL hosting** — managed/remote DB provided by the operator (`DATABASE_URL`); embedded `embedded-postgres` remains the test/CI route only.

### Assumed defaults (no objection → stand)

6. **Guild model** — single guild; schema is single-guild now, `guild_id` column deferred (add via Alembic if a second guild ever appears).
7. **Phase E dual-write** — time-boxed JSON+PG dual-write with watchdog divergence checks for the rollback window.
8. **Discord role grant atomicity** — promotion uses a single `member.edit(roles=desired)` for the kit.
9. **Timestamp conversion** — legacy ms-epoch / `DD.MM.YYYY` → `TIMESTAMPTZ` UTC; display renders Europe/Prague.
10. **GitHub push post-migration** — retained as presentation-only exporter (reads PG), `MAX_PUSH_ATTEMPTS=3` retry preserved.

---

## 16. PHASE A IMPLEMENTATION NOTES (2026-09-25)

Applied deviations / implementation facts:

1. **Test DB engine** — `embedded-postgres` 18.6.3 (`PostgresServer(pgdata,
   cleanup_mode='stop')`), NOT the IoT `pyembedded` lib. On Linux it binds a
   **Unix socket only** (`postgresql://postgres:@/{db}?host={socket_dir}`);
   binaries ship inside the wheel, no external download.
2. **Schema** — 20 tables + `alembic_version` (initial migration
   `63bdbcfda74a`); PKs are `BIGSERIAL`; cooldown uniqueness implemented as
   two **partial unique indexes** (`uq_cooldowns_waitlist`,
   `uq_cooldowns_kit`); CHECK constraints carry short base names and are
   rendered `ck_<table>_<name>` via the naming convention.
3. **Startup** — `bot._init_database()` validates role IDs (fail-fast) and,
   when `DATABASE_URL` is set, connectivity + schema revision vs Alembic head;
   without a URL the bot stays on the JSON backend unless `DB_REQUIRED=true`
   (then startup fails with a generic, secret-free message).
4. **Alembic** — `migrations/env.py` resolves `-x url` via
   `context.get_x_argument` (alembic 1.20 API), keeps `dachshundtiers` logging
   enabled (`disable_existing_loggers=False`); `alembic.ini` has
   `path_separator = os` and an intentionally empty `sqlalchemy.url`.
5. **Engine** — pool args are dropped when a non-`QueuePool` pool class (e.g.
    `NullPool`) is requested; `pool_pre_ping=True`, bounded `pool_recycle` for
    remote/managed PostgreSQL.
 6. **Secrets** — generic Czech error messages only; connection string never
    logged, printed, or required via chat. Operator supplies `DATABASE_URL`
    (or `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD`).

---

## 17. PHASE B IMPLEMENTATION NOTES (2026-09-25)

Repository/service layer delivered; production behavior unchanged (Phase B
does NOT wire the new layer into `cogs/` or `services/`). Applied deviations
from the plan above:

### 17.1 Repository layer (`db/repositories/`)

1. **Stateless classes, explicit session** — every repository method takes the
   caller's `AsyncSession`; the caller owns the transaction via
   `db.services.session.transaction()` (commit on success, rollback on error).
   Reactive/flush-based patterns kept (`session.flush()` after add for IDs).
2. **Identity model (`players.py`)** — `get_or_create_by_discord_id` and
   `claim_discord_id` treat an IGN that already belongs to a *claimed* record
   as a `PlayerIdentityError` (no silent adoption); explicit
   `claim_discord_id(..., claim_mode=...)` / `rename_ign` / `resolve_discord_id`
   / `resolve_ign` cover the four outcomes (`CLAIM_CREATED`, `CLAIM_RENAMED`,
   `CLAIM_ADOPTED`, `CLAIM_UNCHANGED`). IGN lookups are case-insensitive via
   `func.lower(Player.ign)` matching the `uq_players_ign` functional index.
3. **Mirror service is NOT the repository** — the read-only
   `MirrorRepository` (current tiers) and append-only `TierHistoryRepository`
   are complemented by a *service-level* `MirrorServiceRepository` that owns
   the combined "observe and derive" semantics (`apply_observation` returning
   `ObservationResult(tier_changed, first_observation, previous_tier_id,
   current_tier_id, history_id)`). `apply_observation` is transactional in
   the sense that a mirror upsert + history insert happen in one repo call
   when the caller provides a session with an open transaction. Observation
   sources carry CHECK-constraint names (`source` in `player_current_tiers`,
   `tier_history`); `MirrorService` (`db/services/tier_mirror.py`) routes any
   row whose `source` is not `discord_sync`/`promotion`/`manual` into a
   `PlayerCurrentTier` insert that will fail at the CHECK constraint
   (test `test_mirror_reject_unknown_source`).
4. **Upsert targets** — cooldowns use PostgreSQL partial unique indexes:
   `uq_cooldowns_waitlist (player_id) WHERE (cooldown_type='waitlist' AND
   kit_id IS NULL)` and `uq_cooldowns_kit (player_id, kit_id, cooldown_type)
   WHERE (kit_id IS NOT NULL)`. `on_conflict_do_update(index_elements=...,
   index_where=text(...))` + `returning(Cooldown)` +
   `execution_options(populate_existing=True)` so an in-session re-read sees
   the upserted values. `kit_roles` upsert targets the table constraint
   `uq_kit_roles_kit_tier` (NOT a partial index). A role may never map to two
   (kit, tier) pairs — `uq_kit_roles_role` unique ensures that.
5. **Outbox claim policy** — `claim_next` atomically claims the oldest event
   (`FOR UPDATE SKIP LOCKED`). `mark_failed` returns the event to `pending`
   until `attempts >= OUTBOX_MAX_ATTEMPTS (5)`, then `dead_letter`.
   `claim_next(in_progress_before=...)` supports crash recovery: a stale
   `in_progress` event older than the threshold is reclaimed. Implementation
   detail: the base filter switches to
   `(status='pending') OR (status='in_progress' AND created_at < threshold)`
   when the threshold is supplied — previously the pending-only preamble made
   the reclaim branch unreachable (fixed during Phase B test pass).
6. **Ticket close on promotion** — `TicketRepository.close_by_channel` is an
   addition over the plan (`open`/`close` plus the promotion service calling
   it inside the commit transaction).

### 17.2 Service layer (`db/services/`)

1. **`transaction()`** — context manager `async with session_factory() as
   session: async with session.begin(): yield session`; commit only on clean
   exit, rollback on exception.
2. **`tier_mirror.py`** — pure `classify_member_roles(role_snapshot,
   member_role_ids, kit_ids, kit_keys)` returns `ClassificationResult`
   (observations + anomalies + unknown_role_ids, fully deterministic sorted
   tuples). `MirrorService.apply_observations` applies only observations
   whose `anomaly` is `None`; anomalies are REPORTED, never applied. The
   mirror never reaches Discord (no `discord.py` import; contract tests import
   `db.*` in a subprocess and assert `discord` is not in `sys.modules`).
3. **`promotion.py`** — `PromotionCommitService.commit_after_discord_success`
   runs a single transaction: upsert `results`, mirror + history, cooldown
   upserts, ticket close, audit row, and marks `result.promotion_status =
   'committed'`. Idempotency: if the same `result_key` already has
   `promotion_status == 'committed'`, it returns `already_committed=True`
   without re-applying. The wedge (`enqueue_promotion_wedge`) writes an
   `outbox_events` row (`discord_role_confirmed=True`) on a SEPARATE session
   so it survives a failed main transaction (invariant 7); reconciling that
   event must never re-mutate Discord because the flag says the Discord side
   already happened.
4. **`config_validation.py`** — `validate_kit_role_configuration` FAILS FAST
   when (a) no mappings exist, (b) any mapped role is non-numeric, or (c) a
   DB-mapped Discord role is missing from the guild (`mapped_roles -
   guild_role_ids`). Extra guild roles are IGNORED (real guilds carry many
   roles the bot never maps). Kits with no role mapping produce a warning;
   with `strict_kits=True` that warning becomes an error. NOTE: the earlier
   draft had the direction reversed (guild role missing from DB) — corrected
   during Phase B tests; DB-mapped-missing-from-guild is the invariant-6/user
   intent ("If required role configuration is missing: FAIL FAST").
5. **DB failure is loud** — no JSON fallback; `OperationalError`/
   `ConnectionDoesNotExistError` propagates (contract test asserts a loud
   exception with no `discord`-free silent path; engine-to-nonexistent socket
   raises `OSError` on the client, still not a JSON fallback).

### 17.3 Testing / verification evidence (Phase B)

- New test modules: `test_repos_players.py`, `test_repos_kits_tiers.py`,
  `test_repos_ops.py`, `test_repos_outbox_audit.py`, `test_services_mirror.py`,
  `test_services_promotion.py`, `test_services_config_validation.py`,
  `test_no_discord_contract.py` (+ `tests/phase_b_db_verify.py`).
- Full suite: **646 passed** twice (584 Phase A + 62 Phase B); fixes applied
  during the pass: outbox stale-reclaim filter, cooldown partial-index arbiter
  columns, populate_existing on upsert reads, config-validation direction,
  sync-run FK player fixtures, mirror-history timestamp ties, DB-failure
  exception class, contract-token set ordering.
- `tests/phase_b_db_verify.py` (standalone): fresh DB → `upgrade head` (21
  tables, 38 unique indexes, 18 CHECK constraints, 13 FK-bearing tables) →
  `downgrade base` → `upgrade head` again — identical introspection both
  times.
- Diagnostics: only the known environment `reportMissingImports`
  false-positives (same set appears on Phase A files); `py_compile` clean;
  no silent JSON fallback anywhere in `db/` (audit grep).
---

## 18. PHASE C IMPLEMENTATION NOTES (2026-09-25)

### 18.1 JSON compatibility contract (item 9)

Phase C adds PostgreSQL as a *shadow mirror*; it does NOT change the JSON
contract of the running bot. Compatibility guarantees (enforced by tests):

1. **`players.json` shape is preserved** — `username`, `discordId`, `modes`,
   `history` (+ legacy fields) are read and written in exactly the Phase A
   format. `/result`, `/topresult`, `/edituser`, `/linkdiscord` all keep
   writing JSON first; PostgreSQL mirrors the confirmed outcome.
2. **`players.json` remains the business canonical store through Phase C**
   (invariants 4/5/14: players.json is NOT authoritative for *current tier* —
   Discord is — but it stays the bot's operational record until Phase D
   dual-write cutover).
3. **GitHub artifact = legacy presentation only** (design §15 d.1): the
   generated `players.json` → GitHub push is EXPORT-only. It never reads back
   into `players.json` or PostgreSQL, and web content never changes
   canonical data (verified by contract test:
   `test_websync.apply_never_writes_back_to_canonical_file`).
4. **websync remains the single GitHub writer** (`github_sync.push_players`;
   `MAX_PUSH_ATTEMPTS=3` = `websync.DEFAULT_ATTEMPTS`; retry on transient
   failure, 409-merge handled internally, no-token ⇒ no write).
5. **Identity extensions are JSON-first**: `/linkdiscord` claims are persisted
   to `players.json` (canonical) and mirrored to PostgreSQL best-effort
   (loud); the DB identity model never overwrites the JSON claim.
6. **Storage layer untouched**: `storage.py` (single writer, atomic write via
   transaction on `data/`) keeps operating as before; no Phase C code calls it
   from the PostgreSQL path and no PostgreSQL failure falls back to it
   silently.

### 18.2 GitHub exporter export-only verification (item 10)

- Direction audit: repositories of `github_sync.fetch_players` /
  `github_sync.push_players` are `services/websync.py` (read for analysis,
  write only the canonical payload) and `cogs/sync.py::_fetch_website`
  (read-only for `/checkweb` consistency report). No code path writes web
  data into `players.json`, PostgreSQL, or Discord.
- Retry contract preserved: `websync.sync_website` retries fetch and push
  with `DEFAULT_ATTEMPTS = 3`; covered by `test_sync_retries_fetch_until_success`
  and `test_sync_retries_push_on_transient_failure` (1 fetch + 2 pushes;
  no-token ⇒ no retry).
- Empty-canonical guard: `test_sync_empty_canonical_refuses_and_never_pushes`.
- EXPORT-only contract test added in Phase C:
  `test_websync.apply_never_writes_back_to_canonical_file` — a successful
  apply does not create or modify local `players.json`.

### 18.3 Phase C tests A–N map and verification evidence (items 13–15)

Test inventory (all files in `tests/`, engine = pytest, async via asyncio/pytest-asyncio):

| Scope | Where | Evidence |
|---|---|---|
| (A) atomická tier role | `tests/test_roles.py` (7) | single `member.edit(roles=target)`; idempotent skip |
| (B) wedge + outbox recovery | `tests/test_services_promotion.py` (15) | DB failure → wedge → consumer replay |
| (C) cog DB-block payload | `tests/test_results.py` (3), `tests/test_topresult.py` (4) | 5d/1a2 call `commit_promotion_with_wedge` only after `grant.ok`; exact kwargs (chytí rodinu `int('LT2')` – nyní `bridge_tier_code`) |
| (D) /linkdiscord | `tests/test_edituser.py` (6) | JSON-first + DB mirror + conflict/adopt/create |
| (E) startup validation | `tests/test_bot.py` (5), `tests/test_db_config.py` (1) | fail-fast, strict env, guild-retry |
| (F) bridge code resolution | `tests/test_services_promotion.py` (2) | code→id, unknown code never blocks commit |
| (G) JSON/export contract | `tests/test_websync.py` (34) | export-only; retry `DEFAULT_ATTEMPTS=3`; no write-back |
| (H–N) authority & safety | `test_sync.py` (51), `test_no_discord_contract.py`, `test_services_mirror.py`, `test_repos_*.py` | observe-only sync, no Discord mutation in db/, unconfirmed outbox refused |

Full suite: **695 passed** three times on the final tree
(673 Phase A/B/C carryover + 22 Phase C additions).
Ruff: **All checks passed** (`cogs/`, `db/services/`, `bot.py`, Phase C tests).
Alembic round-trip (`tests/phase_b_db_verify.py`): upgrade head → downgrade base →
re-upgrade head, identical introspection (21 tables / 38 unique idx / 18 CHECK /
13 FK tables), migration chain `63bdbcfda74a → a1b2c3d4e5f6` on embedded PG.

### 18.4 Skeptical authority audit (item 14)

- Role-mutation surface is confined to 3 touchpoints, all explicit business ops:
  `cogs/roles.py::auto_grant_kit_role` (atomic, idempotent, Discord-first),
  `/result` optional `add_role`/`remove_role` params, and admin-gated manual
  commands in `cogs/_shared.py`.
- `db/` and `services/` (outbox consumer, mirror, role_sync, playersync,
  websync) contain **zero** role mutations; `role_sync.py` computes + audits
  but never edits.
- Outbox consumer refuses events where `discord_role_confirmed is not True`.
- `/sync discord` (observe-only) never mutates roles; `_fetch_website` is a
  read-only consistency check.
- No silent JSON fallback in `db/`; PostgreSQL failures are loud (wedge/loud
  notes), never revert Discord.
