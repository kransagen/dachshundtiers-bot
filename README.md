# 🐕 DACHSHUNDTIERS – Discord Bot (Python)

Discord bot pro **DACHSHUNDTIERS** (tier testy, fronty, HT3+ tickety a
turnaje). Port původního JS bota
([`kransagen/DACHSHUNDTIERSQBOT`](https://github.com/kransagen/DACHSHUNDTIERSQBOT))
na **discord.py**.

## Architektura

- **Discord je jediná autorita aktuálního tieru hráče.** Role na Discordu se
  mění jen přes autorizované příkazy (`/result`, `/topresult`, `/edituser`,
  `/sync discord-rollback`) – nikdy automaticky z databáze ani z JSONu.
- **PostgreSQL je povinný.** `DATABASE_URL` musí být nastavené a schéma
  zmigrované (`alembic upgrade head`), jinak start bota tvrdě selže. Žádný
  JSON-only deployment mode, žádný tichý fallback.
- `players.json` slouží jen jako **export/kompatibilita**
  (kopie pro GitHub/web) – nikdy se nečte zpět jako zdroj aktuálního tieru.
  Pokud export z PostgreSQL selže, `/sync web` to nahlásí nahlas jako
  „LEGACY JSON FALLBACK“, nikdy potichu.
- **Normální synchronizace je read-only.** `/sync discord` jen čte Discord a
  zapisuje do PostgreSQL zrcadla; nikdy neopačně. Jediné příkazy, které smí
  měnit Discord role, jsou vypsané výše.

Podrobnosti a auditní důkaz viz `docs/PHASE_G0_FINAL_REPORT.md` a
`docs/PHASE_H_FINAL_REPORT.md`.

### Pravidla identity a historie

- **Retired tier** – prefix `R` v `players.json` (např. `RLT2`) je archivovaná
  historie: nikdy se nemaže ani nepřepisuje, synchronizace ho jen reportuje.
- **`discordId`** je permanentní identita hráče (páruje se přes změnu IGN).
  Konflikt (stejný IGN, jiné `discordId`) se nikdy neřeší automaticky.
- **`/removeplayertiers`** odebírá jen aktuální tiery (`modes`); historie
  zůstává.

## Funkce

### 🎯 Fronty na tier testy
| Příkaz | Popis |
|---|---|
| `/openq kit` | Otevře frontu pro kit – vyčistí kanál kitu a pošle živý panel (embed + tlačítka) s `@everyone`. |
| `/closeq kit` | Zavře frontu (jen bez čekajících testerů). |
| `/addqchannel kit [kanal]` *(admin)* | Nastaví kanál panelu fronty pro kit. |
| `/queue join ign kit` | Přidá se do fronty (kontrola 4denního cooldownu). |
| `/queue joinastester` | Zaregistruje testera jako globálně aktivního. |
| `/queue joinasqueue kit` | Tester se přidá do fronty jako další tester. |
| `/queue leaveq kit` | Tester opustí frontu. |
| `/queue list` | Přehled front a aktivních testerů. |
| `/queue pull` | Vytáhne prvního hráče z fronty do vybrané roomky. |
| `/removeq hrac` | Ručně odstraní hráče z fronty. |
| `/skip hrac` | Skipne AFK hráče – vyhodí ho z roomky i z fronty. |
| `/mktesterroom [hrac] [kategorie]` | Soukromá tester roomka pro pullnutí hráče. |

Tlačítka panelu: **Join Queue** (modál s Minecraft IGN), **Leave Queue**,
**Pull Player ⚔️** (výběr roomky). Panel se aktualizuje automaticky.

> `/openq` vždy posílá panel do kanálu určeného pro daný kit (priorita:
> `/addqchannel` → env `QUEUE_CHANNELS_JSON` → výchozí kanál), ne do kanálu,
> kde byl příkaz zadán.

### 📝 Výsledky tier testů
| Příkaz | Popis |
|---|---|
| `/result hrac ign kit tier score outcome [add_role] [remove_role]` | Zápis výsledku. Tier: `LT5/HT5/LT4/HT4/LT3/LT3+eval`. Nový kit se zaregistruje automaticky; role tieru se rozdá dle `/setkitrole`. |
| `/testerstats tester` | Portfolio testera (počty testů, oblíbený kit, tier, čas). |
| `/testersstats current\|all` | Žebříček testerů (měsíc / vše). |
| `/addtest tester amount month` *(admin)* | Ruční přidání historických testů. |
| `/removetest user amount` *(admin)* | Odečtení testů. |
| `/removeplayertiers ign` *(admin)* | Odebere hráči všechny aktuální tiery (historie zůstává). |
| `/setkitrole kit tier role` *(admin)* | Namapuje roli tieru pro kit. |
| `/unsetkitrole kit tier` *(admin)* | Zruší mapování role tieru. |
| `/kitrole` | Vypíše mapování kit → role tieru. |
| `/topresult hrac ign kit fight_tier outcome score opponent tier_status [bridge]` *(tester)* | HT Fight výsledek (**ne žebříček**) – veřejná zpráva do `TOP_RESULT_CHANNEL_ID` s pingem `TOP_RESULT_ROLE_ID`. Výhra povyšuje hráče (volitelně přeskočením přes `bridge`), prohra tier nemění. V HT Fight ticketu zavře ticket a nastaví cooldown. |

`/result` navíc: nastaví 4denní cooldown, odebere hráče z fronty/roomky,
uloží tier + historii, započítá statistiky testerovi a pošle výsledek do
`RESULT_CHANNEL_UPPER` (HT3+) nebo `RESULT_CHANNEL_LOWER` (LT3 a níž).
Web se z `/result` neaktualizuje přímo – to dělá výhradně `/sync web`.

### 🔄 Synchronizace (`/sync`)

Jeden centrální příkaz pro admin synchronizační operace. Business logika žije
ve službách (`services/checkweb`, `services/role_sync`, `services/websync`,
`services/datacheck`); cog je jen orchestrace. Původní `/checkweb`,
`/playersync`, `/websync`, `/datacheck` fungují dál jako deprecated aliasy.

| Příkaz | Co dělá | Mění Discord/DB? |
|---|---|---|
| `/sync check [area]` *(admin)* | Read-only diagnostika: Discord role × DB × web + integrita dat. Agreguje OK/WARNING/CONFLICT/ERROR, filtr `area`. | Nic – jen report. |
| `/sync discord mode:preview\|apply` *(admin)* | Discord tier role vs. kanonická DB. `apply` po potvrzení aplikuje rozdíly (role add/remove). | Jen Discord role (nikdy DB z Discordu nepřepisuje opačně – to už tak nefunguje, DB se aktualizuje ze čtení Discordu). |
| `/sync discord-rollback [mode] [target_ts]` *(admin)* | Bezpečná inverze posledního `/sync discord apply` – vrátí Discord do stavu před syncem, výhradně přes `memberId`/`roleId` z auditu. Kontroluje, jestli hráč nebyl mezitím znovu povýšen. Výchozí `mode:preview` (dry run). | Jen Discord role, po explicitním potvrzení. |
| `/sync importdiscord mode:preview\|apply` *(admin)* | Náhled jednoznačných rozdílů Discord × DB – **nic nezapisuje** (ani DB, ani web); nasměruje k `/sync discord` a `/sync web`. | Nic. |
| `/sync web mode:preview\|apply` *(admin)* | Export kanonických dat na GitHub/web. `apply` po potvrzení nahradí web. Selhání GitHubu se nikdy nehlásí jako úspěch. | Jen GitHub soubor. |
| `/sync data` *(admin)* | Kontrola integrity: duplicity, neplatné tiery, konfliktní role, osamocené tickety. Opravy jen po potvrzení tlačítkem. | Jen po potvrzení, jen neautoritativní opravy. |

Audit: `data/playersync_log.json` (discord), `data/playersync_rollback_log.json`
(rollback), `data/websync_log.json` (web), `data/checkweb_log.json` +
`data/datacheck_log.json` (check/data).

### 💸 HT3+ tickety
| Příkaz | Popis |
|---|---|
| `/sendht3` | Pošle panel „Žádost o TierTest“ s výběrem kitu. |
| `/cooldown hrac` | Zobrazí HT3+ cooldowny hráče. |
| `/add hrac` | Přidá hráče do aktuálního HT ticketu/roomky. |
| `/seteval ign kit` *(tester)* | Nastaví „LT3 + eval“ – hráč smí otevírat HT3+ tickety. |
| `/uneval ign kit` *(tester)* | Odebere „LT3 + eval“. |

Tok: výběr kitu → kontrola 7denního cooldownu → modál (IGN + cílový tier) →
ticket roomka → tlačítko **🔒 Close Ticket** (nastaví cooldown, smaže roomku).

- **Bez bypassu cooldownu:** znovuotevření ticketu se stejnou kontrolou
  cooldownu jako nový ticket.
- **Limit tieru:** ticket na tier vyšší než hráčův aktuální (žebříček
  `LT5 < HT5 < LT4 < HT4 < LT3 < LT3+eval < HT3 < LT2 < HT2 < LT1 < HT1`) se
  zablokuje s vysvětlením. Retest na aktuálním tieru projde.
- **Brána „Bez evalu“:** HT3+ ticket otevřou jen hráči se statusem
  „LT3 + eval“ nebo tierem HT3+.
- Kategorie ticket roomky je nastavitelná per kit (`HT3_TICKET_CATEGORIES_JSON`).

### 🏆 Turnaje
| Příkaz | Popis |
|---|---|
| `/createturnaj role skupiny hodiny kit tier` | Vytvoří turnaj (kategorie + přihlašovací kanál). |
| `/turnajresult kit hrac z_tieru na_tier` | Pošle výsledek do `TOURNAMENT_RESULT_CHANNEL_ID` (ping jen `TOP_RESULT_ROLE_ID`). |
| `/deleteturnaj kit` | Smaže turnaj i jeho kanály. |

Po deadlinu se přihlašování ukončí, hráči se rozdělí do skupin (skupinové
roomky) a vylosují se 1v1 zápasy.

### 🗂️ Správa kitů
| Příkaz | Popis |
|---|---|
| `/addkit kit` *(admin)* | Přidá nový kit. |
| `/removekit kit` *(admin)* | Odebere kit. |
| `/kits` | Vypíše registrované kity. |
| `/verze` | Diagnostika běžící verze a slash příkazů. |
| `/dbstatus` *(admin)* | Dostupnost PostgreSQL a počet záznamů (nikdy nevypisuje host/heslo). |

**Nový kit v praxi:** `/addkit` (nebo automaticky přes `/result`) →
`/addqchannel` → `/openq` → vytvoř role tierů na serveru → namapuj přes
`/setkitrole`.

## Struktura projektu

```
bot.py                # jádro bota (vstup: python bot.py)
main.py               # vstupní bod pro hosting (startup file = main.py)
config.py             # konfigurace (.env) + runtime kanály front
storage.py            # legacy JSON I/O + non-tier operační JSONB tabulka
db/                    # PostgreSQL: modely, repozitáře, služby, migrace
panel.py               # živý waitlist panel
views.py               # tlačítka, select menu, modály
utils.py               # pomocné funkce
cogs/
  queues.py             # fronty
  results.py            # výsledky + statistiky + auto role
  roles.py              # /setkitrole, /unsetkitrole, /kitrole + auto-grant rolí
  sync.py               # /sync (check | discord | web | data) + deprecated aliasy
  _shared.py            # sdílené orchestrace helpery
  topresult.py          # /topresult
  edituser.py           # /edituser
  info.py               # /verze, /dbstatus
  ht3.py                # HT3+ tickety
  tournaments.py        # turnaje
  kits.py               # správa kitů
data/                   # JSON databáze (vytvoří se za běhu; export/legacy v PG režimu)
```

## Instalace a spuštění

```bash
# 1. Závislosti
pip install -r requirements.txt

# 2. Konfigurace (token a volitelně další hodnoty)
cp .env.example .env   # doplň DISCORD_TOKEN
# nebo: export DISCORD_TOKEN=...

# 3. Spuštění lokálně
python bot.py
# nebo (hosting obvykle spouští main.py)
python main.py
```

> `.env` je automaticky načten přes `python-dotenv` (viz `config.py`).

### Nastavení bota na Discord Developer Portalu
- Intents: **Guilds**, **Guild Messages**, **Message Content**.
- Oprávnění: *Manage Channels*, *Manage Roles*, *Send Messages*.
- Role testera stačí, aby obsahovala „tester" v názvu (case-insensitive).

## Konfigurace

| Proměnná | Význam |
|---|---|
| `HT3_PANEL_CHANNEL_ID` | Kanál pro `/sendht3` panel. |
| `HT3_TICKET_CATEGORY_ID` | Výchozí kategorie pro HT3+ ticket roomky. |
| `HT3_TICKET_CATEGORIES_JSON` | Mapa „kit → kategorie" pro HT3+ tickety. |
| `RESULT_CHANNEL_LOWER` / `RESULT_CHANNEL_UPPER` | Výsledkové kanály pro `/result` (LT3 a níž / HT3 a výš). |
| `TOP_RESULT_CHANNEL_ID` / `TOP_RESULT_ROLE_ID` | Kanál a role pro `/topresult`. |
| `QUEUE_CHANNELS_JSON` | Mapa „kit → kanál panelu fronty". |
| `TESTER_ROOM_CATEGORY_ID` | Kategorie pro tester roomky (`0` = bez kategorie). |
| `TOURNAMENT_RESULT_CHANNEL_ID` | Kanál pro `/turnajresult`. |
| `GUILD_ID` | Scope registrace příkazů – nastaveno = jen tato guilda, prázdné = globálně. |
| `TESTER_ROLE_FRAGMENT` | Fragment názvu tester role (default `tester`). |
| `GITHUB_*` | Volitelný export `players.json` na GitHub. |
| `DATABASE_URL` | PostgreSQL připojení, např. `postgresql://user:heslo@host:5432/dachshundtiers`. Bez něj běží legacy JSON-only režim. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Alternativa k `DATABASE_URL` (bot URL sestaví sám). |
| `DB_HOSTADDR` | Volitelné vynucení IPv4 adresy DB (hodí se bez IPv6 trasy). |

> Kanál panelu fronty jde nastavit i za běhu přes `/addqchannel`
> (`data/queue_channels.json`, má přednost před env i defaulty).

## Data

`DATABASE_URL` je **povinné** – bez něj bot start odmítne (žádný JSON-only
deployment mode, žádný tichý fallback). Discord je autorita aktuálního
tieru, PostgreSQL je perzistentní zrcadlo/historie/audit. `players.json`
(a ostatní JSON soubory) zatím zůstávají jako export/kompatibilita a jako
podpůrné úložiště pro některé nekritické subsystémy (cooldowny, tickety,
fronta) – jejich postupný přesun na relační PostgreSQL tabulky probíhá.
Nedostupné/nemigrované PostgreSQL při startu = tvrdá chyba.

Migrace existujících JSON dat do PostgreSQL (nemaže originály):

```bash
DATABASE_URL='postgresql://user:heslo@host:5432/dachshundtiers' \
  python migrate_json_to_postgres.py
```

Skript načítá i `.env` – místo `DATABASE_URL` jde použít samostatné
`DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`. Po migraci nastav
stejnou proměnnou v hostingu, restartuj bota a ověř `/dbstatus`.

Datové soubory (JSON-only režim / export): `players.json` (`modes`, `history`,
volitelně `discordId`), `queue.json`, `active_queues.json`,
`queue_messages.json`, `queue_channels.json`, `testers.json`, `cooldowns.json`,
`testers_stats.json`, `ht3_cooldowns.json`, `tournaments.json`,
`pulled_players.json`, `kits.json`. Kanonická historie výsledků
(`/result` i `/topresult`) je v `ht_results.json` (append-only). Auditní logy
synchronizací: viz sekce `/sync` výše. Server-specific `data/kit_roles.json`
(role dle `/setkitrole`) se necommituje.

**Odolnost proti poškozeným souborům:** čtení vrací default a zaloguje
chybu; zápisy (transakce) čtou ve strict režimu – poškozený JSON soubor se
**nikdy nepřepíše** defaultními daty, operace se bezpečně přeruší. Zápis je
atomický (dočasný soubor + `os.replace`).
