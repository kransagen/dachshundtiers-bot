# Phase F1 — Obsolete JSON Persistence Inventory & Classification

Datum: 2026-09-25 · Fáze F · Rozsah: celý produkční kód (cogy, services, db, utils, views, panel, bot.py)
Metoda: přímý grep/čtení call-sites (žádné background agenty — prokazatelně nefunkční).
Zdroje: `docs/MIGRATION_DESIGN.md` (§16/§17), `docs/PHASE_E_PATH_AUDIT.md`, `services/phase_e/json_compat.py`, `db/models/*`, `db/repositories/*`.

---

## 1. Klasifikační legenda

| Třída | Význam | Zacházení ve Fázi F |
|---|---|---|
| **A (dead)** | Žádný živý produkční call path (jen tooling/testy) | kandidát na smazání po důkazu |
| **B (migrated)** | Produkce už čte/zapisuje PG; JSON = export/compat | ponechat jako export, nic nepřepisovat |
| **C (export)** | Musí zůstat jako deterministický export pro GitHub/web | zachovat zápis, odebrat čtení z produkce (F4/F5) |
| **D (tooling)** | Konzumováno jen migrací/backup/restore toolingem | ponechat (F12: nemazat bez důkazu) |
| **E (audit)** | Append-only diagnostický audit, neautoritativní | ponechat JSON nebo volitelně AuditLog; nízké riziko |
| **F (production)** | STÁLE na JSON cestě v produkční exekuci | **F2/F3 přepis na relační repo** |
| **U (unsafe)** | Silent fallback / korektnostní riziko | **F10: odstranit (blokery)** |

---

## 2. Klasifikace per JSON klíč

