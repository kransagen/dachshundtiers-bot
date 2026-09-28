# Phase B/C – relational feature build + B1/B2 JSON-removal: final report

This report covers two passes, both uncommitted on top of `842ccc6`:

1. **B/C feature build** — kit→tester-room mapping, `/queue pull <kit>`,
   `/result` without role options, idempotent eval→HT3 ticket, HT3 panel with
   kit-only select, Minecraft identity one-to-one.
2. **B1/B2 – runtime JSON persistence removed** — the 53 dual JSON/PostgreSQL
   branches are gone; PostgreSQL is the only persistent app storage. JSON
   remains only for external exports/projections and legacy diagnostics.

---

## 1. What changed (B/C feature build)

### 1.1 Kit → tester room (`kit_tester_rooms`)

- `db/models/ops.py` — new `KitTesterRoom`: `kit_id` PK/FK → `kits`,
  `channel_id` UNIQUE, `created_by`, `created_at`, `updated_at`.
- `db/repositories/kits.py` — `KitTesterRoomRepository` (`set_room` upserts on
  `kit_id`; `get_for_kit`, `get_for_channel`, `clear`, `list_all`).
- `services/queue_service.py` — `set_tester_room`, `resolve_tester_room`,
  `clear_tester_room`, shared `resolve_kit` (business key, then case-insensitive
  display name).
- Migration `f7a8b9c0d1e2` creates the table. `channel_id` is UNIQUE so two kits
  can never claim the same room.

### 1.2 `/queue pull <kit>`

`pull_for_kit` resolves the room *before* touching the queue; `PULL_NO_ROOM`
leaves the entry `waiting`. `db/repositories/queues.py::claim_next_waiting` uses
`UPDATE … SELECT … FOR UPDATE SKIP LOCKED RETURNING *`; four concurrent pulls
hand out four distinct players (proven by test).

### 1.3 `/result` no longer takes role options

`[add_role]` / `[remove_role]` removed; role changes happen only through
`auto_grant_kit_role` + `commit_confirmed_promotion`. The
`AUTHORIZED_MUTATION_SITES` allowlist dropped both deleted sites (equality is
checked bidirectionally).

### 1.4 Eval → HT3 ticket, idempotent

`/seteval` opens the matching HT3 ticket via the **only** creation point
`views._open_ht3_ticket` (also used by the panel). Idempotency is the partial
unique index `uq_tickets_open_player_kit` (`status='open'`); on a duplicate the
just-created orphan channel is deleted.

### 1.5 HT3 panel: kit only, IGN and tier derived

`HT3Modal` deleted. The player picks only a kit; IGN comes from the **linked
Minecraft account** (never free-text `players.ign`), target tier from
`effective_ticket_tier` + `next_ticket_tier`, current tier from the
Discord-confirmed mirror only. Every refusal names its fix.

### 1.6 Minecraft identity, one-to-one

- `db/models/identity.py` — `MinecraftAccount` UUID is UNIQUE with a strict
  dashed-UUID CHECK; `PlayerLinkToken` UNIQUE code, partial live index, TTL and
  consumed-state CHECKs.
- `db/models/players.py` — `Player.minecraft_account_id` FK with UNIQUE
  constraint: one-to-one is enforced in the database.
- `db/repositories/identity.py` — race-safe `get_or_create`
  (`ON CONFLICT DO NOTHING` + re-SELECT), supersede on re-issue, conditional
  `consume` (`UPDATE … WHERE consumed_at IS NULL`).
- `services/minecraft_link.py` + `cogs/link.py` — `/link`, `/linked`, `/unlink`.
  9-char codes from an alphabet without `0/O/1/I`, 15-min TTL.
- **Ownership proof is a separate hop by design** (`complete_link` is meant for
  the Minecraft side); it is not wired — see section 6.

Three bugs caught while testing: rejection recorded-but-rolled-back on a bad
UUID, retry burning all attempts on *any* `IntegrityError` (now narrowed to the
unique violation), and a `None` dereference.

---

## 2. What was deliberately not changed

The relational core (players, kits, tier_definitions, kit_roles,
player_current_tiers, tier_history, results, tickets, ticket_members, queues,
queue_entries, queue_testers, evaluations, testers, outbox_events, sync_runs,
sync_actions, audit_logs, bot_config, migration_import_issues, tournaments,
tournament_entries, tester_credits) was already relational and compliant.
Cooldowns remain per `(player, kit, type)` with `('waitlist','ht3')` only.

No cooldown-semantics changes, no queue/HT3/Minecraft-linking redesign, no
cooldown-type additions. Discord stays authoritative for Discord-owned state.

---

## 3. B1/B2: runtime JSON persistence removed

Everything below was the content of the old "Remaining legacy paths" section;
it is now resolved.

### 3.1 The 53 dual branches — by file

