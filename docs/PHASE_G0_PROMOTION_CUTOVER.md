# Phase G0 — Production Promotion Cutover (internal trace + design)

> Scope: eliminate **H1** (legacy `players.json` participating in the live
> `/result` / `/topresult` current-tier decision) and give the promotion path
> exactly ONE canonical implementation.
>
> This document is the read-only architecture trace (Phase G0 step 1) and the
> design record (steps 2–3). Everything below was verified **in the current
> working tree**, not taken from the previous audit.

---

## 0. Correction to the "known state" in the phase brief

The brief states that `/result` and `/topresult` "currently do NOT pass
`session_factory` into `record_result` / `record_ht_fight`" and that
"`_db_record_result` / `_db_record_ht_fight` exist but were previously dead".

**That is stale for this working tree.** Verified call sites:

| Call | Evidence |
| --- | --- |
| `cogs/results.py:211` | `sf = getattr(self.bot, "db_session_factory", None)` |
| `cogs/results.py:283` | `record_result(..., session_factory=sf)` |
| `cogs/topresult.py:180` | `sf = getattr(self.bot, "db_session_factory", None)` |
| `cogs/topresult.py:310` | `record_ht_fight(..., session_factory=sf)` |

So the *mechanical* H1 flip already happened here. What is **not** done, and
what this phase actually had to finish:

1. the promotion **gate + commit orchestration is duplicated** in both cogs
   instead of living in one canonical service;
2. the **final Discord role state is never verified** on the success path, so
   "Discord-first" was in practice "Discord-request-didn't-raise first";
3. `services/phase_e/json_compat.py` still classified the live JSON
   promotion path as an **open** H1 finding, so the scanner could no longer
   detect a regression of this exact class;
4. `db/services/health.py` (unresolved promotions, stale outbox claims,
   dead-letter) had **no production caller**;
5. `/topresult` wrote its announcement status to **JSON in DB mode**;
6. `tests/test_g0_promotion_cutover.py` (added by the earlier attempt)
   created and destroyed the **shared embedded-PostgreSQL server**, which made
   244 other tests error out.

---

## 1. CURRENT PROMOTION FLOW (as found)

### 1.1 `/result` (`cogs/results.py::Results.result`)

```
 1  sf = bot.db_session_factory                                  cogs/results.py:211
 2  get_ticket(channel_id, session_factory=sf)                              :219
 3  validate_result_tier(tier, ticket.targetTier, ticket.currentTier)       :226
 4  get_kits(sf) / add_kit(sf)                                              :243
 5  record_result(..., session_factory=sf)                                  :265
        └─ services/results.py:423  sf is not None ?
              ├─ YES → _db_record_result                                   :607
              │        ONE transaction:
              │          _db_resolve_kit                    (INSERT kits)
              │          ResultRepository.get_by_key / delete(discord_pending)
              │          CooldownRepository.is_active  → "duplicate"
              │          TicketRepository.get_by_channel → validation
              │          PlayerRepository.claim_discord_id   (WRITE players)
              │          MirrorRepository.get_current  ← PREVIOUS TIER  :750
              │          ResultRepository.insert(promotion_status=discord_pending)
              │          CooldownRepository.upsert(waitlist)     ← ARMED
              │          TicketRepository.close_by_channel    ← CLOSED
              │          AuditRepository.append("result")
              └─ NO  → legacy `record_result._run` (services/store.transaction)
                        tx.get("players.json")   ← CURRENT TIER  :502
                        apply_result_to_players → previous tier
                        tx.set("players.json")   ← WRITES TIER    :509
                        tx.set("ht_results.json" / "cooldowns.json" / tickets / …)
 6  get_ticket / leave_queue / remove_pulled_player (sf)             :357–381
 7  Discord: channel perms reset, move_to(afk)                        :383–434
 8  Discord: optional add_role/remove_role (unverified)                :463–479
 9  Discord TIER MUTATION: auto_grant_kit_role                        :487
        cogs/roles.py:128  member.edit(roles=target)
        ok=True  ← derived ONLY from "no exception raised"
        ✗ final role state is NOT re-read on the success path
       (re-read happens ONLY in the except branch, :158–178)
10  PG COMMIT (gate duplicated inline):                                :505–543
        if isinstance(grant, TierRoleGrant) and grant.ok
           and grant.tier_role_id is not None:
            commit_promotion_with_wedge(...)
              → resolve_promotion_dimensions (own tx)
              → PromotionCommitService.commit_after_discord_success (one tx)
                   results.promotion_status = committed
                   MirrorServiceRepository.apply_observation
                     pg_advisory_xact_lock(player,kit)
                     player_current_tiers   ← CURRENT TIER
                     tier_history           ← previous_tier_id = mirror pre-image
                   audit_log("promotion_committed")
              → on failure: outbox_events(promotion_commit, confirmed=True)
```

