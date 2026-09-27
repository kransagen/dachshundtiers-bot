# Phase H: Final Report

## 1. STATUS

**COMPLETE — CONTROLLED CUTOVER ARCHITECTURE.**

JSON is no longer authoritative for current tiers in PostgreSQL mode, and all remaining JSON persistence is explicitly classified and isolated. `players.json` and `storage.py` are **not** eliminated — both remain for scenarios the architecture explicitly allows (legacy JSON-only deployment mode; non-tier operational persistence) — but every path that could let JSON or GitHub data override a Discord-derived current tier has been read, traced, and either confirmed already-safe or fixed and covered by a new regression test.

This is not the outcome "JSON completely eliminated." It is the outcome the spec's own decision criteria describe: importdiscord verified safe, the rollback conflict guard is now tested, the export fallback is now visibly labeled, JSON-only mode is explicit and cannot be entered accidentally, storage.py is correctly classified, the legacy JSONB table is classified, the authority scan passes, all tests pass 3x, Ruff is clean, Alembic is clean.

## 2. FINAL ARCHITECTURE

```
Discord
    ↓ (observation only)
CURRENT-TIER AUTHORITY  (mutated only by an authorized promotion
                          or explicit rollback command, never by
                          normal sync/reconciliation/export)

PostgreSQL (normalized schema, Alembic-managed)
    ↓
persistent application DB: mirror, history, outbox, audit, statistics

PostgreSQL → canonical export → GitHub/web
    (one direction only; web is a downstream copy, never a source)

players.json
    ↓
LEGACY JSON-ONLY DEPLOYMENT MODE (no PostgreSQL configured)
    or
EXPORT / COMPATIBILITY ARTIFACT (PostgreSQL mode: written by the
    canonical exporter, or as a loudly-labeled fallback when a
    PostgreSQL export attempt fails — never read back as a source
    of current tier)

storage.py (dachshundtiers_data JSONB key-value table)
    ↓
NON-TIER OPERATIONAL PERSISTENCE (cooldowns, tickets/queue state,
    testers stats, datacheck/edituser audit) — created by raw SQL
    at runtime, outside the Alembic-managed schema, and never
    consulted for current-tier decisions
```

## 3. JSON INVENTORY

| AREA | RUNTIME ROLE | DATA OWNED | CURRENT-TIER IMPACT | CLASSIFICATION | SAFE TO REMOVE? | REASON |
|---|---|---|---|---|---|---|
| `players.json` (PG mode) | export/compat artifact | canonical export snapshot | none — never read for tier decisions in PG mode | C (export) | No | Written only by `services/player_export.py`; read back only as the H6 fallback path, which is now loudly labeled |
| `players.json` (JSON-only mode) | legacy current-tier store | player tiers, for deployments with no `DATABASE_URL` | is the authority, but only in an explicitly-chosen deployment mode | A, but scoped to a mode that's intentionally out-of-scope for elimination | No | Removing it means removing the supported no-DB deployment mode, which the brief explicitly forbids |
| `storage.py` | non-tier operational persistence | cooldowns, tickets/queue state, testers stats, datacheck/edituser audit, and the JSON-only `players.json`/`kit_roles.json` file I/O | none | B, non-authoritative | No | Legitimate secondary persistence; see §5 |
| old JSONB table (`dachshundtiers_data`) | key-value store backing the above | same as storage.py | none | B, non-authoritative | No | See §6 |
| `migrate_json_to_postgres.py` | one-time migration CLI | reads `data/*.json`, writes via `storage.save_data` | none (never imported by `bot.py`/`cogs/`) | D | Not deleted (kept for disaster-recovery value); confirmed zero runtime callers |
| `cogs/_shared.py::save_players` | legacy JSON-only writer, PG-mode guard | `players.json` | none (raises in PG mode) | H, dead-but-guarded | Not deleted this pass | Zero production callers found, but it's the subject of its own regression test (`test_b2_save_players_refuses_in_postgres_mode`) proving the PG-mode refusal; quarantine-and-document rather than delete, per the "if uncertain" rule |
| `_canonical_for_export` | export-path fallback | in-memory only | none (export-only, never tier) | C, now correctly labeled | No | Fixed in this pass — see §7 |
| `/sync importdiscord` | preview-only decision tool | Discord × PG mirror × web, in-memory | **none** — writes nothing | D | No | Read in full this pass — see §4 |
| `/sync web` | export command | PG (or JSON fallback) → GitHub | none | C | No | Canonical export path; now flags fallback |
| `/sync discord` | observation command | Discord → PG mirror | none (write direction is Discord→PG only) | RUNTIME, correct | No | Confirmed observe-only by `test_c2_sync_cog_is_observe_only` |
| `/sync check` | health/observability | reads PG health | none | RUNTIME | No | See §13 |
| `/sync discord-rollback` | explicit authorized mutation | `playersync_log.json` audit → Discord | yes, by design, with safeguards | E | No | See §8 |
| `services/phase_d/*` (backup/import CLI) | standalone admin/DR tooling | JSON + DB markers | none (zero bot-runtime callers) | D/G | No | Preserved per explicit instruction |
| `backups/phase_d/*` | historical snapshots | JSON snapshots | none | G | No | Preserved per explicit instruction |
| remaining JSON readers/writers (`services/cooldowns.py`, `services/config_store.py`, `services/queue_service.py`, `services/kit_roles.py`, `services/tickets.py`) | non-tier operational config/state | cooldowns, config, queue, kit-role-ID mapping, tickets | none | B | No | Confirmed by import trace; none of these determine a player's current tier |

