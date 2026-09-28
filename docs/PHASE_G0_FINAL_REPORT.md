# Phase G0: Final Report

## STATUS
**Implemented.** All production promotion paths use a single canonical Discord-first, PostgreSQL-backed promotion flow. The hard invariants (mutate Discord → verify actual final state → commit PG only if confirmed; no PostgreSQL→Discord; never revert Discord; fail-closed) are enforced.

**Tests:** 1061 passed (all), 24 warnings; 3 consecutive full runs all green. Ruff clean on cogs/, db/, services/, tests/, config.py. Alembic upgrade→downgrade→upgrade round-trip successful.

## CURRENT→TARGET FLOW
The implementation now uses `db.services.commit_confirmed_promotion` as the **single canonical entry point** in both cogs. `cogs.roles.auto_grant_kit_role` returns `TierRoleGrant(ok, verified, ambiguous, tier_role_id, note)` and only a fully confirmed grant is committed to PG. Live verification (`member.edit()` PATCH result → fallback `guild.fetch_member()`) is performed on success and even on the "already has role" cache path and on ambiguous HTTP/timeouts.

## H1 ROOT CAUSE
H1 was structural: two gate sites (cogs/results.py and cogs/topresult.py) duplicated the decision to commit to the mirror. That duplication allowed the gate to drift and left verification as optional. The fix centralizes the gate inside `commit_confirmed_promotion()` (and its normalizer `grant_confirmation()`), which reads a duck-typed grant defensively, rejects any grant that is not `ok and verified and not ambiguous and has tier_role_id`, and imports no Discord/JSON layers. The static JSON-compat audit’s `json_first_write_pending_db_gate` classification was removed for the four actual promotion write sites.

## IMPLEMENTATION CHANGES
- `cogs/roles.py`: `TierRoleGrant.verified: bool = False`, `TierRoleGrant.ambiguous` semantics defined, `_role_ids()` returns `Optional[set[int]]` distinguishing “unreadable” (`None`) from empty, `_read_current_role_ids()` live re-read, `auto_grant_kit_role()` verifies actual final role set on no-op path and on timeout/HTTP ambiguity, refuses to treat unknown state as success, confirmed-mismatch path does not commit.
- `db/services/promotion.py`: `grant_confirmation()` rejects ambiguous outcomes (adds `and not ambiguous`), `commit_confirmed_promotion()` is the canonical service evaluating the grant internally (no cog import).
- `db/services/__init__.py`: exports `commit_confirmed_promotion`, `grant_confirmation`.
- `cogs/results.py`: inline gate removed; single `commit_confirmed_promotion(sf, grant=grant, ...)` call (no `discord_role_id` kwarg from cog).
- `cogs/topresult.py`: same canonical call; `session_factory=sf` passed to all three `set_ht_fight_announcement()` sites including `HTFightRetryView.__init__()`; failure reply uses mode-aware `history_label`.
- `services/phase_e/json_compat.py`: four promotion sites reclassified to `json_legacy_mode_only_pg_gated`; stale `json_first_write_pending_db_gate` name removed.
- `cogs/sync.py`: health embed helpers (`_health_embed`, `_db_health_embed`), `HEALTH_*` constants, `AREAS` gains `"db"`, health appended in `_run_check` for area `all` or `db`.
- `tests/conftest.py`: added `queue_testers` to `ALL_TABLES`, `clean_db` now `pg_terminate_backend` of other backends (prevents TRUNCATE deadlocks on leaked idle-in-transaction connections).
- `services/phase_e/dual_write.py`: documented as deferred/quarantined (zero production callers); guard test enforces no new callers.

## DISCORD AUTHORITY PROOF
Discord is the sole mutator. `commit_confirmed_promotion` refuses unverified/ambiguous/failed grants; wedge is created only when Discord already mutated and PG commit fails. No path performs PostgreSQL→Discord mutation. JSON paths are whole-deployment modes, not a fallback. Static authority scan shows only approved Discord mutation sites; `tests/test_no_discord_contract.py` and `tests/test_phase_f_authority_regression.py` pass.

## FAILURE MATRIX (Cases 1–8)
Covered by `tests/test_g0_promotion_cutover.py`: Case 1 (rejected mutation → nothing committed), Case 2 (no-op already correct → confirmed + committed), Case 3 (confirmed success → single transaction), Case 4 (PG commit fails → wedge durably), Case 5 (identity conflict after Discord → unresolved wedge), Case 6 (ambiguous timeout → nothing committed), Case 7 (ambiguous timeout but actually applied → committed), Case 8 (confirmed mismatch → not committed). Identity/conflict/unresolved paths wedge to outbox with `discord_role_confirmed=True`.

## OUTBOX/WEDGE
`commit_promotion_with_wedge` wedges on resolution/commit failures with appropriate messages (total outage vs recoverable via outbox). Reclaim of stale `in_progress` claims, dead-lettering, replay into PG, and refusal of unconfirmed outbox events are all tested. Discord is never reverted on any wedge.

