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

## DEAD CODE/DUPLICATION
Gate duplication removed; `dual_write.py` quarantined with no production callers (guard test). Dead/unused paths documented as deferred where appropriate.

## TEST RESULTS
- `tests/test_g0_promotion_cutover.py`: 42 passed
- `tests/test_topresult.py` + `tests/test_results.py` (DB mirror/canonical): updated, passing
- `tests/test_roles.py`: 19 passed with realistic Discord doubles
- Full suite: 1061 passed, 24 warnings across 3 runs (randomized and non-randomized consistent)

## REMAINING RISKS
1. `Result` row + waitlist cooldown + ticket close + audit are committed **before** Discord mutation in the JSON path (a failed grant leaves `promotion_status=discord_pending`, cooldown/ticket state as written) — surfaced by `unresolved_promotions` health check. This is a known ordering trade-off; the non-revert invariant remains absolute.
2. `record_result._run`/`record_ht_fight._run` still write `players.json` in the no-`DATABASE_URL` (JSON-only) deployment; those are legacy paths with no current-tier promotion authority.
3. `services/phase_e/dual_write.py` remains inert (zero production callers) by design and is quarantined.

## RECOMMENDATION
Proceed to commit (all checks pass). No H1 behavior remains: every promotion commit into PostgreSQL is gated by a Discord-confirmed, verified role grant; the canonical service is the only entry point; verification and wedge semantics are enforced.