## 4. `/sync importdiscord`

Fully read `cogs/sync.py:2077+` (`sync_importdiscord`, `SyncImportDiscordConfirmView`) and its call chain into `services/checkweb.py` (`build_discord_import_decisions`, `apply_checkweb_decisions`).

**Source data:** it reads Discord (live roles via `_gather_checkweb`), the PostgreSQL mirror (via `_canonical_players`), and GitHub/web (read-only, for the diff display). It does **not** read `players.json` as a tier source in PG mode.

**What it writes:** nothing. `apply_checkweb_decisions` is a pure function — the docstring states plainly "NIC nezapisuje" (writes NOTHING) — it returns a computed `new_players` list that the confirm handler discards (`_, applied = apply_checkweb_decisions(...)`). The only side effect is an audit-log entry (`log_checkweb_event`) recording that the command ran in preview mode. It cannot modify PostgreSQL's current tier and cannot modify Discord.

**Verified directly, not assumed from the name:** this was already provable and is now also proven by an existing regression test, `tests/test_phase_f_authority_regression.py::test_d2_importdiscord_confirm_persists_nothing`, which seeds a Discord/PG mismatch, runs the confirm handler, and asserts the PG mirror tier is unchanged afterward. This test was already passing before Phase H and continues to pass.

**Conclusion:** `importdiscord` is intentionally NOT "Discord → PostgreSQL." It's a diagnostic preview that tells an operator what `/sync discord` (the real Discord→PG mirror command) and `/sync web` (export) would need to reconcile. No fix was required — the F-FIX work referenced in the code comments had already removed the write path in an earlier phase. This is documented explicitly so the distinction between "historical/import operation" and the write-capable canonical commands stays clear (H7).

## 5. ROLLBACK SAFETY TEST

Added `tests/test_rollback_conflict_guard.py` (7 new tests, all passing), covering the `_rollback_conflicts_with_later_promotion` guard in `cogs/_shared.py` directly against a real embedded PostgreSQL database:

- **Case A** — no newer promotion → `test_case_a_no_newer_promotion_allows_rollback`: guard returns `None`, rollback proceeds.
- **Case B** — a newer `source="promotion"` history entry for the same member+kit → `test_case_b_newer_promotion_same_member_kit_blocks_rollback`: guard returns a non-`None` reason.
- **Case C** — multiple later promotions → `test_case_c_multiple_later_promotions_still_blocks`: still blocked.
- **Case D** — an unrelated player has a later promotion → `test_case_d_unrelated_player_later_promotion_does_not_block`: target rollback is unaffected.
- **Case E** — the only later change is a non-`promotion`-sourced mirror refresh (e.g. `discord_sync`) → `test_case_e_later_non_promotion_change_does_not_block`: existing behavior preserved — the guard is specifically about `/result`/`/topresult` promotions, not general mirror drift.
- Two integration tests exercise `apply_rollback_actions` end-to-end with mocked Discord members: `skipped_newer_promotion` status is produced in the blocked case, `applied` status in the clear case.