| Klíč (data/*.json) | Třída | Poznámka |
|---|---|---|
| `players.json` | **F → C** | Po F3–F5 zůstane ČISTĚ export (F4: deprecated, export-only) — čte se v ~8 produkčních místech (viz §3) |
| `kits.json` | **F** → D | utils.get_kits/add_kit/remove_kit jsou JSON; po F2→D (tooling) |
| `kit_roles.json` | **F** → D | **runtime produkční read** v `cogs/roles.py auto_grant_kit_role:63` (promotion!) + /setkitrole |
| `evals.json` | **F** → D | utils.set_eval/get_evals (eval-eligibility /result) |
| `testers.json` | **F** → D | `cogs/queues.py save_data` + čtení queue flow |
| `cooldowns.json` | **F** → D | ht3.py pull zápis, result/cd čtení, edituser display |
| `ht3_cooldowns.json` | **F** → D | zdroj HT3 cooldownů (ht3.py:458, results.record_result, topresult) |
| `ht_tickets.json` | **F** → D | services/tickets.py kompletní stav ticketů (store.transaction) |
| `ht_ticket_logs.json` | **F** (migrovat) | log ticketů; čten v UI flow; → AuditLog |
| `ht_results.json` | **F** → D | results/topresult append-only kanonická historie → Result repo |
| `queue.json` | **F** → D | services/queue_service + cogs/queues |
| `active_queues.json` | **F** → D | cogs/queues (transaction) + panel.py read |
| `queue_messages.json` | **F** → D | panel.py update_panel read; → Queue.panel_message_id |
| `tournaments.json` | **F** | **BEZ relační tabulky → rozhodnutí §5** |
| `pulled_players.json` | **F** | **BEZ relační tabulky → fold do QueueEntry(PULLED) / nová tabulka** |
| `testers_stats.json` | **F** | **BEZ relační tabulky → odvodit z Result / nová tabulka** |
| `ht3_panel_message.json` | **F** | → BotConfig; čteno kits.py:23, bot.py startup |
| `queue_channels.json` | **F** | → BotConfig; čteno config.get_queue_channel_id |
| `playersync_log.json` | **F/E** | **/sync discord-rollback čte tento JSON** (role_sync.get_playersync_log) — viz §5 nuance |
| `playersync_rollback_log.json` | E | append-only audit |
| `websync_log.json` | E | append-only audit (services/websync) |
| `checkweb_log.json` | E | append-only audit (services/checkweb) |
| `datacheck_log.json` | E | append-only audit (services/datacheck) |
| `edituser_log.json` | E | append-only audit (services/edituser) |
| `db blob (JSONB key-value)` | **B/F** | storage.py dvojitý backend; Phase F odstraňuje blob-y jako perzistenci, NE nutně samotný storage (viz §5) |

---

## 3. Call-site inventář (ověřené writers/readers)

### players.json
- **Writers**: `cogs/_shared.save_players` (:180-186, store.transaction) · `services/results.record_result` (append + close ticket + HT3 cooldown) · `services/topresult.record_topresult` (append, ht_fight) · `cogs/sync.py` importdiscord confirm (:1081-1094 save_players) + checkweb apply (:1414-1428) · `cogs/edituser.py` :1171-1173 (export po /linkdiscord, best-effort) · `services/phase_e/json_compat.export_players` (tooling) · migration tooling
- **Readers**: `cogs/sync.py` check :1550 / web :999/:2006 / importdiscord :1081 / checkweb :1414 · `services/tickets.py` :139 (find_player_tier / effective_ticket_tier — **next-tier computation**!) · edituser.py :95/:314/:369/:635/(:+5) · services/checkweb/websync/playersync/datacheck (parametry) · `cogs/_shared.kit_display_map`? (ne — čte kits.json)
- **Relační ekvivalent**: `Player` + `PlayerCurrentTier` + `TierHistory` + `PlayerRepository` (CLAIM_*, RESOLVE_*, PlayerIdentityError) + `db/services/tier_mirror.py` + `db/services/promotion.py`

### kits.json
- **Writers**: `utils.add_kit` (:91-100, save_data) / `utils.remove_kit` (:104-113)
- **Readers**: `utils.get_kits` (:23-32, load_data) — voláno z results/topresult/ht3/edituser/queues/kits/views/roles (autocomplete+validace) · `cogs/_shared.kit_display_map`
- **Relační**: `Kit` + `KitRepository` + `ensure_dimensions` (tier ladder sync)

### kit_roles.json
- **Writers**: `cogs/roles.py /setkitrole` (store.transaction)
- **Readers**: `cogs/roles.py auto_grant_kit_role` **:63 (load_data, runtime promotion)** · `cogs/sync.py` :226/:1551 (check + gather) · `cogs/edituser.py` :1226 · `services/checkweb.analyze_checkweb` (roles_map param) · datacheck
- **Relační**: `KitRole` + `KitRoleRepository`

### evals.json / testers.json
- **Writers**: utils.set_eval (:167-175) / unset :189 · queues.py save_data(testers.json)
- **Readers**: utils.get_evals (:155-156) · /result eval-eligibility · queues cog
- **Relační**: `Evaluation`+`EvaluationRepository`; `Tester`+`TesterRepository`

### cooldowns.json / ht3_cooldowns.json
- **Writers**: cogs/ht3.py :107 (pull: cooldown + panel message + pulled) · services/results.record_result (HT3) · services/topresult (HT3+ na prohru)
- **Readers**: cogs/ht3.py :457-467 (queue+HT3 cd check) · cogs/edituser.py :152-153 (display) · result/queues flow
- **Relační**: `Cooldown` (typ waitlist|HT3, uq_cooldowns_waitlist / uq_cooldowns_kit partial unique) + `CooldownRepository` (COOLDOWN_HT3 / COOLDOWN_WAITLIST)

### ht_tickets.json / ht_ticket_logs.json
- **Writers**: services/tickets.py — create/claim/close/reopen/unclaim/set_panel (store.transaction na HT_TICKETS_FILE; log_ticket_event na HT_TICKET_LOGS_FILE)
- **Readers**: services/tickets.get_ticket/find_open_ticket/get_ticket_logs · cogs/ht3.py (panelMessageId) · datacheck (orphaned_tickets) · results (close → record_result)
- **Relační**: `Ticket` (channel_id unique, panel_message_id, status open/closed) + `TicketMember` + `TicketRepository`; logy → `AuditLog` (rozhodnutí §5)

### ht_results.json
- **Writers**: services/results.record_result (append-only, idempotence 1 ticket = 1 result) · services/topresult (record + set_ht_fight_announcement)
- **Readers**: cogs/topresult (get_ht_fight_results) · /testerstats · datacheck (orphaned_results) · edituser/history display
- **Relační**: `Result` + `ResultRepository` (ANNOUNCEMENT_*, PROMOTION_COMMITTED/DB_FAILED_OUTBOXED/DISCORD_FAILED/DISCORD_PENDING)

### queue.json / active_queues.json / queue_messages.json
- **Writers**: services/queue_service (transaction) · cogs/queues.py (transaction na active_queues/queue/queue_messages.json; save_data queue_messages/testers.json) · cogs/results.py (leave_queue/remove_pulled_player)
- **Readers**: cogs/queues.py (load_data) · panel.py update_panel (load_data queue_messages+active_queues) · views.py · ht3.py
- **Relační**: `Queue` (+panel_message_id!) + `QueueEntry` + `QueueRepository` (QUEUE_ENTRY_LEFT/PULLED/TESTED/WAITING)

### tournaments.json / pulled_players.json / testers_stats.json
- tournaments.json: writers cogs/tournaments.py save_data (:36/:245/:370); readers tournaments.py (+views?)
- pulled_players.json: writers cogs/ht3.py :205; readers :194 (pull state)
- testers_stats.json: writers cogs/results.py (/testerstats); readers cogs/results.py (statistiky)
- **Relační ekvivalent: ŽÁDNÝ — viz §5**

### ht3_panel_message.json / queue_channels.json
- ht3_panel_message.json: writers cogs/ht3.py :107; readers cogs/kits.py :23, bot.py startup (panel restoration :477-478)
- queue_channels.json: writers /setqueuechannel; readers config.get_queue_channel_id (panel.py, queues)
- **Relační**: `BotConfig` (key/value JSONB) → nahradí oba

### *_log.json (6 souborů)
- playersync_log.json: writer services/playersync (transaction); reader role_sync.get_playersync_log → **/sync discord-rollback** (produkční read!)
- playersync_rollback_log / websync_log / checkweb_log / datacheck_log / edituser_log: append-only audit, čteny jen příslušnými get_* fcemi

---

## 4. Unsafe patterny (U — F10 blokery)

| Místo | Riziko |
|---|---|
| `cogs/roles.py auto_grant_kit_role:63` (load_data KIT_ROLES_FILE default {}) | korupce/absence → promotion tiše nepřidělí roli |
| `utils.get_kits` (load_data default []) | korupce → všechny kit validace tiše selžou |
| `utils.get_evals` / `utils.set_eval` (save_data) | korupce → eval stav tichá ztráta/záměna |
| `services/tickets.py:139` load_data(players.json) | next-tier výpočet na tichém defaultu |
| `storage.load_data` silent default na korupci | částečně ošetřeno strict proby v cogs/sync.py (_corrupt_data_files, DataCorruptionError); OSTATNÍ čtenáři nechráněni |
| `services/tickets.py` store.read/transaction | po přepisu na reposervice se obchází (F2) |

---

## 5. Rozhodnutí k potvrzení (F2/F3)

1. **tournaments.json, pulled_players.json, testers_stats.json** — nemají relační tabulku. Doporučení:
   - `tournaments.json` → **nové tabulky** `tournaments` + `tournament_entries` (alembic) + `TournamentRepository` (operační stav turnaje).
   - `pulled_players.json` → **fold do `QueueEntry`** (status `QUEUE_ENTRY_PULLED` existuje; ověřit sémantiku pull = odstranění z queue) — jinak nová tabulka `pulled_players`.
   - `testers_stats.json` → **odvodit za běhu z `Result`** (per-tester month tallies; ověřit, že Result má tester identitu + month) — jinak nová tabulka.
2. **`*/_log.json` audit soubory (playersync_rollback/websync/checkweb/datacheck/edituser)** — ponechat jako JSON append-only (neautoritativní diagnostika; F12 je chrání). Migrace do AuditLog = volitelně později.
3. **`playersync_log.json` + /sync discord-rollback** — dnes rollback čte starý JSON log. DiscordSyncService (mirror) píše DB `sync_runs`/`sync_actions`. **Ověřit**, zda mirror píše i JSON; rollback zdroj přepnout na `SyncActionRepository` (applied akce s discord_role_id) — jinak rollback zůstane na JSONu (F7).
4. **checkweb/importdiscord „use_discord"** — sémantika „tier := Discord tier BEZ zápisu do historie". Po přepisu na PG: zachovat bez TierHistory (dokumentovat) NEBO zapisovat historii se zdrojem => rozhodnutí v F2/F3 (doporučeno: zachovat stávající sémantiku).
5. **storage.py dual-backend (JSONB blob)** — po F3 zůstává storage jen pro export/audit JSON logy a tooling; relační perzistence = ORM. Samotný storage.py NEmazat (D — tooling/restore).

---

## 6. F2 — Mapa náhrad (JSON op → relační repo op)

| Doména | JSON op (dnes) | Relační náhrada | Poznámky |
|---|---|---|---|
| Identity | `claim_ign(players, discord_id, ign)` | `PlayerRepository.claim_discord_id(session, ...)` (existuje; /linkdiscord už DB-first :1144-1173) | export-only do players.json po claimu |
| Current tier / next tier | `services/tickets.py:128` load_data → find_player_tier/effective_ticket_tier | `PlayerRepository.get_current_tier` + `db/services/tier_mirror` | remove JSON read |
| Player edit (/edituser) | read players.json, mutate, save | `PlayerRepository` (tier+history) + `TierHistoryRepository` | rozhodnutí: zápis historie |
| Results | `services/results.record_result` (players+ht_results+ticket+cd) | `ResultRepository.record` + Player tier + TierHistory + TicketRepository.close + CooldownRepository(HT3) | idempotence 1 ticket→1 result zachovat (DB unikátní constraint) |
| topresult | `services/topresult.record_topresult` + set_ht_fight_announcement | tentýž pipeline s `result_key=ht_fight:{id}`; `ResultRepository.set_announcement` | prohra: close+cd; výhra: nic |
| testerstats | testers_stats.json agregace | `ResultRepository` per-tester/month query | viz §5.1 |
| Tickets | services/tickets CREATE/CLAIM/CLOSE/REOPEN/SET_PANEL (store.transaction) | `TicketRepository` + `TicketMemberRepository` | panel_message_id už je na Ticket |
| Ticket logy | log_ticket_event → ht_ticket_logs.json | `AuditLogRepository` (event_type=ht_ticket_event) | nebo E (ponechat) |
| Kity | utils.get_kits/add_kit/remove_kit | `KitRepository.list/create/delete` + `ensure_dimensions` | wrappery v utils → volající se nemění |
| Kit role | /setkitrole (store.transaction) + auto_grant_kit_role (load_data) | `KitRoleRepository.upsert/get_role_id` | **auto_grant: bez silent fallbacku** (raise/alert) — F10 |
| Evals | utils.set_eval/get_evals | `EvaluationRepository` (add/remove/list) | wrap v utils |
| Testers | queues testers.json | `TesterRepository` | has_tester_role: repo check + role names |
| Cooldowns | cooldowns.json/ht3_cooldowns.json | `CooldownRepository` upsert/expired/delete (WAITLIST/HT3) | uq_* unique indexy = přirozená idempotence |
| Queue | queue_service join/leave/status; active_queues.json; queue_messages.json | `QueueRepository` + `QueueEntryRepository` (ENTRY_* stavy) + Queue.panel_message_id | panel obnova čte Queue ne queue_messages.json |
| Pulled players | ht3.py pulled_players.json | QueueEntry(PULLED) / nová tabulka | §5.1 |
| Turnaje | tournaments.json | nové Tournament tables + repo | §5.1 |
| Bot config | queue_channels.json, ht3_panel_message.json | `BotConfigRepository.get/set("queue_channels"/"ht3_panel_message")` | config.get_queue_channel_id → repo |
| Mirror sync | DiscordSyncService (už DB) | beze změny | jediná oprava: rollback zdroj (viz §5.3) |
| Canonical export (F4/F5) | read players.json → sync_website | **jeden deterministic exporter** `pg → players.json` (json_compat na PG datech) použitý: /sync web, /linkdiscord, /result, importdiscord/checkweb apply, F11 | **F5 deliverable** |
| /sync check | players/kit_roles load_data | PlayerRepository + KitRoleRepository + canonical export + Result/Ticket repos | read-only zůstává |

---

## 7. Autoritativní směr (F13 preview — kontroly nad F2 mapou)

- POVOLENO: Discord → PG mirror (observe-only) · explicitní promotion Discord → confirm → PG · PG → export JSON → GitHub.
- ZAKÁZÁNO: JSON → aktuální Discord tier · JSON → PG current tier · PG current tier → Discord (mimo explicitní promotion confirm) · GitHub → current tier · reconciliation → Discord role mutace.
- F2 mapy výše žádný zakázaný směr neobsahují.

---

## 8. F2 implementation progress (2026-09-25)

Hotovo:
- **Alembic `b2c3d4e5f6a7`** (`migrations/versions/b2c3d4e5f6a7_add_tournaments_and_queue_username.py`, down_revision `a1b2c3d4e5f6`):
  `tournaments` + `tournament_entries` (signupy; unikátní (tournament, player); FK
  `tournament_entries.tournament_id` ON DELETE CASCADE) + `queue_entries.username`
  (nullable Text).
- **`db/models/tournaments.py`** (Tournament, TournamentEntry) + registrace v
  `db/models/__init__.py`; `QueueEntry.username` v `db/models/ops.py`.
- **`db/repositories/tournaments.py`**: get / get_by_kit (blokuje vytvoření, dokud
  existuje libovolný řádek pro kit — JSON-faithful) / list_all / create / mark_ended /
  is_participant / add_participant / list_participant_ids / count_participants /
  delete (entries + tournament).
- **`cogs/tournaments.py`**: /createturnaj, /deleteturnaj a `end_tournament_signup`
  čtou/zapisují DB; bez DB → jasná chyba, žádný JSON fallback. /turnajresult beze
  změny (jen Discord ping).
- **`views.py` TournamentSignupView**: přihlášení přes TournamentRepository
  (session_factory z `interaction.client.db_session_factory`).
- **`bot.py`** on_ready restore turnajů: čte aktivní turnaje z DB, registruje view,
  plánuje auto-end; blok se přeskočí bez DB.
- `tests/conftest.py` ALL_TABLES += tournaments, tournament_entries;
  `tests/test_repos_tournaments.py` (repo + schema testy).

Rozhodnutí navíc (doplněk k §5.1): `queue_entries.username` — legacy queue entry
nese Discord display name, který relační tabulka neměla; panely ho renderují, proto
sloupec (nullable, best-effort). Semantika turnaje zůstává JSON-faithful: řádek žije
až do /deleteturnaj; ended turnaj blokuje nový pro stejný kit.

## 8b. Todo #6 — services/tickets.py DB režim (hotovo 2026-09-25)

- **`db/repositories/tickets.py`**: `TicketRepository.open` + `category_id` /
  `panel_message_id`; nové `list_all`, `set_panel_message`, `reopen_by_channel`;
  `claim(claimer_id=None)` = unclaim (typ Optional[int]).
- **`db/repositories/kits.py`**: `KitRepository.get_by_name` (case-insensitive lookup
  dle display name; `Kit.name` unikátní).
- **`services/tickets.py`**: všechny veřejné funkce přijímají nepovinný
  `session_factory`; `None` → beze změny JSON cesta (test seam, legacy zelené);
  předán → PostgreSQL transakce přes repos. Zachován JSON kontrakt:
  - `create_ticket` — claim identity (PlayerIdentityError → `identity_conflict`),
    rezoluce kitu dle display name (auto-create key=lower) a tieru (LT3E=virtual),
    prevence duplicit přes `list_open`; `invalid_kit` při prázdném kitu.
  - `claim/unclaim/add_member/remove_member` — plná JSON odpovídající sada výsledků
    (own_ticket / already_claimed / not_claimer / is_owner / is_claimer /
    player_not_found / identity_conflict / already_member / not_member).
  - `add_member` nový volitelný parametr `member_name` (vytvoří runtime hráče).
  - `close_ticket` + HT3 cooldown upsert (`cooldown_type='ht3'`, kit_id,
    partial index kit-bound); `reopen_ticket` + cooldown check → `cooldown` s
    `remaining_ms`.
  - ticket logy → `audit_logs` (entity_type='ticket', entity_id=channel_id,
    details={'ts', 'details'}); `get_ticket_logs` řadí asc.
  - `_db_ticket_to_dict` vrací ownerId/claimerId/members jako **Discord ID**.
- **`services/phase_e/json_compat.py`**: manifest `services.tickets` reader
  posunut 128 → 139 (posun řádků; flow `json_ui_read` beze změny).
- **`tests/test_services_tickets_db.py`** (16 testů): create+duplicate, identity
  conflict, kit rezoluce, get/get_tickets/find_open, claim, unclaim+force,
  add/remove member, close+cooldown, reopen+cooldown/expiry, set_panel_message,
  logy round-trip, fight type + panel, Discord-ID kontrakt. JSON režim ověřen
  test_tickets.py (35 testů) beze změny.
- Full suite: **830 passed** (814 + 16), ruff clean na změněných souborech.
## 8c. Todo #7 — services/results.py + topresult.py DB režim (hotovo 2026-09-25)

- **`services/results.py`** — `record_result` deleguje na `_db_record_result` při
  předaném `session_factory`; čtečky wired: `_db_get_result_by_ticket`,
  `_db_get_results_for_player` (sort timestamp asc), `_db_get_all_results`;
  `_db_result_to_dict` (sdílený s topresult.py): id = result_key bez prefixu,
  playerId/evaluatorId jako Discord ID (fallback PK), displayTier = "LT3 + eval"
  při eval, timestamp ms UTC. Evaluator uložen jako FK (discord_id lookup;
  evaluatorId/evaluatorName = '' když player row neexistuje); queue duplicate
  přes cooldown waitlist (kit_id=None, partial index kit-bound); ticket finalizace
  uzavírá ticket + HT3 cooldown; JSON-kit strings → Kit dimension rows
  (get_or_create; parity: JSON /result ukládá libovolný kit string, ticket path
  porovnává kit s ticketem).
- **`services/topresult.py`** — `record_ht_fight` deleguje na `_db_record_ht_fight`;
  rename `results_dict` → `_db_ht_fight_to_dict`; čtečky wired:
  `_db_set_ht_fight_announcement`, `_db_get_ht_fight_result_for_ticket`,
  `_db_get_ht_fight_results`. Pořadí validací: idempotence (ticket:
  `get_by_key("ht_fight:{tid}:ht_fight")`; free: `_db_find_recent_ht_fight_duplicate`
  přes `list_free_ht_fights`, fingerprint subtype/score/outcome/opponent_id/
  tier_status, okno HT_FIGHT_DEDUP_WINDOW_MS) → validace → free dedup
  (read-only `KitRepository().get_by_name`, žádný create na duplicate cestě) →
  claim player → previous tier z mirroru → bridge/promoted (Won+bridge strictly
  higher → invalid_bridge; Won+current → next_ticket_tier) → insert →
  loss uvnitř ticketu: close_by_channel + HT3 cooldown (source="ticket_close")
  + audit action="ht_fight" details "N/A (prohra)". HT Fight score formát
  `^\d+-\d+$` (pomlčka). Player claim povinný na Won i Lost (FK NOT NULL
  divergence oproti JSON — akceptováno, F13 Discord-first).
- **`db/services/promotion.py`** — update-branch None-guards pro
  subtype/evaluator_id/ticket_channel_id/opponent_id/opponent_name/notes
  (topresult cog commit je neposílá); kind/player_id/kit_id/new_tier_id/
  previous_tier_id/bridge_tier_id/tier_status/score/outcome/eval_flag/date
  bezpodmínečné; `promotion_status='discord_pending'` při výhře.
- **`db/repositories/results.py`** — `set_announcement`, `list_free_ht_fights`
  (fingerprint window), `insert`, `get_by_key`, `list_for_player`, `list_all`.
- **`tests/test_services_results_db.py`** (14 testů): /result ticket
  created/duplicate/errors/queue+cooldown/eval/readers/commit preserves
  row metadata; /topresult ht_fight win (mirror+queue promotion wedge),
  loss (close + HT3 cooldown + audit), error branches (not_found /
  not_fight_ticket / wrong_player / wrong_kit / invalid_tier / invalid_score /
  invalid_outcome / invalid_bridge), free win promote + dedup, bridge deny,
  announcement + readers, commit preserves subtype. JSON režim ověřen
  test_results.py + test_topresult.py beze změny.
- Poznámka (test-design): DB `create_ticket` dedupuje otevřené tickety per
  owner+kit — testy více ticketů pro jednoho hráče používají různé kits/owners.
- Full suite: **844 passed** (830 + 14), ruff clean na změněných souborech
  (services/results.py, services/topresult.py, db/services/promotion.py,
  db/repositories/results.py, tests/test_services_results_db.py).

## 8d. Todo #8 — services/queue_service.py DB režim (hotovo 2026-09-25)

- **`services/queue_service.py`**: join_queue / leave_queue / pop_for_kit /
  remove_by_player_id / save_pulled_player / remove_pulled_player přijímají
  nepovinný `session_factory` (None → beze změny JSON cesta; předán → PostgreSQL
  transakce). Zachován JSON kontrakt výsledků:
  - `join_queue` — closed (kit bez aktivní fronty) → cooldown (waitlist, kit_id
    NULL; remaining = expires_at − now) → duplicate (waiting entry) → joined
    (claim identity + enqueue s `position` = next, `username` = Discord
    display name, `ign`, `kit_id`); `identity_conflict` při PlayerIdentityError.
    Souběžný join stejného hráče: unique index `uq_queue_waiting_player` /
    `uq_players_discord_id` → IntegrityError → **jeden retry** (čerstvá
    transakce vidí committed stav → duplicate), stejně jako JSON critical section.
  - `leave_queue` — waiting → `left` (removed_at + removed_reason="leave"), bool.
  - `pop_for_kit` — první waiting záznam kitu (pořadí position) → `pulled`
    (pulled_at); vrací dict {id=Discord ID, username, ign, kit=display name,
    joinedAt, testerId=None}; None když fronta prázdná/neexistuje.
  - `remove_by_player_id` — waiting → `pulled` pro všechny aktivní fronty
    (JSON „vyjmi z queue" = pull flow, ne leave), bool.
  - `save_pulled_player` — nejnovější `pulled` záznam hráče dostane
    `room_channel_id` (JSON overwrite jednoho záznamu na hráče); no-op bez
    záznamu — přednastavený přístup `/mktesterroom` mimo frontu řeší rewiring
    cogs v todo #3 (dokumentováno v docstringu).
  - `remove_pulled_player` — pulled → `tested` (removed_at + reason), bool.
- **`db/repositories/queues.py`**: `enqueue` +`username`; `list_waiting_for_player`
  (globální sémantika queue.json); `list_by_status` (newest first).
- **`tests/test_services_queue_db.py`** (12 testů): join success (username row),
  closed (kit bez fronty / neznámý kit), cooldown block/expiry, duplicate,
  souběžné joiny (same user → duplicate+joined; dva uživatelé → oba), leave
  (left + reason, druhé volání False), pop first-of-kit (per-queue pozice!),
  remove_by_player_id, pulled roundtrip (room_channel_id → tested), no-op bez
  entry. JSON režim ověřen test_queue_service.py (15 testů) beze změny.
- Full suite: **856 passed** (844 + 12), ruff clean na změněných souborech.

## 8e. Todo #3 — pulled_players fold do QueueEntry + cogs/ht3.py rewire (hotovo 2026-09-25)

- **Sémantika pullu ověřena**: pop/remove z fronty = označení QueueEntry
  ``pulled`` (+ room_channel_id při save_pulled_player) — fold je čistý.
- **Nový `services.queue_service.preset_player_room(player, channel_id, *,
  session_factory)`** — přednastavený přístup (hráč ZŮSTÁVÁ ve frontě):
  JSON režim = pulled_players.json (stejný dict jako dřívější inline zápisy);
  DB režim = ``room_channel_id`` na čekajícím záznamu hráče; bez čekajícího
  záznamu no-op (odůvodnění: /result odebere práva přepisem všech kanálů,
  /skip na ne-ve-frontě hráče stejně nic nedělá).
- **`cogs/ht3.py` `/add` legacy větev** (tester roomka, ne HT ticket): inline
  load_data/save_data → ``preset_player_room`` s
  ``session_factory=getattr(self.bot, "db_session_factory", None)`` (F10 vzor).
- **Testy**: preset na waiting entry (room_channel_id, status zůstává waiting,
  pulled_at None), preset bez entry no-op, JSON preset roundtrip → plný běh
  **859 passed** (+3).
- Zbývající pulled_players čtenáři/zapisovatelé (cogs/queues.py /mktesterroom +
  /skip, cogs/results.py remove_pulled_player, views.py pull flow) se wireují
  v todo #10 („zbylé cogy"), service vrstva je kompletní.

## 8f. Todo #4 — testers_stats.json runtime derivace + tester_credits ledger (hotovo 2026-09-25)

- **Návrh odvození (ověřen proti zdroji)**: JSON `_log_tester_stat` se volá na
  konci /result pro KAŽDÝ zaznamenaný výsledek; `topresult.py` statistiky
  NEloguje → **ht_fight se nikdy nepočítá**; `evaluator_id =
  str(interaction.user.id)` vždy (ticket i queue) = stejná identita jako klíč
  JSON; duplicitní replay vrací brzy (bez řádku i bez statistiky) → počet
  `Result` řádků (WHERE evaluator_id = player.id AND kind IN ('ticket',
  'queue')) = JSON total.
- **Nový `services.tester_stats.py`** (dual-mode, F10 vzor): `tester_stats`,
  `tester_leaderboard(period)`, `credit_tester`, `remove_tester_credit`.
  - DB režim: kits/tiers/lastTested/hourly jen z `Result` (Prague tz →
  `%m.%Y` / `%d.%m.%Y` / hodina); total/monthly = výsledky + ledger kredity;
  tier display = `TierDefinition.display_name` (+ " + eval"); leaderboard
  klíč = **discord_id** (join Player na evaluator_id/tester_id; skips NULL).
  - `/result` v DB režimu statistiky NEzapíše (runtime derivace) — JSON režim
    zachovává `_log_tester_stat`.
- **`db/models/tester_credits.py` + repo + migrace `c4d5e6f7a8b9`**: jediní
  mutátoři statistik historicky jsou /addtest (kredity) a /removetest
  (odečet) — nelze odvodit z Result → ledgr `tester_credits` (unique
  (tester_id, month), `credit` akumuluje ORM increment — pozor: původní
  SQL-update + ORM add dvojnásobil částku).
- **cogs/results.py rewire**: 5 míst na service (step 4 guard, /testerstats,
  /testersstats, /addtest, /removetest). `current_date` zůstává vně guardu
  (používá se v embed footeru). Autority-scan allowlist line numbers
  aktualizovány (443/445 → 444/446 → nový posun).
- **Známý edge `_db_credit_tester`**: `get_or_create_by_discord_id(..., ign="")`
  — u dvou RŮZNÝCH neznámých testerů hrozí unikátní konflikt `lower(ign)` na
  prázdném IGN (IntegrityError, /addtest hlásí chybu). Vzácné, akceptováno.
- /removetest v DB režimu odečítá jen z credits aktuálního měsíce (Result
  count je neměnný) — drobná divergence oproti JSON (podčítal total), ok.
- **Testy**: tests/test_services_tester_stats_db.py (10 testů) — derivace bez
  ht_fight (total/kits/tiers/lastTested/hourly parity), neznámý = None,
  credits merge (total+monthly), credits-only, akumulace stejného měsíce,
  neznámý tester → vytvoření hráče, leaderboard all/current (discord klíče),
  remove credit. JSON režim ověřen 1 testem (monkeypatch DATA_DIR).
- Full suite: **869 passed** (859 + 10), ruff clean na všech dotčených.
- Testy běží na reálném embedded PostgreSQL (alembic upgrade); `tester_credits`
  přidána do conftest ALL_TABLES.

## 8g. Todo #5 — BotConfig (queue_channels + ht3_panel_message) (hotovo 2026-09-25)

- **Návrh**: `bot_config` je generic key→JSONB store (`BotConfigRepository.get/set`
  + `pg_insert ... on_conflict_do_update`); klíče `"queue_channels"` a
  `"ht3_panel_message"` pokrývají oba legacy soubory bez změny schématu.
- **Nový `services/config_store.py`** (dual-mode, F10 vzor): `get/set_queue_
  channel_id`, `get/set_ht3_panel` — DB režim přes BotConfigRepository,
  JSON režim resp. `queue_channels.json` / `ht3_panel_message.json`.
  Přednost zůstává runtime přepis > env `QUEUE_CHANNELS`/defaulty (env je
  konfigurace, ne legacy data); DB JSONB ukládá dict `{lowered_kit_key: int}`
  resp. `{"message_id": str, "channel_id": str}` (parity s JSON).
- **Rewire (zůstává JSON v secích, kde se nepředá session_factory)**:
  - `cogs/ht3.py` `/ht3panel` writer → `set_ht3_panel(message.id,
    target_channel.id, session_factory=...)`; smazán HT3_PANEL_MESSAGE_FILE
    konstanta + nepoužitý `save_data` import.
  - `cogs/kits.py` `_refresh_ht3_panel` (read pro /addkit) →
    `get_ht3_panel(session_factory=...)`; `/addqchannel` writer →
    `set_queue_channel_id(..., session_factory=...)`; smazán nepoužitý
    `load_data` import.
  - `bot.py` on_ready :331 (re-registrace HT3 panel view) →
    `get_ht3_panel(session_factory=self.db_session_factory)`.
  - Zbylí čtenáři kanálů fronty (`cogs/queues.py` :100/:212, `panel.py` :53,
    plus `update_panel` callers views.py/results.py/queues.py) se wireují
    v todo #10 — `config.get_queue_channel_id` tam zatím zůstává.
- **Testy**: tests/test_services_config_store_db.py (9 testů) — DB: override
  roundtrip/replace, env fallback (monkeypatch QUEUE_CHANNELS), override >
  env, HT3 panel default {}/roundtrip/replace; JSON: queue + HT3 file parity.
  Pozn.: `anchorpvp` má env default → testy používají `molepvp`; save_data
  pretty-printuje → file asserty přes json.loads.
- Full suite: **878 passed** (869 + 9), ruff clean na všech dotčených.

## 8h. Todo #9 — kits.json / evals.json / kit_roles.json → repos (hotovo 2026-09-25)

- **Návrh**: tři legacy soubory nahrazeny relačními tabulkami bez změny
  schématu — `kits` (KitRepository), `evaluations` (EvaluationRepository),
  `kit_roles` (KitRoleRepository + TierDefinition); hráč se v evalovém
  řádku váže přes `players` (FK NOT NULL, divergence viz níže).
- **Nové služby** (dual-mode, F10 vzor — `session_factory=None` → legacy
  JSON deleguje na utils beze změny):
  - `services/kit_catalog.py`: `get_kits` (ORDER BY name; prázdná TABULKA
    = fallback DEFAULT_KITS — parity s chybějícím souborem; pokud existují
    kity, ale všechny neaktivní → `[]`, jako prázdný soubor), `add_kit`
    (case-insensitive duplicita → False; reaktivace neaktivního kitu ->
    True, jako JSON re-appenda), `remove_kit` (deaktivace active=False,
    druhé volání False), `canonical_kit_name` (case-insensitive `Kit.name`),
    `kit_autocomplete` (session_factory se odvodí z interakčního clienta).
  - `services/evals.py`: `has_eval` / `set_eval` / `unset_eval` — akce nad
    `Evaluation` (grant/revoke/has_active). **Divergence (F10 dokumentace)**:
    eval vyžaduje existující Player + Kit řádek (FK NOT NULL); neznámý
    hráč/kit → False (JSON by zapsal IGN). Druhý set_eval na aktivním evale
    = idempotentní True (JSON semantika dict přepisu; bez tohoto guardu by
    `uq_eval_active` partial unique index hodil UniqueViolation → 500 —
    bug chycený testem, opraveno).
  - `services/kit_roles.py`: `get_kit_role_map` / `get_all_kit_role_maps`
    ( {tier_code: role_id} ), `set_kit_role` (upsert přes
    `uq_kit_roles_kit_tier`), `unset_kit_role` (delete; False když není).
    JSON režim ukládá role_id jako řetězec (legacy), DB jako int —
    volající (`auto_grant_kit_role`) int()-normalizují (dokumentováno v
    modulovém docstringu). JSON set/unset už NEPÍŠOU silently no-op:
    zapisují do kit_roles.json taky (parita).
- **Rewire (kogové, kteří už předávají session_factory)**:
  - `cogs/roles.py`: `auto_grant_kit_role` dostal `*, session_factory=None`
    → čte `get_kit_role_map` místo `load_data(KIT_ROLES_FILE)`; /setkitrole
    a /unsetkitrole → `set_kit_role`/`unset_kit_role`; /kitrole →
    `get_all_kit_role_maps`. Smazány `KIT_ROLES_FILE` + `transaction`/
    `load_data` importy (cog je celý DB-ready).
  - `cogs/results.py`: /result registrace kitu → `get_kits`/`add_kit`
    (await, session_factory), eval zápis → `set_eval` (await), promoce →
    `auto_grant_kit_role(..., session_factory=...)`.
  - `cogs/topresult.py`: čtení katalogu (is_registered_kit naplnění) →
    `await get_kits(session_factory=...)`, promoce → auto_grant se
    session_factory.
  - `cogs/ht3.py`: /seteval a /uneval → `await set_eval`/`unset_eval` se
    session_factory; importy z utils odstraněny.
  - `cogs/kits.py`: /addkit, /removekit, /sendqpanel kit-read, /kits →
    `await add_kit/remove_kit/get_kits` se session_factory; autocomplete
    ze services.kit_catalog (client-driven session_factory).
  - Zbývající JSON čtenáři katalogu/evalů (cogs/views.py QueueView start
    has_eval :525 — eval eligibility joinu, cogs/_shared.py kit_display_map,
    sync/edituser) se wireují v todo #10.
- **Autority-scan**: lines posunuty (roles 112→115 member.edit; results
  444/446→453/455 add_roles/remove_roles) → AUTHORIZED_MUTATION_SITES
  aktualizováno. Došlo k rewire v production cogech, takže scan znovu
  nehlásí nové neočekávané mutace.
- **Testy**: tests/test_services_kits_evals_roles_db.py (22 testů) — DB:
  kity (defaults na prázdné tabulce, add/list order, case-insensitive
  duplicita, remove+readd reaktivace, canonical, remove neaktivního=False),
  evaly (neznámý hráč/kit → False, grant/revoke lifecycle, double grant
  idempotence, double revoke), kit_roles (prázdná mapa, set/get/unset,
  replace, neznámý kit/tier, prázdné vstupy, all_maps lowercase klíče);
  JSON parity: kity add/remove (defaults+nový, remove→defaults),
  evals roundtrip a shape {kit: {ign: ts}}, kit_roles string role_id.
  Starší cog testy: test_roles (import KIT_ROLES_FILE přenesen do
  services.kit_roles; TierRoleGrant import vyhozen), test_topresult a
  test_phase_e_outage (get_kits mock — cog teď katalog čte přes await).
- Full suite: **900 passed** (878 + 22), ruff clean na všech dotčených.

## 8i. Todo #10a — kit_display_map async + services/cooldowns.py + ht3 /cooldown (hotovo 2026-09-25)

- **cogs/_shared.py**: `kit_display_map` je teď `async def kit_display_map(
  *, session_factory=None)` a čte katalog přes `services.kit_catalog.get_kits`
  (dual-mode); utils import odstraněn. Call sites: `cogs/sync.py`
  (`_gather_checkweb` včetně 2 view call sites přes `interaction.client`,
  cog call sites přes `self.bot`) a `cogs/edituser.py` `_role_context`
  (s `await`).
- **services/cooldowns.py** (nový): `get_cooldowns(uid, *, session_factory=None,
  waitlist_cooldown_ms=None) -> {waitlist_ms: int|None, ht3: {kit_name: ms}}`.
  DB: PlayerRepository → CooldownRepository (WAITLIST kit_id=None,
  HT3 per kit, jen aktivní > now), display name přes KitRepository.
  JSON: cooldowns.json (poslední test, window = waitlist_cooldown_ms ??
  PLAYER_COOLDOWN_MS) + ht3_cooldowns.json ({uid: {kit: expires}}) — parity
  beze změny. Voláno z cogs/ht3.py `/cooldown` (nahrazuje legacy
  cooldown_remaining čtení).
- **cogs/ht3.py**: odstraněny nepoužité importy (time, load_data,
  redundantní config_store/evals bloky).
- **Phase E kontrakty**: linie se posunuly o +1..+2 (multi-line call sites)
  → AUTHORIZED_MUTATION_SITES (cogs._shared 96/98/161/163 → 97/99/162/164,
  cogs.edituser 1207→1209, cogs.sync 881→883) a SAFE_TIER_READER_FLOWS
  (cogs.edituser 1171→1173; cogs.sync 227/999/1081/1414/1550/2006 →
  229/1001/1088/1426/1562/2027) aktualizovány datově (scan → diff).
- **Testy**: tests/test_services_cooldowns_db.py (8 testů) — DB: neznámý
  hráč → prázdno, waitlist remaining, expired → None, HT3 keyed by kit
  name, HT3 expired excluded; JSON: waitlist z last-test ts, HT3 remaining
  (range assert kvůli time driftu), neznámý hráč. Test sync/edituser:
  `_interaction` helperu nastaven `client.db_session_factory = None`
  (jinak MagicMock auto-generuje pravdivý factory → DB cesta v single-mode
  testu).
- Full suite: **908 passed** (900 + 8), ruff clean na dotčených.

## 8j. Todo #10b — cogs/queues.py kompletní rewire + queue-scoped testeri (hotovo 2026-09-25)

- **Nový model `QueueTester`** (db/models/ops.py): queue-scoped testeri
  (opener z /openq + členové /queue joinasqueue) = F10 gate pro /closeq a
  /joinasqueue v DB režimu. `id` PK, `queue_id` FK queues.id, `player_id` FK
  players.id, `joined_at`; unikátní index `uq_queue_tester_player`. Migrace
  `d5e6f7a8b9c0_add_queue_testers.py` (down_revision c4d5e6f7a8b9).
- **QueueTesterRepository** (db/repositories/queues.py): add/find/list/
  is_member/remove; opener = nejstarší joined_at (takeover při leaveq).
- **services/queue_service.py — nové dual-mode funkce** (JSON režim
  zachovává původní soubory a tvary beze změny):
  - `open_queue(kit_key, name, opener_uid, opener_ign, *, session_factory)`
    → (ok|exists|unknown_kit|identity_conflict, qdata). DB: transakce +
    claim opener identity do Player + QueueTester; IntegrityError retry.
  - `close_queue` (DB: active→closed, čekající→LEFT removed_reason
    queue_closed, vrací panel; JSON: původní 3 soubory).
  - `queue_state` / `active_queues` / `list_queue_entries` /
    `queue_snapshot` / `peek_first_player` / `panel_message_id` /
    `set_queue_panel` (DB: panel ids na Queue řádku).
  - `join_queue_tester(kit_key, uid, ign)` / `leave_queue_tester` —
    duplicita/not_listed/closed; DB claim identity při prvním vstupu.
  - `register_global_tester` (DB: TesterRepository.grant, no-op při
    existenci; JSON: testers.json).
  - `removeq` (DB: waiting→PULLED, vrací entry; JSON: queue.json filtr).
  - `skip_player` — **exaktní legacy sémantika ověřená proti
    `git show HEAD:cogs/queues.py`**: pullnutý hráč → requeued=True,
    moved=False; hráč ve frontě (nepullnutý) → moved=True, requeued=FALSE a
    DB **pozici nemění** (legacy při len==len záznam nezapisoval).
- **cogs/queues.py**: odstraněny VŠECHNY inline JSON transakce a
  load_data/save_data volání (0 výskytů) — vše přes služby se
  `session_factory=getattr(self.bot, "db_session_factory", None)`.
  `get_queue_channel_id` → async config_store verze.
- **cogs/results.py**: odstraněna produkční díra — `leave_queue` (:353) a
  `remove_pulled_player` (:362) nyní předávají `session_factory` (předtím
  JSON cesta i v DB režimu).
- **panel.py** `update_panel(..., *, session_factory)`: DB režim čte stav
  fronty ze služeb (queue_state + list_queue_entries + panel_message_id),
  JSON režim původní soubory. Call sites v queues.py/results.py předávají
  sf; views.py zůstává (todo #10c).
- **Phase E kontrakty**: AUTHORIZED_MUTATION_SITES (cogs.results 453/455 →
  455/457, +1..+2 z nových sf řádků) aktualizováno datově (scan → diff).
- **Testy**: tests/test_services_queue_lifecycle_db.py (9 DB testů: open/
  exists/unknown_kit, state+close, join/leave tester + takeover, global
  tester, list/snapshot/peek, removeq status PULLED, skip pulled sémantika
  moved/requeued, panel round-trip) + tests/test_queue_lifecycle_json.py
  (9 JSON parity testů se storage.DATA_DIR izolací, včetně legacy skip
  no-op pro nepullnuté hráče a makení entry divů). Test harnessy
  /result-and-outage (MagicMock bot) dostaly `db_session_factory = None`
  (JSON režim) — viz test_sync helper konvence.
- Full suite: **926 passed** (908 + 18), ruff clean, alembic head
  `d5e6f7a8b9c0`.

## 8k. Todo #10c — views.py kompletní rewire + HT3PanelView kits z katalogu (hotovo 2026-09-26)

- **services/tickets.py — nová `async def player_tier(ign, kit, discord_id=None, *, session_factory)`**:
  dual-mode. DB: transakce → `PlayerRepository.resolve` (primárně Discord ID,
  fallback IGN) → `KitRepository.get_by_name` → `MirrorRepository.get_current`
  → `TierDefinitionRepository.get_by_id` → `.code`. JSON: delegace na sync
  `find_player_tier` (players.json, legacy — zůstává kvůli testům).
- **views.py — všechny JSON čtení odstraněny (0 výskytů `load_data`)**, nahrazeno
  dual-mode službami přes nový helper `def _session_factory(interaction)`:
  `getattr(getattr(interaction, "client", None), "db_session_factory", None)`
  (None v JSON režimu/testech — MagicMock trap ošetřen v test helperu).
  - `JoinModal.on_submit`: `join_queue(..., session_factory=sf)` +
    `update_panel(guild, kit, session_factory=sf)`.
  - `QueueView.on_join`: `queue_state` (closed fronta) → `get_cooldowns`
    (waitlist cooldown) → `list_queue_entries` (duplicita) — vše se sf.
  - `QueueView.on_leave`: `leave_queue(uid, kit, session_factory=sf)` +
    `update_panel(sf)`.
  - `QueueView.on_pull/on_pull_channel`: `list_queue_entries(non-empty)` →
    `pop_for_kit(sf)` → `queue_state(kit name)` → `grant_pull_access(sf)`.
  - `grant_pull_access(..., *, session_factory)`: `save_pulled_player(sf)` +
    `update_panel(sf)`. `PullChannelSelectView.on_select`: `remove_by_player_id(sf)`.
  - `HT3PanelView.__init__(kits=None)`: kits z katalogu (cog/bot je předává),
    fallback `get_kits() or DEFAULT_KITS` (JSON/testy). `on_select`:
    `get_cooldowns(uid, sf)["ht3"]` (remaining-ms místo `load_data` + now).
  - `HT3Modal.on_submit`: `player_tier` (async, sf), `has_eval` z
    services.evals (async, sf), `find_open_ticket`/`create_ticket`/
    `set_panel_message`/`log_ticket_event` — vše se sf.
  - `HTTicketView`: `_active_ticket` → `get_ticket(sf)`; claim/unclaim/close/
    reopen + log_ticket_event — vše se sf.
- **cogs/kits.py `_refresh_ht3_panel`** a **cogs/ht3.py `/ht3panel`** +
  **bot.py startup**: `HT3PanelView(kits=await get_kits(session_factory=...))`
  (DB režim dostane DB kits; JSON režim get_kits(None) → JSON).
- **Phase E kontrakty**: json_compat manifest — `("services.tickets", 139)` →
  `140` (posun +1 z přidaných řádků v tickets.py); authority_scan čistý —
  2 false-positive pg-tier violation z literalů `player_current_tiers`
  v docstringu/komentáři odstraněny přeformulováním („mirror tabulky
  aktuálního tieru").
- **Testy**: tests/test_views_dual_mode.py (20 testů) — `_session_factory`
  JSON/DB/missing-client, JoinModal sf předání + closed/duplicate chování,
  QueueView join pre-checky (closed/cooldown/duplicita/ok) + sf propagace +
  leave sf + pull empty, HT3PanelView kits override + cooldown gate,
  HT3Modal sf propagace do všech 6 služeb (JSON+DB). test_ht_ticket_view.py
  helper: `client.db_session_factory = None` (MagicMock trap).
- Full suite: **946 passed** (926 + 20), ruff clean, alembic head `d5e6f7a8b9c0`.

## 8l. Todo #10d — services/edituser.py + cogs/edituser.py DB režim (hotovo 2026-09-26)

- **Krok B — nový `services/player_export.py`** (canonical JSON tvar hráče z DB,
  jediný zdroj pro web canonical + F5 exporter):
  - `build_player_shape(session, player)` → `{"username", "discordId" (jen
    non-None), "modes": {Kit.name: tier_code}, "history": {Kit.name:
    [{"date": "%d.%m.%Y", "tier": code}]}}`; `modes` z mirror JOIN
    kits/tier_definitions (klíče = DISPLAY názvy kitů jako legacy players.json),
    `history` vzestupně (poslední = nejnovější — cog zobrazuje
    `entries[-8:][::-1]`), limit 500 (`DEFAULT_HISTORY_LIMIT`).
  - `export_players(session_factory, *, history_limit)` / `iter_export_players`
    — canonical list všech hráčů pro web push (+ základ #11).
- **Krok A — `services/edituser.py`**: `apply_player_edit` + `execute_player_edit`
  mají nový povinný keyword parametr `session_factory` (None → JSON cesta beze
  změny chování; předán → DB cesta = F10):
  - nová `_apply_player_edit_db` — transakce, `PlayerRepository.get_by_discord_id`,
    pole: cooldown (set/clear_queue přes `CooldownRepository` waitlist,
    set/clear_ht3 per kit, chybějící/neznámý kit → error), `discord_id` (digitálně
    15+ číslic, konflikt → identitní error, jinak UPDATE + flush), `ign` (prázdné →
    error, case-insensitivní duplicita → error, jinak UPDATE), `tier` (`_resolve_kit`
    key→name, `normalize_tier_choice`, retired archivace jen přesně stejné hodnoty
    `R{old}` / žádný přepis retired aktuálním — invarianty shodné s JSON cestou,
    zápis přes `MirrorServiceRepository.apply_observation(source="manual",
    actor_id, actor_name, reason="edituser")`).
  - `_resolve_tier`: `get_by_code` → `get_or_create`; `kind="virtual"` pro
    `LT3E` i retired (R-prefix), jinak `"ladder"`.
  - audit v DB režimu POUZE `AuditRepository.append` (action="edituser",
    entity player, details field/kit/oldValue/newValue); `edituser_log.json` se
    v DB režimu nepíše (F10).
  - `execute_player_edit`: při `session_factory` se `player_after` staví z DB
    (`build_player_shape`) a web canonical z `export_players(session_factory)` —
    žádný JSON read (F10).
- **Krok C — `cogs/edituser.py` rewire** (JSON čtení zůstalo jen v `_find_player_sync`
  a v JSON režimu přes `session_factory=None`):
  - helper `_cog_session_factory(cog)` = `getattr(getattr(cog.bot, ...
  "db_session_factory", None))`; async dual-mode `_find_player(player_id, *,
  session_factory)` — DB: `PlayerRepository.get_by_discord_id` +
  `build_player_shape`; JSON: delegace na `_find_player_sync`.
  - `PlayerEditorView` on_discord/on_ign/on_cooldown/on_history,
  `ConfirmEditView._stale_check`/on_cancel, `/edituser` command — přes
  `await _find_player(...)` (command odolný vůči chybějícímu `.bot` v testech).
  - `_cooldown_embed` + `CooldownEditView._preview_lines`/`_embed` +
  `TierKitSelectView._embed`: async + `get_cooldowns` (ms inty → formátování
  `format_duration`; popisky shodné mezi JSON a DB cestou).
  - `on_back` všech sub-viewů (edituser_tier_back / edituser_cd_back /
  edituser_history_back) + `await view._embed()`; `ConfirmEditView.on_confirm`
  předává `session_factory` do `execute_player_edit`.
  - `_role_context`: DB režim roles_map z `get_all_kit_role_maps(session_factory)`,
  JSON režim `load_data(KIT_ROLES_FILE)` (odstraněné nepoužité importy
  cooldown_snapshot/cooldown_remaining/ht3_cooldown_remaining).
- **Phase E kontrakty**: json_compat manifest — cogs.edituser readers
  `95/314/369/635/1173` → `97/342/397/670/1218` (posun z rewiringu), services.edituser
  `804/833` → `1175` (json_ui_read, player_after JSON režim) + `1209`
  (web_export_canonical, web push JSON režim); authority_scan authorized mutation
  site `cogs.edituser 1209` → `1254`; false-positive pg-tier violation z literálu
  `player_current_tiers` v docstringu player_export.py odstraněn přeformulováním.
- **Testy**: tests/test_edituser_db.py (28 DB testů, embedded PG): ign/discord_id
  změna+audit+idempotence+konflikt, cooldown set/clear/chybějící kit/neznámá akce,
  tier change (mirror manual → history ≥2 + audit), retired archivace RLT3 povolena
  / přes jiný aktuální error / current-over-retired error, neznámý kit, not_found
  (neexistující i ne-numerické ID), execute_player_edit player_after z DB (role
  plán vidí nový HT3) + web canonical z export_players + unchanged bez rolí;
  test_edituser_db seed helper: `source="manual"` (seed není povolené source).
- JSON režim beze změny chování (tests/test_edituser.py + views = 99 passed).
- Full suite: **974 passed** (946 + 28), ruff clean, alembic head `d5e6f7a8b9c0`.

## 8m. Todo #10e — /sync discord + bot.py startup (hotovo 2026-09-26)

- **`/sync discord` (cogs/sync.py `_run_discord`) je observe-only a už plně na DB** —
  žádné JSON čtení: `DiscordSyncService().sync_guild(session_factory, members, ...)`
  (mirror do PostgreSQL), DB failure = tvrdá chyba bez jakékoliv zmény rolí.
  Ověřeno staticky (rozsah #10e zahrnoval kontrolu, ne rewire).
- **`bot.py` on_ready — startup re-registrace persistentních view**:
  - `queue_messages.json` (QueueView panely) → **`QueueRepository().list_active` +
    `KitRepository().list`** (`Queue.panel_message_id`; §6 mapa „panel obnova čte
    Queue ne queue_messages.json");
  - `ht_tickets.json` (HTTicketView tlačítka) → **`TicketRepository().list_open`
    (`Ticket.panel_message_id`)**, chybějící panelMessageId → warning (stejná
    logika jako legacy);
  - oba bloky pod `if self.db_session_factory is not None:` — bez DB se přeskočí,
    **žádný JSON fallback** (F10; vzor tournaments bloku); HT3 panel registrace
    zůstává mimo guard (get_ht3_panel/get_kits jsou dual-mode služby).
  - odstraněný unused import `storage.load_data` (bot.py už JSON nečte).
- **Zbývající JSON reads v cogs/sync.py zůstávají klasifikované SAFE** (json_compat
  manifest bez posunu): `:203`/`:228-229` `/check check-all` diagnostika,
  `:1001`/`:2027` web canonical export, `:1088`/`:1426` operátorské importy
  (importdiscord – Discord → players.json → web; checkweb apply – manuální potvrzený
  repair, záměrně mimo DB protože je to legacy operátorský/repair tok),
  `:1562-1563` 3-cestná analýza (Discord × DB × web) preview auditu.
- **Testy**: plný běh **974 passed** (beze změny počtu — rewire startupu nemá
  dedicovaný test; on_ready je pokryt staticky + import test `python -c "import bot"`),
  ruff check clean, `bot.py` pyright: jen pre-existing env chyby (discord).

## 8n. Todo #11 — players.json export-only/deprecated + deterministický exporter (hotovo 2026-09-26)

- **DEPRECATED marker (F4)**:
  - `services/player_export.py` modul docstring začíná `DEPRECATED (F4)` — players.json
    je export-only soubor; žádná produkční funkce ho nesmí číst pro stanovení aktuálních
    tierů/rolí/identity/promočního stavu; jediné zapisovače = tento modul a legacy
    operátorské repair toky (importdiscord / checkweb apply).
  - `cogs/_shared.save_players` docstring: už JEN operátorské repair toky a JSON režim
    services (tooling/migrace).
- **Deterministický F5 exportér — `services/player_export.write_players_export`**:
  `export_players` (ORDER BY id = stabilní pořadí) → atomický souborový zápis
  (`_write_export_file`, indent=2, ensure_ascii=False). **Nepoužívá `storage.save_data`**
  — ten s aktivním PostgreSQL backendem píše do JSONB blobu, ne na disk; export musí
  mířit vždy do players.json (F4).
- **`/linkdiscord` export přepojen z ruční JSON mutace na `write_players_export`**:
  mizí `load_data(players.json)` + `claim_ign` + `save_data` — canonical export jde
  z DB jediným exportérem. Nepoužité importy `claim_ign` a `save_data` odstraněny;
  `PlayerIdentityConflict` zůstává importovaný (používá jej `/ignore` a `PlayerEditView`
  konfliktní větev). Chybová hláška „export přeskočen (conflict)" sloučena do jediné
  „export selhal – DB claim zůstává".
- **Phase E kontrakty** (line shifts z odstraněných řádků): json_compat manifest
  cogs.edituser `97/342/397/670/1218` → `96/341/396/669/1214`; authority_scan
  mutation site `1254` → `1247`.
- **Testy**: test_edituser_db.py nový `test_db_write_players_export_deterministic`
  (export do tmp DATA_DIR; soubor == `export_players` výstup; stabilní pořadí
  mendu__/bob_; canonical tvar); TestLinkDiscordCog oba export testy přepsány na
  mock `services.player_export.write_players_export` (úspěch → „aktualizováno",
  chyba → „export selhal", players.json se nemění).
- **Status**: skupina 144 passed (edituser DB + JSON + views + phase E); full suite po
  #11 `975 passed, 1 warning`.

## 8o. Todo #12 (F6–F9) — GitHub/web export z DB + rollback auditu (hotovo 2026-09-26)

- **F6 — GitHub je export-only transport, ne zdroj pravdy**: `services/github_sync.py`
  (`fetch_players` = čtení pro diff analýzu, `push_players` = zápis kanonického
  exportu) + `services/websync.sync_website` zapisují na web **výhradně z canonical
  databáze**. `services/phase_d/legacy_writers.py` klasifikuje `github_sync.py` jako
  `export_only`.
- **F9 — web export už nečte players.json jako primární zdroj**: nový helper
  `cogs/sync._canonical_for_export(session_factory)` (modulová funkce) volá
  `export_players(session_factory)`; JSON je pouze fallback. Zapojen na oba
  produkční body: `/sync web` náhled (`Sync._run_web`) i potvrzení
  (`SyncWebConfirmView.confirm` → `sync_website`).
  - **Hlasitý fallback, ne tichý**: při výpadku DB `log.exception` a pokračování
    z `data/players.json`. Export kanál nesmí selhat při DB výpadku — to vyžadují
    existující outage testy (`test_phase_e_outage.SyncWebExportDbOutageTests`,
    `test_phase_e_github_export.GitHubExportDownstreamOnlyTests`). JSON zde nikdy
    není zdroj pravdy pro stav, jen zdroj exportu, a nikam se nepropírá do
    autoritativních cest (F4/F10 v pořádku).
- **F7 — `playersync_log.json` zůstává legacy E/D, ale už se nepíše**:
  `db/services/mirror_sync.py` (`DiscordSyncService.sync_guild`) je **observe-only** —
  zapisuje `SyncRun`/`SyncAction` do PG a nikdy nemutuje Discord role. Produkční
  writer `log_playersync_event` proto nemá žádného volajícího. `/sync discord-rollback`
  (`cogs/sync.py` + `services/role_sync.py`) čte `playersync_log.json` jen jako
  **historický** zdroj pro rollback starších mutujících synců (disaster recovery) —
  to je výslovně zachovávaná funkce, ne porušení: JSON se z něj nikdy nečte pro
  aktuální tier/roli/identitu/promoci.
  - **Nové observe-only sync rollback nepotřebují** — žádná role se nezměnila, takže
    není co invertovat. Promoce (jediný legitimní Discord mutátor) jde
    Discord → confirm → PG + outbox a je idempotentní; outbox zápis neprovádí žádnou
    Discord mutaci.
  - Soubor se **nemazal** (F12): zůstává jako ověřená záloha pro Phase H review.
- **F8 — promotion kanál**: `db/services/promotion.py`
  (`enqueue_promotion_wedge` / `commit_promotion_with_wedge`) potvrzuje roli na
  Discordu **před** zápisem do PG a ukládá outbox event; volají ho až po úspěšné
  Discord mutaci `cogs/results.py:495` a `cogs/topresult.py:413`. Ani
  `db/services/promotion.py`, ani `db/services/outbox_consumer.py` neobsahují žádnou
  Discord mutaci (`add_roles`/`remove_roles`/`edit`) — outbox je čistě PG-side a
  nikdy nemění role (F8/F4 OK).
- **Phase E kontrakty** (line shifts): json_compat `cogs.sync` `229/1001/1088/1426/
  1562/2027` → `238/246/1106/1444/1580` (helper `web_export_canonical` +
  `discord_observe_or_analysis`); authority_scan mutation site `883` → `900`.
- **Testy**: sync + websync + json_compat + authority_scan + github_export + outage
  `115 passed` (včetně dvou DB-down export testů, které byly předtím červené).

## 8p. Todo #12 (F10–F15) — audit, klasifikace, trojí verifikace, uzavření (hotovo 2026-09-26)

- **F10 — žádný DB režim nepotřebuje JSON pro korektnost**: `json_compat.SAFE_TIER_READER_FLOWS`
  pokrývá **13/13** read sites `players.json` (0 nezařazených). Zbývající čtení jsou
  buď legacy JSON režim (`session_factory=None`: `cogs.edituser` json_ui_read,
  `services.tickets:140`, `services.edituser:1175`), nebo klasifikované
  non-canonical cesty (`discord_observe_or_analysis`, `web_export_canonical`).
  Nový `_canonical_for_export` je **jediný** most mezi DB a web pushem — žádný
  druhý JSON-first export kanál neexistuje.
- **F11 — jediný deterministický exportér ve všech DB cestách**:
  - `/linkdiscord` → `write_players_export` (soubor, F4/F5)
  - `/sync web` (náhled + confirm) i alias `/websync` → `_canonical_for_export` →
    `export_players`
  - `/edituser` tier/ign/discord_id push → `export_players` (`services/edituser.py:1203`)
  - `/result` a `/topresult` **web nepropagují** (odkazují na `/websync`), žádná
    JSON cesta
  - `importdiscord` / `checkweb apply` jsou záměrně **JSON-repair** toky (E/D):
    jejich canonical vzniká z JSON + Discord importu, takže exporter by zrušil
    jejich sémantiku („tier := Discord tier", §5.4). Klasifikace
    `discord_observe_or_analysis` zůstává.
- **F12 — klasifikace beze změny, žádné mazání**: `build_writers_report('.')` =
  **17 writer sites, 0 unclassified** (`export_only` 2, `legacy_operational_record`
  10, `storage_layer_atomic_writer` 3, `migration_tooling` 1, `audit_tooling` 1).
  Doplněno 5 chybějících klasifikací (`services/config_store.py`,
  `services/kit_roles.py`, `services/queue_service.py` = legacy JSON větev dual-mode
  služeb; `services/player_export.py` = export_only; `services/phase_e/json_compat.py`
  = audit_tooling, docstring false-hit). `identity_claim_dualwrite` = 0, protože
  dualwrite zápis v `/linkdiscord` byl nahrazen explicitním `write_players_export`
  (#11) — zastaralý manifest entry `("cogs/edituser.py", 1136)` zůstává jako historie
  a už se neuplatní (žádný `save_data` v tom souboru).
  **Smazáno 0 souborů** (Phase H cleanup): `playersync_log.json`,
  `playersync_rollback_log.json`, `websync_log.json`, `checkweb_log.json`,
  `datacheck_log.json`, `edituser_log.json` = append-only audit (E/D),
  `storage.py`/`services/store.py` = D tooling, `migrate_json_to_postgres.py` +
  `migrations/` + `backups/` = disaster recovery.
- **F13 — autoritativní směry čisté**: `build_authority_report('.')` →
  `mutation_violations: []`, `pg_tier_violations: []`, `json_to_discord: []`,
  `json_to_pg: []`, `github_export_only: true`,
  `pg_to_json_to_discord_path: false`; conclusion „authority directions hold".
  Povolené Discord mutace jsou jen `cogs/_shared.apply_role_actions` (explicitní
  promotion/edituser), `cogs/results.py` (výsledek turnaje), `cogs/roles.py`
  (manuální správa rolí) — žádný z nich nečte JSON pro tier.
- **F14 — trojí verifikace**: `ruff check .` **All checks passed** (opraveno 20
  chyb z Phase A–E práce: 13 auto-fix unused importů, 3 re-exporty přidány do
  `__all__`, 3 unused locals v testech, 1 unused import v `tests/test_sync.py`;
  na HEAD byly 0). Následující tři po sobě jdoucí full běhy:
  **975 passed** (38.86s / 39.20s / 36.82s), 6 warnings, 0 failures.
  - Transparentně: **jeden** mezilehlý běh (před ruff opravou) skončil `1 failed`;
    identita testu nebyla zachycena a následných ~10 běhů (včetně 6 zelených
    po sobě) prošlo. Nepovažuji to za vyřešené — je to netestovaný flaky kandidát
    pro Phase H.
- **F15 — uzavření Phase F**: níže 10 finálních výstupů. **STOP** — Phase G
  ani finální garbage collection se nespouští.

## 9. Phase F — finálních 10 výstupů

1. **PostgreSQL je jediná provozní perzistence** pro hráče, queue, tickety,
   results, evaluace, kity, kit role, cooldowns, turnaje, testery i bot config;
   dual-mode JSON větev zůstává pouze pro běh bez `DATABASE_URL`.
2. **Discord je jediná autorita aktuálního tieru** — mirror sync je observe-only
   a zapisuje `SyncRun`/`SyncAction`; jediné Discord mutace jsou explicitní
   promotion, `/edituser` a manuální správa rolí.
3. **Promoce jde Discord → confirm → PostgreSQL** (+ outbox, který nikdy nemutuje
   role); DB failure nikdy nevyvolá Discord rollback.
4. **`players.json` je export-only** a má jediný deterministický exportér
   (`export_players` → `write_players_export`, `ORDER BY id`, atomický zápis);
   žádná autoritativní funkce ho nečte.
5. **GitHub je export-only transport** (`push_players`) + read pro diff analýzu
   (`fetch_players`); nikdy se nečte zpět do DB ani Discordu.
6. **Web push běží z DB** ve všech DB režimech (`/sync web`, `/websync`,
   `/edituser`) přes `_canonical_for_export`; při výpadku DB běží hlasitý
   (`log.exception`) fallback z `players.json`, aby export kanál nezhasl.
7. **Audit čtení je úplný**: 13/13 read sites `players.json` klasifikovaných,
   17/17 JSON writerů klasifikovaných, 0 unclassified v obou auditech.
8. **Autoritativní směry ověřeny skenerem**: žádný `JSON → tier/role`,
   `PG tier → Discord`, `GitHub → tier`, žádná `PG → JSON` write cesta z `db/`.
9. **Disaster recovery zachována**: `/sync discord-rollback` čte historický
   `playersync_log.json` (poslední mutující syncy), audit logy, `storage.py`,
   `migrate_json_to_postgres.py`, `migrations/` a `backups/` zůstávají; smazáno 0
   souborů.
10. **Ověření**: 975 testů × 3 po sobě zelené běhy + `ruff check .` čistý
    (baseline na HEAD byl rovněž čistý, takže žádný regresní lint).
