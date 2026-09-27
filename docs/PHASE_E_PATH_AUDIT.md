# Phase E — Production Path Audit (E2)

Stabilization of the post-cutover system. Every production path is traced as
**SOURCE → TRANSFORMATION → DESTINATION**, and the critical question is
answered for each: *can it mutate Discord roles?*

Authority model (design §16, unchanged):

> **Discord = sole authority for current player tiers.**
> Normal paths: Discord → PostgreSQL. Never: PostgreSQL → Discord,
> players.json → Discord, GitHub → Discord.
> Explicit authorized promotion is the only normal business operation that
> intentionally changes Discord.
> If Discord succeeds and PostgreSQL fails: Discord remains authoritative →
> wedge/outbox → PostgreSQL catches up. NEVER revert Discord because
> PostgreSQL failed.

Legend for Discord-mutation column:

- **NO** — observes/reads or writes only PostgreSQL/JSON/GitHub.
- **YES (authorized)** — changes Discord and it is an explicitly authorized
  business operation (promotion, admin edit, audit-exact rollback).
- **YES (helper)** — a code-level engine that changes Discord; it is invoked
  ONLY from authorized flows, never from reconciliation/observation.

---

## 1. `/sync discord` — observe-only mirror into PostgreSQL

| | |
|---|---|
| Location | `cogs/sync.py:1651` (`sync_discord`), `_run_discord` `:1654` |
| Service | `db/services/mirror_sync.py` `DiscordSyncService.sync_guild` |
| SOURCE | Discord guild members (`member.id`, `member.role_ids`) |
| TRANSFORM | `classify_member_roles` (pure, `db/services/tier_mirror.py:41`) → per-kit (kit → tier) observations; anomalies `multiple_tier_roles` / `missing_tier` / `unknown_roles` are recorded, never guessed |
| DESTINATION | PostgreSQL only: `player_current_tiers`, `tier_history`, `sync_runs`, `sync_actions`, `audit_logs` |
| Discord MUTATION | **NO** — observe-only. Reads only `member.id` / `member.role_ids`; no `add_roles` / `remove_roles` / `member.edit` anywhere in `mirror_sync.py` or `tier_mirror.py`. `OBSERVE_SOURCE="discord_sync"` (mirror_sync.py). |
| Requires PG | Yes (`OBSERVE_REASON="/sync discord observe — mirror only"`) — no JSON fallback |
| Verdict | ✅ Matches the required classification (Discord → PG mutations **NO**). |

## 2. `/sync discord-rollback` — audit-exact inversion of applied sync actions

| | |
|---|---|
| Location | `cogs/sync.py:1782`, `_run_discord_rollback` `:1805`, `apply_rollback_actions` `:881` → `cogs/_shared.py:106` |
| SOURCE | PostgreSQL `sync_actions` audit (historical **applied** sync actions only) |
| TRANSFORM | invert each historical op (add ↔ remove), member identity strictly from audit `member_id`/`role_id`; idempotent per-action (`already_correct`) |
| DESTINATION | **Discord roles** (`member.add_roles` / `member.remove_roles`, `_shared.py:161,163`) — only for actions that were historically applied |
| Discord MUTATION | **YES (authorized)** — the only normal-sync mutation. Inverts EXACTLY the historical applied sync actions recorded in PG audit; never invents actions, never reads players.json as a plan source. |
| Verdict | ✅ Consistent with authority model: it is a reversal of a previously authorized Discord state change, driven by the audit log, preview + button confirm required. |

## 3. `/sync importdiscord` — Discord → players.json → web (legacy, no PG mirror)

| | |
|---|---|
| Location | `cogs/sync.py:1929`, confirm view `:1052` → `confirm` `:1066` |
| SOURCE | Discord roles (observed fresh via `_gather_checkweb(guild)`) + current `players.json` |
| TRANSFORM | `build_discord_import_decisions` + `apply_checkweb_decisions` (only unambiguous, per-record decisions; ambiguous → `skipped`) |
| DESTINATION | `players.json` (`save_players`) → web/GitHub (`sync_website`) |
| Discord MUTATION | **NO** |
| PG involvement | **NONE** — this path writes JSON + web but never PostgreSQL. |
| Flags (Phase E) | ⚠️ Legacy operational record (writer class in D6). Direction Discord → JSON → web is downstream (Discord stays authority), but the PG mirror is **not** updated for these players, so PG mirrors and players.json can diverge for them. Per E2 spec this path must NOT reintroduce the old JSON-authority path — it does not (it never feeds JSON back into PG current tiers or Discord). Actionable items: deprecated alias `checkweb apply`; only explicit decisions; never fuzzy. |
| Verdict | ✅ No Discord mutation, direction downstream. ⚠️ Note for E9/E13 report: PG mirror divergence risk; importdiscord should be retired in favor of `/sync discord` + `/edituser`. |