The guard's logic itself was **not weakened** — no code in `cogs/_shared.py` was changed for this step, only test coverage was added.

## 6. EXPORT FALLBACK VISIBILITY

`cogs/sync.py::_canonical_for_export` previously fell back to `players.json` on a PostgreSQL export failure with only a `log.exception` — invisible to the Discord operator. Fixed:

- The function now returns `(players, source)` where `source` is one of `EXPORT_SOURCE_POSTGRES`, `EXPORT_SOURCE_JSON_ONLY_MODE`, or `EXPORT_SOURCE_JSON_FALLBACK` — distinguishing a genuine PG-failure fallback from the legitimate, explicitly-configured JSON-only deployment mode (which is not a failure and must not be mislabeled as one).
- `/sync web` (`_run_web`) and the apply confirmation (`SyncWebConfirmView`) both now show a `⚠️ LEGACY JSON FALLBACK` field in the result embed whenever `EXPORT_SOURCE_JSON_FALLBACK` occurs, stating plainly that the PostgreSQL export failed and the legacy file was used instead. `log.exception` is unchanged/still fires.
- `EXPORT_SOURCE_JSON_ONLY_MODE` intentionally does **not** trigger this warning — it's normal operation for that deployment mode, not a degraded state.
- New test: `tests/test_sync.py::test_pg_export_failure_is_labeled_as_legacy_json_fallback` — configures a PG-mode bot, forces `export_players` to raise, and asserts the resulting embed contains the `LEGACY JSON FALLBACK` field.
- No fallback was introduced anywhere for current-tier operations; this change only affects the export display.

## 7. AUTHORITY TEST FOR IMPORT TOOLS

`test_d2_importdiscord_confirm_persists_nothing` already proves the required invariant precisely: it seeds a `DATABASE_MISMATCH` (Discord says one tier, PG mirror says another), runs the importdiscord confirm handler — which would compute a "use_discord" decision — and asserts the PG mirror's actual tier is **unchanged** afterward. This directly proves a PostgreSQL current-tier value observed from Discord cannot be silently replaced by JSON/GitHub input via this command. No new test was needed; this existing coverage was verified to still pass and to genuinely exercise the mismatch scenario (not just the equal-state case).

`services/phase_d/import_data.py` (the Phase D one-time import CLI) remains outside the AST authority scanner's direct coverage — it's a standalone script with zero bot-runtime callers, so it cannot participate in any current-tier decision at runtime; its safety rests on that isolation plus its own functional tests, and this distinction is now documented explicitly (§14, Remaining Risks).

## 8. JSON-ONLY DEPLOYMENT MODE BOUNDARY

Verified in `bot.py::_init_database` (lines ~474–514): there are exactly three outcomes, no ambiguous fourth path:

1. `DATABASE_URL` unset, `DB_REQUIRED=true` → hard `SystemExit`.
2. `DATABASE_URL` unset, `DB_REQUIRED` not set → **legacy JSON-only mode**, logged plainly ("PostgreSQL není nakonfigurováno — backend zůstává JSON"). This is a deliberately supported deployment mode, not a fallback-after-failure.
3. `DATABASE_URL` set but connectivity/schema validation fails at startup → hard `SystemExit` ("PostgreSQL selhalo při startovní kontrole"), engine disposed. No JSON fallback.

There is no code path where PostgreSQL is configured, fails, and the bot silently continues in JSON mode — that would require *removing* the startup `SystemExit`, which nothing in this repository does. This is additionally covered by `tests/test_no_discord_contract.py::test_db_failure_raises_no_silent_json_fallback`, proving a mid-transaction DB failure raises rather than degrading silently. `DB_REQUIRED` defaulting to `false` means JSON-only is an operator's explicit choice (absence of `DATABASE_URL`), never an automatic transition triggered by a failure.