| File | Branches | Status |
|---|---|---|
| `services/queue_service.py` | 21 | converted (0 remaining) |
| `services/tickets.py` | 14 | converted (0 remaining) |
| `services/tester_stats.py` | 4 | converted (0 remaining) |
| `services/topresult.py` | 4 | converted (0 remaining) |
| `services/results.py` | 4 | converted (0 remaining) |
| `services/edituser.py` | 3 | converted (0 remaining) |
| `cogs/sync.py` | 2 | converted (0 remaining) |
| `cogs/edituser.py` | 1 | converted (0 remaining) |

Grep guard: **zero** `if session_factory is not None:` remain in `cogs/`,
`services/`, `views.py`, `bot.py`, `utils.py` (the only such checks left are
`bot.py`'s startup guards, which gate optional view/panel re-registration — not
persistence). All `if session_factory is None:` sites are now **refusal**
checks, not fallbacks.

### 3.2 Refusal pattern

Converted public service functions require `session_factory` (no default) and
raise Czech `RuntimeError` naming the legacy file, e.g.:

- `player_tier potřebuje PostgreSQL; players.json se už nepoužívá`
- `get_ticket_logs potřebuje PostgreSQL; ht_ticket_logs.json se už nepoužívá`
- `credit_tester potřebuje PostgreSQL; testers_stats.json se už nepoužívá`

Views respond to a missing DB with a user-facing ephemeral message; nothing
falls back to a file. `test_phase_e_outage.py` proves DB-down runs never touch
`players.json` (wedge + outage flows).

### 3.3 Dead JSON-mode code deleted

- `utils.py` — `get_evals` / `has_eval` / `set_eval` / `unset_eval`
  (`evals.json`) and `get_kits` / `add_kit` / `remove_kit` (`kits.json`) were
  already dead in production; the module head now documents why. Remaining
  callers take the kit list from PostgreSQL-backed services.
- `services/_shared.py` helpers (`save_players` and friends) removed; the only
  `players.json` writer left in production is `services/player_export`.
- `services/store.py` survives **only** for legacy audit-log tooling
  (datacheck / checkweb / playersync / websync) — no converted service imports
  it (`test_g0_promotion_cutover.py` locks `services.store.transaction` out of
  the promotion path).

### 3.4 Test rewrites (per file, final counts)

Legacy JSON-mode test classes were deleted or rewritten as DB-mode / pure /
refusal tests. The DB-mode files share the `session_factory`/`clean_db`
fixtures (pytest-asyncio). Per-file totals:

| Test file | Passed | Notes |
|---|---|---|
| `tests/test_topresult.py` | 33 | pure + refuse + cog plumbing |
| `tests/test_results.py` | 18 | pure + DB-mode (`ResultDbMirrorTests` with sentinel factory) |
| `tests/test_services_results_db.py` | 17 | incl. concurrency dedup on `result_key` UNIQUE |
| `tests/test_phase_f_authority_regression.py` | 14 | AST/qualname authority guards |
| `tests/test_g0_promotion_cutover.py` | 42 | PG-only promotion, `services.store` locked out |
| `tests/test_kits.py` | 2 | pure only |
| `tests/test_edituser.py` | 52 | pure + cog plumbing, JSON classes deleted, `NoJsonModeTests` |
| `tests/test_edituser_db.py` | 34 | DB execute error-paths (web/role partial failure) |
| `tests/test_tickets.py` | 8 | pure `TicketShapeTests` + `TierHelperTests` + refusal |
| `tests/test_services_tickets_db.py` | 17 | DB coverage incl. `player_tier` from mirror |
| `tests/test_sync.py` | 52 | `SyncCheckTests`/`SyncWebTests` DB-mode |
| `tests/test_phase_e_github_export.py` | 5 | export source = PostgreSQL |
| `tests/test_phase_e_outage.py` | 9 | DB-down: no JSON fallback |

The old baseline before this session was **952 passed / 128 failed**; those 128
failed tests are now green (see section 5).

### 3.5 Remaining JSON references — inventory

Production (non-test, non-migration), by category.

**A. Approved exports / projections (PostgreSQL → JSON, never a fallback):**

| Location | What |
|---|---|
| `services/player_export.py` | THE canonical `players.json` writer; deterministic PG → export |
| `services/websync.py` (+ `github_sync.py`) | reads PG canonical, pushes `players.json` to GitHub (Contents API) |
| `services/phase_e/json_compat.py` | static analysis of JSON usage (pure, read-only) |

**B. Legacy tooling kept (explicit scope):**

| Location | What |
|---|---|
| `cogs/sync.py::_corrupt_data_files` | strict `load_data(..., strict=True)` probe over the 8 legacy state files |
| `services/datacheck.py` | read-only diagnostics over legacy files; `DATACHECK_LOG_FILE` audit |
| `services/checkweb.py` | Discord × players.json × web comparison; `CHECKWEB_LOG_FILE` |
| `services/playersync.py` | `PLAYERSYNC_LOG_FILE`, `PLAYERSYNC_ROLLBACK_LOG_FILE` |
| `services/websync.py` | `WEBSYNC_LOG_FILE` |
| `services/store.py` | local audit-log transaction layer, used **only** by the four above |