## 4. `/sync web` — canonical → GitHub (downstream export)

| | |
|---|---|
| Location | `cogs/sync.py:1986` (`sync_web`), `_run_web` `:1999`, confirm view `:840` |
| SOURCE | `load_data("players.json")` (`sync.py:2006`) — the legacy JSON **export artifact**, treated as canonical web payload |
| TRANSFORM | `preview_website` / `sync_website` (`services/websync.py`), GitHub Contents API via `github_sync.push_players` (per-loop lock, `MAX_PUSH_ATTEMPTS=3`, 409 → re-download + re-merge + retry) |
| DESTINATION | GitHub `players.json` only |
| Discord MUTATION | **NO** |
| Guards | empty canonical never pushed; without `GITHUB_TOKEN` nothing changes; GitHub failure never reported as success; GitHub failure never changes Discord |
| Flags (Phase E) | ⚠️ E1/E8 item: the web export source is currently the **JSON artifact**, not the PostgreSQL mirror. Both are downstream of Discord (export-only), so authority is intact, but Phase E goal "PostgreSQL is operational persistence" argues the canonical web source should eventually be read from PG state. Documented, not changed in Phase E (no unrelated refactor). |
| Verdict | ✅ Export-only, no Discord mutation. |

## 5. `/sync check` — read-only diagnostics

| | |
|---|---|
| Location | `cogs/sync.py:1521` (`sync_check`), `_run_check` `:1533` |
| SOURCE | Discord + PostgreSQL + data files — read-only gathers |
| DESTINATION | Report embeds only |
| Discord MUTATION | **NO** |
| Verdict | ✅ |

## 6. `/sync data` — deep integrity check + button-confirmed JSON repairs

| | |
|---|---|
| Location | `cogs/sync.py:2032` (`sync_data`), `_run_data` `:2038`, repair view `:1184` → `perform_repairs` |
| SOURCE | JSON data files (`run_datacheck`, `services/datacheck.py`) |
| TRANSFORM | lossless `normalize_tier` + close orphan ticket; corrupted files → `DataCorruptionError`, never auto-overwritten |
| DESTINATION | JSON data files only (self-repair) |
| Discord MUTATION | **NO** — and never PG tier correction |
| Verdict | ✅ DB diagnostic/repair only, never Discord tier correction. |

## 7. `/result` — authorized promotion (Discord-first) + PG mirror + wedge

| | |
|---|---|
| Location | `cogs/results.py` (flow `:380-502`) |
| SOURCE | authorized evaluator/admin command (tester/admin gates) |
| TRANSFORM | 1) `record_result` → `players.json`; 2) optional `add_roles`/`remove_roles` (`:436-438`); 3) `auto_grant_kit_role` (`:450`); 4) **PG commit AFTER Discord success** `commit_promotion_with_wedge` (`:472`); 5) web best-effort `sync_website` |
| DESTINATION | Discord → PostgreSQL (`commit_after_discord_success`: result + mirror + cooldowns + ticket close + audit, single tx) → players.json → web |
| Discord MUTATION | **YES (authorized)** — the designated promotion operation. Order: Discord FIRST, PG second (invariant 6). |
| Failure semantics | PG tx fails AFTER Discord change → outbox wedge (`promotion_commit`, `discord_role_confirmed=True`, fresh session) → replay brings PG into alignment with Discord. Never reverts Discord. |
| Verdict | ✅ Promotion Discord-first, wedge recovery, no JSON fallback, never reverts Discord. |

## 8. `/topresult` — authorized HT-fight promotion

| | |
|---|---|
| Location | `cogs/topresult.py:392-399` (same `commit_promotion_with_wedge` pattern) |
| Discord MUTATION | **YES (authorized)** — identical semantics to `/result` |
| Verdict | ✅ Same as `/result`. |

## 9. `/linkdiscord` — identity claim, PostgreSQL-first, JSON export-only

| | |
|---|---|
| Location | `cogs/edituser.py:1094` (`linkdiscord`) |
| SOURCE | authorized admin command (IGN + Discord user) |
| TRANSFORM | `PlayerRepository.claim_discord_id` (single `transaction`, `:1144`); then `players.json` export via `claim_ign` + `save_data` (`:1171-1173`) — best effort, never returns the DB claim |
| DESTINATION | PostgreSQL (`players` identity claim) primary; players.json export artifact optional |
| Discord MUTATION | **NO** (no role change) |
| Guards | No DB → command refuses loudly (no JSON-only claim, `:1126-1132`); JSON export failure loud but never reverts DB claim (`:1179-1185`). |
| Flag | D6 class `identity_claim_dualwrite` |
| Verdict | ✅ PG-first, JSON downstream export, no Discord mutation. |