## 9. `storage.py` CLASSIFICATION

`storage.py` is **not a source of truth for current player tiers in PostgreSQL mode.** Its responsibilities split cleanly into:

- **A. Tier authority:** none. `db/services/promotion.py` and `cogs/roles.py` (the promotion/grant path) do not import `storage.py` at all.
- **B. Non-tier operational persistence:** the generic `dachshundtiers_data` JSONB table (see §10) backing `services/cooldowns.py`, `services/config_store.py`, `services/queue_service.py`, `services/kit_roles.py` (kit→tier→Discord-role-ID mapping, not a player's tier), and `services/tickets.py`, plus datacheck/edituser audit trails.
- **C. Legacy compatibility:** `load_data`/`save_data` for `players.json` in JSON-only deployments (§8), and `cogs/results.py`/`cogs/topresult.py`'s JSON branch, reachable only when `session_factory is None`.

All top-level functions in `storage.py` (`data_exists`, `postgres_load`, `postgres_save`, `postgres_lock_keys`, `ensure_data_dir`, `data_path`, `backend_name`, `database_status`, plus the connection/schema helpers) were checked for callers outside `storage.py` and tests — every one has a live production caller. **No dead code was found inside `storage.py` in this pass**, so nothing was removed from it, per the "don't rewrite for cosmetic reasons" instruction.

## 10. LEGACY POSTGRESQL JSONB TABLE

`storage.py:_POSTGRES_TABLE = "dachshundtiers_data"` — a generic `key TEXT PRIMARY KEY, value JSONB, updated_at` table created by raw SQL (`CREATE TABLE IF NOT EXISTS`, `storage.py` line ~167) **at runtime, entirely outside Alembic**. Confirmed: `grep -rl "dachshundtiers_data" migrations/versions/` returns nothing — this table has never been, and is not, managed by any Alembic migration, so there is no "obsolete migration" to remove for it and no upgrade/downgrade path to reconcile.

**Still used** — confirmed live callers: `services/cooldowns.py`, `services/config_store.py`, `services/queue_service.py`, `services/kit_roles.py`, `services/tickets.py`, plus datacheck/edituser audit and the JSON-only `players.json` I/O. It stores non-tier operational state only. **KEPT**, documented here as: does not own current tier, is not part of the normalized Alembic schema, and has zero relationship to the promotion/authority path.

Separately checked the Alembic-managed schema's own JSONB *columns* (`audit_logs.details`, `bot_config.value`, `migration_import_issues.payload`, `outbox_events.payload`, `sync_runs.summary`) — these are normal structured-metadata columns on proper normalized tables, not a generic key-value legacy mechanism, and are unrelated to the `dachshundtiers_data` question. No schema cleanup migration is proposed.

## 11. FINAL AUTHORITY AUDIT

Re-ran the authority scan after all changes (`tests/test_no_discord_contract.py`, `tests/test_phase_f_authority_regression.py`, `tests/test_phase_e_authority_scan.py`, `tests/test_phase_e_json_compat.py` — 40 tests, all passed):

| # | Invariant | Result | Evidence |
|---|---|---|---|
| 1 | JSON cannot determine current tier in PostgreSQL mode | PASS | `test_a_json_tier_never_wins_over_pg_mirror`, `test_players_json_is_the_only_current_tier_artifact` |
| 2 | GitHub cannot determine current tier | PASS | `test_pg_current_tier_only_in_db`, `test_github_export_only` |
| 3 | PostgreSQL current tier cannot mutate Discord | PASS | `test_db_package_never_imports_discord_import_graph`, `test_db_sources_contain_no_discord_mutation_tokens` |
| 4 | Normal reconciliation cannot mutate Discord | PASS | `test_c_views_have_no_role_mutation` |
| 5 | `/sync discord` cannot mutate Discord | PASS | `test_c2_sync_cog_is_observe_only` |
| 6 | Web export cannot mutate current tier | PASS | `test_d_checkweb_apply_persists_nothing`, `test_d2_importdiscord_confirm_persists_nothing` |
| 7 | Import/migration tools cannot silently override Discord-derived current tier | PASS | `test_d2_importdiscord_confirm_persists_nothing`, `test_d3_no_persistence_left_in_views`; `services/phase_d/import_data.py` proven safe by isolation (zero runtime callers) rather than the AST scanner directly — documented, not hidden |
| 8 | Only explicitly authorized promotion/rollback surfaces can mutate Discord | PASS | `test_c3_mutation_surface_still_exactly_authorized` (exact-match against a fixed, AST/qualname-keyed `AUTHORIZED_MUTATION_SITES` set) |

Confirmed: the scanner keys are `(module, qualname, call)`, never line numbers — explicitly tested by `test_mutation_surface_survives_unrelated_line_number_drift` and `test_json_compat_survives_unrelated_line_number_drift`. No invariant failed; nothing was patched around a failing test.

## 12. DATABASE SCHEMA

No schema changes were made or proposed this pass. §10 establishes that the one candidate for "legacy JSONB backend" (`dachshundtiers_data`) is outside Alembic's scope entirely (not created by any migration), so it cannot be the subject of an Alembic drop migration — there is nothing in the 7 existing migration files to remove. The Alembic-managed schema's JSONB *columns* are legitimate structured-data columns on live, normalized tables. No destructive migration was written.

## 13. PRODUCTION CONFIGURATION

Confirmed at `bot.py::_init_database` (§8): PostgreSQL is a required dependency whenever `DATABASE_URL` is set or `DB_REQUIRED=true`; a configured-but-unreachable database is a hard startup failure, never a silent JSON fallback. `DB_REQUIRED` defaults to `false`, making JSON-only a valid, explicit, non-hybrid deployment choice. No secrets were printed or logged during this investigation.

## 14. BACKUP / RESTORE

Not modified this pass. `services/phase_d/*` (backup/import CLI) and `backups/phase_d/*` (historical snapshots) confirmed to have zero bot-runtime callers — they are standalone disaster-recovery tooling, run manually, and were left untouched per explicit instruction. `tests/test_phase_d_backup.py` and `tests/test_phase_e_backup_restore.py` are part of the full suite and passed in all three consecutive runs.

## 15. OBSERVABILITY

`/sync check` (unchanged this pass) exposes: `observation_freshness` (Discord↔mirror staleness), `last_mirror_sync`, `unresolved_identities`, `sync_anomalies`, `outbox_backlog` (includes dead-letter count), `outbox_stale_claims`, `unresolved_promotions`, `recent_db_failures` — via `db/services/health.py` surfaced through `cogs/sync.py::_run_check` / `_db_health_embed`. No changes were needed here; it already covers the operability signals Phase H would otherwise have asked for.

## 16. DEAD CODE

- **Removed:** nothing. No function in `storage.py` had zero callers (§9).
- **Quarantined/documented (not removed):** `cogs/_shared.py::save_players` — zero production callers found, but it is the explicit subject of `test_b2_save_players_refuses_in_postgres_mode` (a regression guard against a future accidental PG-mode write) and is referenced in `services/phase_e/json_compat.py`'s classification map. Per the "if uncertain, quarantine + document rather than delete" rule, it is documented here as dead-but-guarded and left in place; a future pass could remove it together with its guard test if that guard is judged no longer necessary — that is a separate decision, not made in this pass.
- **Retained as one-time tooling:** `migrate_json_to_postgres.py` — confirmed zero runtime callers, kept for disaster-recovery value.

## 17. DOCUMENTATION

This report (`docs/PHASE_H_FINAL_REPORT.md`) is the documentation deliverable for this phase. No other doc files were rewritten — `docs/PHASE_G0_FINAL_REPORT.md` remains as the historical record of that phase and is referenced, not altered.

## 18. FILES CHANGED

**Production:**
- `cogs/sync.py` — `_canonical_for_export` now returns `(players, source)` with three explicit labels; `SyncWebConfirmView`/`_run_web`/`_websync_embed` surface a loud `⚠️ LEGACY JSON FALLBACK` warning when the PG export fails and JSON is used instead; JSON-only mode is not mislabeled as a fallback.

**Tests:**
- `tests/test_rollback_conflict_guard.py` (new) — 7 tests for the `_rollback_conflicts_with_later_promotion` guard (Cases A–E + 2 integration tests).
- `tests/test_sync.py` — 1 new test proving the export-fallback visibility fix.

**Migrations:** none.

**Docs:** `docs/PHASE_H_FINAL_REPORT.md` (this file, new).

*(Note: the working tree also carries pre-existing, uncommitted Phase G0 finalization changes — `db/services/promotion.py`, `services/phase_e/dual_write.py`, and several test files — that predate this Phase H session and were left untouched.)*

## 19. TEST RESULTS

- Suite #1: **1069 passed**, 24 warnings, 264.02s
- Suite #2: **1069 passed**, 24 warnings, 258.43s
- Suite #3: **1069 passed**, 24 warnings, 274.72s
- Ruff (`cogs/`, `db/`, `services/`, `tests/`, `config.py`): **clean**
- Alembic upgrade→downgrade round-trip: covered by `tests/test_db_migration.py`, passed in all three runs
- Authority regression scan (`test_no_discord_contract.py`, `test_phase_f_authority_regression.py`, `test_phase_e_authority_scan.py`): **all pass**
- JSON compatibility scan (`test_phase_e_json_compat.py`): **all pass**
- Storage dependency scan: manual grep-based trace (§9), all callers accounted for
- importdiscord regression: `test_d2_importdiscord_confirm_persists_nothing` — pass
- Rollback conflict tests: 7/7 new tests — pass
- Export fallback visibility test: 1/1 new test — pass
- Backup/restore tests (`test_phase_d_backup.py`, `test_phase_e_backup_restore.py`): part of full suite, pass

No flakes were observed across three runs; nothing needed to be rerun or investigated for non-determinism.

## 20. REMAINING RISKS

1. `services/phase_d/import_data.py` is safe by isolation (zero runtime callers), not by direct AST-scanner coverage — a future refactor that wires it into the bot's runtime would need a dedicated authority test at that time, not before.
2. `cogs/_shared.py::save_players` is dead-in-production-but-guarded; it's a small, low-risk piece of technical debt, documented rather than removed this pass.
3. The JSON-only deployment mode remains a fully separate authority regime by design; anyone standing up a fresh JSON-only deployment should know that `/sync discord-rollback` and the authority scan's PG-mode invariants don't apply to it — this is inherent to the two-mode architecture, not a new risk introduced here.

## PRODUCTION READINESS

**Proven:** JSON cannot reach current-tier state in PostgreSQL mode (8/8 authority invariants pass); the export path is loud about its one legitimate fallback; the rollback guard against reintroducing stale roles after a newer promotion is now under direct test; `/sync importdiscord` cannot silently override PG's Discord-derived tier; `storage.py` and the legacy JSONB table are non-authoritative and correctly classified; startup has no silent DB-down→JSON path; the full suite passes 3x consecutively with Ruff and Alembic clean.

**Not proven (out of scope for this pass, by explicit instruction):** whether the JSON-only deployment mode should eventually be deprecated; whether cooldowns/tickets/queue state should eventually move off the `dachshundtiers_data` JSONB table onto the normalized schema; whether `save_players` and its guard test should eventually be deleted together.

## NEXT STEP

The repository is ready for deployment under the controlled cutover architecture described above: Discord is the sole current-tier authority, PostgreSQL is the persistent application database in PostgreSQL mode, and all remaining JSON persistence is explicitly classified, isolated, and (where it could otherwise look like a silent authority path) now loudly labeled. No further Phase H work is required to reach that state. Migrating storage.py's non-tier data off the JSONB table, or deprecating JSON-only mode entirely, are separate future projects, not blockers to this cutover.