## CONCURRENCY
Concurrent same-tier promotions deduplicate (mirror updated, history not duplicated); concurrent different-tier promotions keep history consistent; same `result_key` is idempotent. Advisory locks respected.

## HEALTH/OPERABILITY
`/sync check` appends a read-only PostgreSQL health embed (from `db.services.health`) for area `all` or `db`, showing unresolved promotions, outbox backlog/stale claims, dead-letter, recent DB failures etc., without affecting existing severity counts.

## LEGACY JSON
JSON is legacy/export only. Promotion current-tier determination uses no `players.json`. JSON compat scan reports zero `json_to_discord` and zero `json_to_pg` violations; promotion sites reclassified to `json_legacy_mode_only_pg_gated`. `find_player_tier` reads from players.json only where legitimately read-only/UI or in JSON-only paths (not for promotion decision). Web export remains downstream-only.

**Cooldown identity and the legacy `cooldowns.json` import.** The cooldown identity is `(player_id, kit_id, cooldown_type)`, enforced by `uq_cooldowns_kit`. The source `cooldowns.json` is kit-less (`{discord_id: expires_ms}` — the old bot had one global 4-day cooldown), so the importer originally wrote every row with `kit_id=NULL`, producing a global cooldown that blocked all kits. The kit is however *derivable*: the legacy writer set `cooldowns[playerId] = now` in the same transaction as the result carrying that `now`, its `kit` and its `timestamp`. The importer now indexes `ht_results.json` by `(playerId, timestamp)` and writes a per-kit row when — and only when — exactly one distinct kit matches. Every other outcome (`no_result`, `ambiguous`, `unknown_registry`) keeps the row global (never deleted, never rewritten) and records an open `MigrationImportIssue` with category `cooldown_kit_unattributable` / `cooldown_kit_unknown_in_registry`, surfaced in the `BUCKET_COOLDOWN` operator report. `ht3_cooldowns.json` is already per-kit and stays per-kit. Idempotency is preserved (second run adds no rows and no issues).

**No new global cooldowns.** All seven production cooldown writers pass a resolved `kit_id`, pinned by an AST guard (`tests/test_cooldown_scope.py::test_production_cooldown_writers_always_pass_a_kit_id`) that also fails if the writer set shrinks below seven, so the guard cannot silently go stale. The seventh writer is the canonical promotion service (`db/services/promotion.py`), where `CooldownSpec.kit_id` is optional and the outbox replay path feeds it from `raw.get("kit_id")`; a spec that omits the kit now falls back to the promotion's own kit instead of writing a kit-less row that would block every kit. A global (`kit_id IS NULL`) row can therefore only ever originate from the Phase D importer, and only ever already flagged with an open issue — asserted end-to-end by `test_legacy_global_row_is_always_paired_with_an_open_issue`.

**Check constraint naming.** `db/models/ops.py::Cooldown` declares the constraint as the bare suffix `type`; `db/base.py`'s convention (`"ck": "ck_%(table_name)s_%(constraint_name)s"`) expands it to `ck_cooldowns_type`, exactly what migration `63bdbcfda74a` created, so `alembic revision --autogenerate` sees no phantom diff. Spelling the full name out in the model would produce `ck_cooldowns_ck_cooldowns_type` and cause exactly the DROP+CREATE that was to be avoided. Both halves are pinned by `tests/test_cooldown_scope.py::test_check_constraint_name_matches_the_migration`.

## DEAD CODE/DUPLICATION
Gate duplication removed; `dual_write.py` quarantined with no production callers (guard test). Dead/unused paths documented as deferred where appropriate.

## TEST RESULTS
- `tests/test_g0_promotion_cutover.py`: 42 passed
- `tests/test_topresult.py` + `tests/test_results.py` (DB mirror/canonical): updated, passing
- `tests/test_roles.py`: 19 passed with realistic Discord doubles
- `tests/test_cooldown_scope.py`: 14 passed — the cooldown identity regression suite (see LEGACY JSON)
- Full suite: 1061 passed, 24 warnings across 3 runs (randomized and non-randomized consistent)

## REMAINING RISKS
1. `Result` row + waitlist cooldown + ticket close + audit are committed **before** Discord mutation in the JSON path (a failed grant leaves `promotion_status=discord_pending`, cooldown/ticket state as written) — surfaced by `unresolved_promotions` health check. This is a known ordering trade-off; the non-revert invariant remains absolute.
2. `record_result._run`/`record_ht_fight._run` still write `players.json` in the no-`DATABASE_URL` (JSON-only) deployment; those are legacy paths with no current-tier promotion authority.
3. `services/phase_e/dual_write.py` remains inert (zero production callers) by design and is quarantined.

## RECOMMENDATION
Proceed to commit (all checks pass). No H1 behavior remains: every promotion commit into PostgreSQL is gated by a Discord-confirmed, verified role grant; the canonical service is the only entry point; verification and wedge semantics are enforced.