### 1.2 `/topresult` (`cogs/topresult.py::TopResult.topresult`)

```
 1  sf = bot.db_session_factory                              cogs/topresult.py:180
 2  get_ticket(channel_id, sf) / get_kits(sf)                            :218
 3  validate_ht_fight_tier / _score / _status                           :264
 4  record_ht_fight(..., session_factory=sf)                            :292
        └─ services/topresult.py:406  sf is not None ?
              ├─ YES → _db_record_ht_fight                               :667
              │        ONE transaction: dedup, ticket validation,
              │        claim_discord_id, results.insert(discord_pending
              │        on win), ticket close + ht3 cooldown on LOSS,
              │        audit_log("ht_fight")
              └─ NO  → legacy `record_ht_fight._run`
                        players = tx.get("players.json")  ← CURRENT TIER  :511
                        find_player_tier_in(...)          :512
                        tx.set("players.json", players)    ← WRITES TIER  :542
                        tx.set("ht_results.json")                        :572
 5  promoted = new_tier and new_tier != previous_tier                   :385
 6  Discord TIER MUTATION: auto_grant_kit_role(..., session_factory=sf)  :393
        (same unverified success path as /result)
 7  PG COMMIT (the SAME gate, duplicated again)                         :410–446
        commit_promotion_with_wedge(..., result_key=f"ht_fight:{id}", …)
 8  set_ht_fight_announcement(id, sent|failed)   ← NO sf  ✗ JSON in DB mode
        cogs/topresult.py:107 / :487 / :503
```

### 1.3 The three "promotion implementations"

| # | Implementation | Status found | What it owns |
| --- | --- | --- | --- |
| A | `services.results.record_result._run` / `services.topresult.record_ht_fight._run` (JSON) | live **only** when `DATABASE_URL` is empty | result record + `players.json` current tier + history + cooldowns + ticket close |
| B | `services.results._db_record_result` / `services.topresult._db_record_ht_fight` | **live** (not dead) | result row at `discord_pending` + cooldowns + ticket close + audit |
| C | `db.services.promotion.commit_promotion_with_wedge` | live | mirror + `tier_history` + `promotion_committed` audit + wedge |

B and C are two *different* transactions for one logical promotion. That is
intentional (Discord mutates between them and the wedge must survive a failed
B→C write), but the **gate** that decides whether C runs was copy-pasted into
both cogs, and the `previous_tier` value used by C comes from two different
read sites.

---

## 2. Defects that survive the mechanical H1 flip

| id | Defect | Why it breaks an invariant |
| --- | --- | --- |
| **D1** | `auto_grant_kit_role` returns `ok=True` on the happy path with **no verification** of the resulting Discord role set (`cogs/roles.py:127–133`) | invariant 6 requires `mutate → verify actual final Discord role state → commit`. Today `ok=True` only means "the HTTP call did not raise", and that is then hard-coded into the wedge as `discord_role_confirmed=True` — the flag the outbox consumer treats as the licence to write authoritative mirror rows |
| **D2** | The invariant-6 gate is **duplicated** in `cogs/results.py:508–512` and `cogs/topresult.py:414–418` | two callers can drift; nothing structurally prevents a future caller from committing without a grant |
| **D3** | `TierRoleGrant.ambiguous` is **dead data** — no production caller reads it | a genuinely-unknown Discord outcome is reported exactly like a definite failure; only the (unreliable) reply text differs |
| **D4** | `services/phase_e/json_compat.py:70–108` still tags `record_result._run` / `record_ht_fight._run` as `json_first_write_pending_db_gate` ("LIVE in every deployment … open finding") | the static allow-list is keyed on `(module, qualname, func)`, so re-introducing the H1 bug (dropping `session_factory=sf`) would still **pass** the audit |
| **D5** | `db/services/health.py` has **no production caller** | operators cannot see unresolved promotions / stale outbox claims / dead-letter without SQL |
| **D6** | `set_ht_fight_announcement` is called **without** `session_factory` in all three `cogs/topresult.py` sites | in DB mode `results.announcement_status` / `announcement_message_id` stay `NULL` forever; the retry button can never resolve. The dead branch is `services/topresult.py:616 → _db_set_ht_fight_announcement` |
| **D7** | `tests/test_g0_promotion_cutover.py` (untracked, from the earlier attempt) built its **own** embedded-PostgreSQL server and called `server.cleanup()` | it destroys the session-scoped server from `tests/conftest.py`; 244 tests in files sorted after it error with `FileNotFoundError` on the Unix socket |
| **D8** | `services/phase_e/dual_write.py` (`json_export_enabled`, `watchdog_divergence`) has **zero production callers** | the documented "kill switch" for legacy JSON export gates nothing |