All of B is host-local audit/diagnostics; none feeds a promotion or stores
authoritative state. `db.models.tester_credits`, `db.repositories.cooldowns`,
`db.repositories.queues`, `db.repositories.tournaments` and the migrations only
*mention* the legacy files in docstrings.

**C. Config / startup (not app state):**

- `config.py` — `GITHUB_FILE_PATH` (export filename), `PHASE_E_JSON_EXPORT_ENABLED`
  (now inert; `dual_write` module proven caller-free by
  `test_phase_e_dual_write`).
- `db/validation.py::validate_configured_role_ids` — fail-fast read of host
  `data/kit_roles.json` (gitignored config) at startup.
- `storage.py` — `load_data`/`save_data` primitives (used only by B) and the
  PG `Jsonb` type.

**D. One-time migration tooling:** `migrate_json_to_postgres.py`,
`services/phase_d/*` (backup, import, inventory, snapshot, report, cli).

**E. Data / backups:** `data/*.json` (legacy inputs/docs) and
`backups/phase_d/*`.

**F. Intentional mentions:** refusal `RuntimeError` texts (asserted by tests)
and user-facing strings that now correctly describe the PG-only behavior —
`/result` messages say "uloženo do PostgreSQL", websync language says
"PostgreSQL → players.json export → GitHub", `/edituser` audit footers point to
PostgreSQL `audit_logs`.

> Stale JSON references found and fixed during the sweep: `cogs/results.py`
> user messages + comments, `cogs/edituser.py` audit footers,
> `cogs/topresult.py` `history_label` ternary + docstring/comments,
> `cogs/queues.py` comments, `cogs/ht3.py` docstring, `cogs/kits.py` /
> `cogs/roles.py` docstrings, `views.py` `HTTicketView` docstring,
> `services/datacheck.py` comment, `config.py` Phase E comment.

---

## 4. Migration and data risks

1. **Migration `f7a8b9c0d1e2`** adds three tables, one column, one FK, one
   UNIQUE on a populated table all starting empty / nullable-unique — no
   existing row is affected. `downgrade()` exists.
2. **`uq_players_minecraft_account_id`** cannot fail on existing data (no
   production path writes it yet); once `/link` is used the DB refuses a second
   link by design.
3. **Kit→room data does not exist yet**; `/queue pull <kit>` reports
   `PULL_NO_ROOM` until `/mktesterroom` mapping exists (legacy mapping had no
   attributable kit — deliberately not guessed).
4. **Model-vs-migration drift guard** — `test_db_migration.py` asserts zero
   `compare_metadata` drift.
5. **B1/B2 residuals (now fixed → watch forwards):**
   - `record_result` / `record_ht_fight` no longer have a JSON branch — the old
     "still writes players.json in a no-DATABASE_URL deployment" risk is gone;
     they refuse with `RuntimeError` instead.
   - `AUTHORIZED_MUTATION_SITES` is compared bidirectionally — deleting any
     Discord mutation site without editing the allowlist fails the authority
     scan test by design.
   - `result_key unique=True` is the DB-level same-ticket concurrency
     invariant (`IntegrityError` propagates; no catch in `record_result`).
6. **Still outside scope, documented:** legacy audit-log JSON files written by
   B-tooling (websync/checkweb/datacheck/playersync) remain; they are host-local
   diagnostics. Removing them is a follow-up, not a correctness gap.

---

## 5. Test results

Full suite, both orderings, `pytest-randomly` enabled and disabled:

```
1010 passed, 19 warnings in 273.80s   (pytest -p no:randomly -q)
1010 passed, 19 warnings in 268.03s   (pytest -q, randomized)
```

Baseline before this session: 952 passed / 128 failed. Zero failures now.

`ruff check` clean on all touched files: `bot.py`, `views.py`, `utils.py`,
`config.py`, `storage.py`, `cogs/{_shared,edituser,ht3,kits,queues,results,
roles,sync,topresult,tournaments,link}.py`, `services/{queue_service,tickets,
tester_stats,topresult,results,edituser,ht3_tickets,minecraft_link,datacheck,
phase_e/*,phase_d/*}.py`, `db/{models,repositories}/*`,
`tests/test_*` (rewritten set).

---

## 6. Remaining work, in order

1. **Wire the Minecraft proof hop.** `complete_link` is built and tested but
   nothing can call it from the Minecraft side (plugin/operator endpoint).
   Until then `/link` issues a code that only completes once that hop exists.
2. **Retire the legacy tooling JSON logs** (section 3.5.B) — move
   websync/checkweb/datacheck/playersync audit logs to PostgreSQL (or accept
   them as host-local by design). This also lets `services/store.py` and
   `storage.load_data/save_data` be deleted.
3. **Prune remaining docstring/comment mentions** of legacy file names once the
   B-tooling decision is made (they are intentionally kept while the tooling
   still references those files).
4. **Observe the ordering risk** flagged in B/C §4: the `Result` row + waitlist
   cooldown + ticket close + audit commit happen before the Discord mutation,
   surfaced only by `unresolved_promotions`.