## 10. `/edituser` (tier/IGN/discord_id) — admin edit → PG + Discord role + web

| | |
|---|---|
| Location | `cogs/edituser.py:1056`, confirm view `:937` → `execute_player_edit` + `_apply_roles` (`:1201` → `apply_role_actions` `_shared.py:57`) |
| SOURCE | authorized admin edit (stale-checked against current state) |
| TRANSFORM | DB transaction + audit first → **Discord role apply for tier edits** (`apply_roles` only when `field == "tier"`) → web best-effort |
| DESTINATION | PostgreSQL (player, tiers, cooldowns, history, audit) → Discord role → players.json/web |
| Discord MUTATION | **YES (authorized)** — only for `field == "tier"` edits, only after admin confirm; IGN/discord_id edits never touch roles |
| Verdict | ✅ Explicit admin action; role change gated to tier edits; stale-check prevents apply-what-you-didn't-see. |

## 11. `auto_grant_kit_role` — single-`member.edit` tier role engine

| | |
|---|---|
| Location | `cogs/roles.py:49-112` (`member.edit(roles=target)` at `:112`) |
| SOURCE | `data/kit_roles.json` mapping + caller-supplied target tier |
| TRANSFORM | atomic single `member.edit` (no add+remove intermediate state); `ok=False` → caller must NOT write PG mirror as confirmed |
| DESTINATION | Discord roles (one atomic swap of the kit's tier roles) |
| Discord MUTATION | **YES (helper)** — invoked ONLY from `/result` (`results.py:450`) and `/topresult`; never from reconciliation/observation/sync |
| Verdict | ✅ Authorized helper engine, no independent entry point. |

## 12. Manual/admin Discord role ops

External to the bot (Discord client / server admin UI). Out of audit scope, but
the bot never watches and re-syncs them back into players.json or PG as
authority — observation (`/sync discord`, reconciliation E3) mirrors them
Discord → PG only.

## 13. Web/GitHub reads & GitHub export

| | |
|---|---|
| Reads | `github_sync.py` fetch (contents API), used by `/sync check` / checkweb comparisons — read-only |
| Export | `github_sync.push_players` / `services/websync.py` `sync_website` — GitHub is the only destination; failure never changes Discord, never changes PG |
| Discord MUTATION | **NO** (both directions) |
| Verdict | ✅ GitHub is export-only (D6 `export_only`). No GitHub → PG current-tier path, no GitHub → Discord path. |

---

## Global checks

- **Discord mutation call sites (whole tree)** — exactly these, all authorized:
  - `cogs/roles.py:112` `member.edit(roles=target)` — `auto_grant_kit_role` helper (authorized promotion only)
  - `cogs/results.py:436-438` `member.add_roles/remove_roles` — `/result` optional roles (authorized)
  - `cogs/_shared.py:96,98,161,163` — `apply_role_actions` (used only by `/edituser` tier edits) and `apply_rollback_actions` (used only by `/sync discord-rollback`)
- **Zero stray mutation paths**: no `add_roles`/`remove_roles`/`member.edit` inside `db/`, `services/phase_d/`, `mirror_sync.py`, `tier_mirror.py`, `outbox_consumer.py`, `promotion.py`, `github_sync.py`, `websync.py`, `datacheck.py`, `checkweb.py`.
- **No JSON → Discord path**: `players.json` is written everywhere as a downstream export of Discord-confirmed or admin-authorized state; no reader of `players.json` `modes` feeds Discord or PG current tiers (Phase D import explicitly ignores `modes`).
- **No JSON → PG-current-tier path**: import (`import_data.py`) contributes identity + dated history only; mirror is written exclusively from Discord observation / promotion / manual admin (`player_current_tiers.source IN ('discord_sync','promotion','manual')`).
- **No PG → Discord path**: `db/` never imports `storage` for writing (D6 direction audit); reconciliation (E3) is observe-only by contract.

## Phase E action items surfaced by this audit

1. `/sync importdiscord` (and deprecated `checkweb apply`) writes JSON + web
   without updating the PG mirror → flag for E9/E13 docs; recommend retiring in
   favor of `/sync discord` + `/edituser`.
2. `/sync web` reads its canonical from `players.json` (JSON artifact), not the
   PG mirror → E1/E8 note: downstream-only, authority intact; PG becomes the
   canonical web source in a later phase (not Phase E, no unrelated refactor).
3. All other paths conform to the authority model already; no blocker found.