---

## 3. TARGET PROMOTION FLOW

```
/result  ·  /topresult
      │
      ├─ 1. record the RESULT (application state, Discord-independent)
      │      record_result(sf) / record_ht_fight(sf)
      │      → PostgreSQL only  (sf is not None ⇒ _db_record_*)
      │      → previous/current tier read from the PG mirror
      │      → players.json never read, never written
      │
      ├─ 2. mutate Discord          cogs.roles.auto_grant_kit_role
      │      single member.edit(roles=…)
      │      → returns TierRoleGrant(ok, tier_role_id, verified, ambiguous, note)
      │
      ├─ 3. VERIFY the ACTUAL final Discord role set
      │      PATCH response → else live re-read (fetch_member)
      │      verified=True only when the observed role-id set == intended set
      │
      └─ 4. commit PostgreSQL      db.services.promotion.commit_confirmed_promotion
             the ONLY production entry point
               ├─ grant not confirmed  → refuse: NO mirror, NO tier_history,
               │                          loud note (CASE 1/2/6)
               └─ grant confirmed      → resolve dims → ONE transaction:
                    results.promotion_status = committed
                    player_current_tiers   (advisory lock, previous_tier_id
                                            = mirror pre-image)
                    tier_history
                    audit_log("promotion_committed")
                  on any failure → outbox_events(promotion_commit,
                    discord_role_confirmed=True) on a FRESH session
                    (CASE 4 / CASE 7) — Discord is NEVER reverted
```

### Canonical-service contract

```python
async def commit_confirmed_promotion(session_factory, *, grant, **commit_kwargs)
    -> PromotionWedgeOutcome
```

* `grant` is the `TierRoleGrant` (duck-typed: `ok`, `tier_role_id`,
  `verified`, `ambiguous`, `note`).
* The service itself evaluates the gate. A caller **cannot** forget it, and
  cannot commit without a Discord-confirmed grant.
* It accepts no `session_factory` → refuses, writes nothing, says so loudly.
* It never mutates Discord and never reads a JSON artifact.

This makes the forbidden directions structurally impossible at the single
point every production caller must go through:

* `PostgreSQL → Discord` — the service holds no Discord handle at all.
* `players.json → current tier → Discord` — the service's inputs are the grant
  (Discord) and raw identifiers; the current tier comes from
  `MirrorRepository.get_current`.
* `players.json → current PostgreSQL tier` — the service imports no store.

---

## 4. Legacy JSON after the cutover

| Reader / writer | After G0 |
| --- | --- |
| `services.results.record_result._run` (`tx.get`/`tx.set("players.json")`) | runs **only** when `DATABASE_URL` is empty, i.e. when there is no PostgreSQL at all. Reclassified from `json_first_write_pending_db_gate` to `json_legacy_mode_only_pg_gated`. |
| `services.topresult.record_ht_fight._run` (same) | same |
| `services.tickets.find_player_tier` (`load_data("players.json")`) | ticket-creation display only; never the promotion decision |
| `cogs.edituser` / `cogs.datacheck` / `cogs.sync` readers | UI/diagnostics/export, unchanged |
| `services/playersync.py` | `/sync discord` analysis only, never the promotion path |

There is **no** JSON fallback *inside* the DB path: `sf is None` ⟺
`db.config.database_url()` empty ⟺ `storage.using_postgres()` false
(`db/config.py:19` delegates to `storage._database_url_from_environment`).
The two backends can never be mixed in one deployment.
