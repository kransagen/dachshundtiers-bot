# 🐕 DACHSHUNDTIERS – Discord Bot (Python)

Pythonový port Discord bota pro **DACHSHUNDTIERS** (tier testy, fronty, HT3+
tickety a turnaje). Původně napsaný v JavaScriptu (discord.js) v repozitáři
[`kransagen/DACHSHUNDTIERSQBOT`](https://github.com/kransagen/DACHSHUNDTIERSQBOT) –
tento repozitář obsahuje stejné funkce postavené na **discord.py**.

## Co je nového (aktuální verze)

- **`/result`** – okamžitá odpověď (defer, jako v originále), **auto-registrace
  nového kitu** (kit se přidá do `kits.json` rovnou ze zápisu výsledku) a
  **automatické rozdávání rolí** (`/setkitrole`): hráč dostane roli nového
  tieru, ostatní tiery stejného kitu se odeberou. Volitelně i ruční
  `add_role` / `remove_role`.
- **`/queue pull`** – vytáhne prvního hráče a **přidá ho do roomky** (výběr
  roomky + práva + uvítací zpráva); stejný tok jako pull tlačítko na panelu.
- **`/mktesterroom`** – soukromá text roomka pro pullování hráčů, zavírá se
  tlačítkem v roomce.
- **`/verze`** – diagnostika: commit běžícího bota, stav `/result` a přehled
  slash příkazů (lokální tree / Discord globální / Discord guild + duplicitní
  jména global ∩ guild – read-only).
- **`/setkitrole` / `/unsetkitrole` / `/kitrole`** – mapa „kit → role tieru"
  v `data/kit_roles.json` (necommituje se).
- **Deterministická synchronizace příkazů (bez duplicit)** – každý start
  synchronizuje slash příkazy do **JEDINÉHO scope**: s `GUILD_ID` jen do guildy
  (produkce), bez něj jen globálně. Při startu se nejdřív bezpečně odstraní
  **obsolete guild příkazy** téhle aplikace z dřívějších nasazení (jen naše
  příkazy, jen v cílové guildu, globální scope se nedotýká) a pak se
  nasyncuje zamýšlený set (`copy_global_to` + `sync(guild=...)`). Sync běží
  **přesně jednou za běh procesu** (reconnect nepřepisuje nic zbytečně),
  scopy se nikdy nemíchají → duplicity global+guild nevznikají.
- **`/sync` – centrální synchronizace (check | discord | web | data)** –
  jeden příkaz pro všechny admin synchronizační operace. Cog je jen
  orchestrace – business logika zůstává v existujících službách
  (`services/checkweb`, `services/role_sync`, `services/websync`,
  `services/datacheck`, `github_sync`). Původní `/checkweb`, `/playersync`,
  `/websync` a `/datacheck` zůstávají jako **funkční deprecated aliasy**,
  které v patičce upozorní „⚠️ Deprecated – použij /sync …".

- **`/sync check [area]` – read-only diagnostika (Discord × DB × web + integrita)** –
  pro každého hráče a kit porovná **Discord tier role × `players.json` × web
  (GitHub)** (statusy: shoda / chybějící role / databáze ≠ Discord / web ≠ DB /
  víc tier rolí = **KONFLIKT** / neznámá role / neznámý hráč / duplicitní hráč)
  a k tomu přičte nálezy integrity dat a rozdílů webu. Nález se agreguje do
  **OK / WARNING / CONFLICT / ERROR** a dá se filtrovat podle oblasti
  (`area`: all / identity / tiers / roles / web / data). **Nic nezapisuje** –
  Discord role už NEJSOU autoritativní zdroj (zdrojem pravdy zůstává hodnocení
  `/result`, role je projekce DB). Web se stahuje jednou; když se nepodaří
  přečíst, websync sekce se přeskočí (žádné falešné nálezy). Audit jde do
  stávajících `data/checkweb_log.json` + `data/datacheck_log.json`. Poškozené
  datové soubory se jen ohlásí a kontrola se bezpečně přeruší (nic se
  nepřepisuje).

- **`/sync discord mode:preview|apply` – synchronizace tier rolí s players.json** –
  porovná Discord tier role (`/setkitrole`) s kanonickou `players.json`
  (chybějící role, špatné role, víc tier rolí, neznámí hráči, neplatné tiery,
  retired tiery v DB – R-prefix je jen report, role se neřeší). **Nikdy neřeší
  konflikty automaticky a DB z Discordu se nikdy nepřepisuje.** `preview`
  ukáže rozdíly (dvojice člen × kit, které sedí, se počítají jako „beze
  změny“), `apply` vyžaduje **explicitní potvrzení** (tlačítko); stav mezi
  náhledem a potvrzením se ověřuje a každá akce (role add/remove) se aplikuje
  zvlášť – chyba (Forbidden) = PARTIAL, nikdy crash. Audit
  v `data/playersync_log.json`.

- **`/sync web mode:preview|apply` – synchronizace webu s kanonickou `players.json`** –
  web (players.json na GitHubu) je jen kopie, jediný zdroj pravdy zůstává
  lokální kanonická DB. `preview` stáhne web a detekuje chybějící hráče,
  špatné tiery, zastaralá data, duplicitní hráče a neplatné záznamy – **nic
  neposílá**. `apply` po **explicitním potvrzení** (tlačítko) nahradí
  players.json na webu kanonickou DB; čtení i zápis se opakují s retry.
  Selhání GitHubu **nikdy není hlášeno jako úspěch**; bez `GITHUB_TOKEN` se
  web nedá číst ani zapisovat; **prázdná DB se na web nikdy neposílá**. Audit
  v `data/websync_log.json` (timestamp, počet záznamů, úspěch/selhání, chyby).

- **`/topresult` – HT Fight výsledek (specializovaná verze `/result`)** – **není
  to žebříček**: vytvoří veřejný výsledek HT Fightu přesně ve stylu serveru do
  vyhrazeného kanálu (`TOP_RESULT_CHANNEL_ID`) a zapinguje nakonfigurovanou
  roli (`TOP_RESULT_ROLE_ID`, `<@&ID>`). Záznam jde do **stejné** kanonické
  historie jako `/result` (`data/ht_results.json`, `resultType: "ht_fight"`).
  **Výhra povyšuje hráče** v `players.json` (canonické `next_ticket_tier` ze
  žebříčku bez LT3E), **prohra tier nemění**; neznámý/retired aktuální tier se
  nikdy nehádá. **Výhra uvnitř HT Fight ticketu** ticket zavře + nastaví HT3+
  cooldown vlastníka a připíše událost do logu ticketu (sdílené zavírání
  s `/result`); prohra nechává ticket otevřený. V kanálu HT Fight ticketu se
  hráč/IGN/kit berou z ticketu (autoritativně) a druhé odeslání se zablokuje
  (idempotence `ticketId + result_type`). Odeslání zprávy do kanálu se sleduje
  přes stavy `pending → sent/failed` na záznamu (selhání nabídne opakování
  tlačítkem). Validuje skóre `0-4`.

- **`/sync data` – kontrola integrity dat** – projede všechny místní databáze
  a hlásí duplicitní hráče / Discord ID / IGN, neplatné tiery, konfliktní
  Discord role, chybějící webové záznamy, neplatné eval reference a osamocené
  tickety a výsledky. **Nic se automaticky nemaže** – bezpečné opravy (zavření
  osamoceného ticketu = jen status, bezeztrátová normalizace tierů) se aplikují
  jen po explicitním potvrzení tlačítkem a zapisují se do
  `data/datacheck_log.json`.

## Architektura synchronizace (Discord → DB → Web)

Tok dat je jednosměrný, kanonická `players.json` je **jediný zdroj pravdy**:

1. **Hodnocení píše do DB** – `/result` (a `/topresult` s `resultType=ht_fight`)
   zapisuje tier + historii do kanonické `players.json`. Na web/GitHub
   **nic neposílá**.
2. **Discord role = projekce DB** – `/sync discord` čte `players.json` +
   `kit_roles.json` přes centrální `services/role_sync.py`, navrhne rozdíly
   a `/sync discord mode:apply` je po potvrzení aplikuje (audit v
   `playersync_log.json`).
3. **Web = kopie** – jediný zapisovatel `players.json` na GitHub je
   `/sync web` (preview/apply s potvrzením, audit v `websync_log.json`).
   Žádný jiný příkaz web nemění.

Pravidla z auditu (kodifikovaná v `services/role_sync.py` + `services/datacheck.py`):

- **Retired tiery** – tier s prefixem `R` v `players.json` (např. `RLT2`) je
  archivovaná historie: **nikdy se nemaže ani nepřepisuje**. RoleSync je
  ignoruje (retired role z `kit_roles.json` nezpůsobí wrong_role /
  unknown_player) a `/sync discord` je reportuje jako samostatnou kategorii
  „🧓 Retired tiery v DB" bez návrhu akce. Datacheck hlásí retired tiery
  zapomenuté v `modes` („🧓 Retired tiery v modes") – jen report, žádná oprava.
- **`discordId` = permanentní identita** – hráč s polem `discordId` se páruje
  s Discord členem podle ID (přes změnu IGN); jméno je jen fallback.
  Záznam BEZ `discordId`, který odpovídá zadanému IGN, se **adoptuje**
  (připojí Discord ID, historie zůstává – nikdy se neslučuje). IGN patřící
  záznamu s JINÝM `discordId` = **konflikt**, operace se odmítá bez zápisu
  (konflikt se nikdy nehádá ani neslučuje automaticky). Duplicitní
  `discordId` napříč hráči detekuje datacheck
  („🆔 Duplicitní discordId hráčů").
- **`/removeplayertiers`** odebírá jen aktuální tiery (`modes`); záznam hráče
  i historie zůstávají.

## Funkce

### 🎯 Fronty na tier testy
| Příkaz | Popis |
|---|---|
| `/openq kit` | Otevře frontu pro kit – vyčistí určený kanál kitu a pošle tam živý panel (embed + tlačítka) se `@everyone`. |
| `/closeq kit` | Zavře frontu (jen pokud v ní nejsou další testeri). Panel v kanálu kitu se upraví na CLOSED a odstraní tlačítka; vyčistí čekající hráče daného kitu. |
| `/addqchannel kit [kanal]` *(admin)* | Nastaví určený kanál panelu fronty pro (nový) kit. Bez `kanal` se použije kanál, kde příkaz běží. |
| `/queue join ign kit` | Přidá se do otevřené fronty (kontrola 4denního cooldownu). |
| `/queue joinastester` | Zaregistruje testera jako globálně aktivního. |
| `/queue joinasqueue kit` | Tester se přidá do už otevřené fronty jako další tester. |
| `/queue leaveq kit` | Tester opustí frontu (při odchodu otevíratele převezme frontu další tester). |
| `/queue list` | Přehled všech front a aktivních testerů. |
| `/queue pull` | Vytáhne prvního hráče z fronty a nechá testera vybrat roomku – hráč do ní dostane přístup (jako pull tlačítko). |
| `/removeq hrac` | Ručně odstraní hráče z fronty. |
| `/skip hrac` | Skipne AFK hráče (pullnutého z fronty) – odebere mu přístup do roomky a vrátí ho na konec fronty (jde před něj každý další). |
| `/mktesterroom [hrac] [kategorie]` | Vytvoří soukromou tester roomku (text kanál) pro pullnutí hráče; zavírá se tlačítkem v roomce. |

Tlačítka na panelu: **Join Queue** (otevře modál pro Minecraft IGN; potvrzení
vidí jen přihlášený hráč), **Leave Queue**, **Pull Player ⚔️** (výběr roomky,
hráč dostane práva a ping). Panel se aktualizuje automaticky (sleduje
příchody/odchody).

> `/openq` **vždy** pošle panel do určeného kanálu daného kitu (ne do kanálu,
> kde byl příkaz zadán) a celý kanál předtím vyčistí. Kanál se určí podle
> priority: `/addqchannel` (runtime, `data/queue_channels.json`) → env
> `QUEUE_CHANNELS_JSON` → výchozí kanály z původního bota.

### 📝 Výsledky tier testů
| Příkaz | Popis |
|---|---|
| `/result hrac ign kit tier score outcome [add_role] [remove_role]` | Zápis výsledku testu. `kit` i `tier` mají autocomplete; tier jde zadat jen **LT5 / HT5 / LT4 / HT4 / LT3 / LT3 + eval**. Volba **LT3 + eval** uloží tier LT3 (stejná role) a přidá hráči **eval status** (data/evals.json) – pak může otevírat HT3+ tickety. Nový kit se automaticky zaregistruje; automaticky rozdá roli tieru dle `/setkitrole`; volitelně i ruční role. |
| `/testerstats tester` | Portfolio testera (celkem testů, oblíbený kit, tier, průměrný čas). |
| `/testersstats current\|all` | Žebříček testerů (tento měsíc / všechny časy). |
| `/addtest tester amount month` *(admin)* | Ruční přidání historických testů. |
| `/removetest user amount` *(admin)* | Odečtení testů (upraví i aktuální měsíc, nikdy pod nulu). |
| `/removeplayertiers ign` *(admin)* | Odebere hráči **všechny aktuální tiery** (`modes`); záznam hráče i historie zůstávají. Web se aktualizuje přes `/sync web`. |
| `/setkitrole kit tier role` *(admin)* | Namapuje roli tieru pro kit – po `/result` ji hráč dostane automaticky. |
| `/unsetkitrole kit tier` *(admin)* | Zruší mapování role tieru pro kit. |
| `/kitrole` | Vypíše všechna namapovaná role (kit → tier). |
| `/sync check [area]` *(admin)* | Komplexní **read-only** diagnostika: porovná **Discord role × `players.json` × web** (statusy: MATCH / MISSING_DISCORD_ROLE / DATABASE_MISMATCH / WEBSITE_MISMATCH / MULTIPLE_TIER_ROLES (KONFLIKT) / UNKNOWN_ROLE / UNKNOWN_PLAYER / DUPLICATE_PLAYER) + integritu dat a rozdíly webu. Agregace **OK/WARNING/CONFLICT/ERROR**, filtr podle `area` (all/identity/tiers/roles/web/data). **Nic nemění** – Discord role už nejsou zdroj pravdy, jen projekce DB. Audit v `data/checkweb_log.json` + `data/datacheck_log.json`. |
| `/sync discord mode:preview\|apply` *(admin)* | Tier role na Discordu vs. kanonická `players.json` (RoleSyncService). `preview` ukáže rozdíly – **nic nemění**; `apply` vyžaduje **explicitní potvrzení** (tlačítko) před aplikací (role add/remove). Stav se mezi náhledem a potvrzením ověřuje, konflikty se neřeší automaticky a **DB se z Discordu nepřepisuje**. Audit v `data/playersync_log.json`. |
| `/sync web mode:preview\|apply` *(admin)* | Web (players.json na GitHubu) vs. kanonická DB. `preview` stáhne a porovná (detekuje chybějící hráče, špatné tiery, zastaralá data, duplicitní hráče, neplatné záznamy) – **nic neposílá**. `apply` po potvrzení nahradí soubor na webu kanonickou DB (čtení i zápis mají retry). Selhání GitHubu **není hlášeno jako úspěch**, prázdná DB se neposílá. Audit v `data/websync_log.json`. |
| `/sync data` *(admin)* | Kontrola integrity všech databází: duplicitní hráči / Discord ID / IGN, neplatné tiery, konfliktní Discord role, chybějící webové záznamy, neplatné eval reference, osamocené tickety a výsledky, **retired tiery v modes** a **duplicitní discordId hráčů**. **Nic nemaže** – bezpečné opravy jen tlačítkem po potvrzení, audit v `data/datacheck_log.json`. |
| `/topresult hrac ign kit fight_tier outcome score opponent tier_status` *(tester)* | HT Fight výsledek – specializovaná verze `/result`, **ne žebříček**. Vyvaliduje skóre `0-4`, HT tier (z žebříčku, bez LT3E) a status; zapíše záznam s `resultType=ht_fight` do **stejné** historie `ht_results.json`. **Výhra povyšuje hráče** (`players.json`, `next_ticket_tier` bez LT3E; v ticketu navíc zavře ticket + nastaví HT3+ cooldown + událost do logu ticketu), **prohra tier nemění a ticket nechává otevřený**; neznámý/retired tier se nehádá. Zprávu pošle ve stylu serveru jen do `TOP_RESULT_CHANNEL_ID` s pingem `TOP_RESULT_ROLE_ID` (stav odeslání `pending → sent/failed`, selhání nabízí opakování tlačítkem). V HT Fight ticketu se hráč/IGN/kit berou z ticketu; 1 ticket = 1 fight výsledek (idempotence). |
> **Deprecated aliasy:** `/playersync preview|apply`, `/websync preview|apply`,
> `/checkweb preview|apply` a `/datacheck` zůstávají funkční a v patičce
> upozorní „⚠️ Deprecated – použij /sync …". Jedinou výjimkou je
> `/checkweb apply`: jako jediný zapisuje do `players.json` z Discordu a
> vyžaduje **explicitní per-záznamové rozhodnutí** ([Use Discord] /
> [Keep Database] / [Ignore]) + potvrzení tlačítkem – „Use Discord“ změní JEN
> tier v `players.json` (bez zápisu do historie), u KONFLIKTU (víc tier rolí)
> se nikdy nevybírá automaticky; audit v `data/checkweb_log.json`. Web se tím
> nemění – na to je `/sync web`.

`/result`:
- nastaví hráči 4denní cooldown (`cooldowns.json`),
- odebere hráče z fronty a práva z roomky (i z voice roomky – hráč se
  přesune do AFK kanálu nebo se odpojí; práva nestačí, voice hráče samy
  nevyhodí),
- uloží tier + historii do `players.json` (modes/history),
- započítá statistiky testerovi (celkem, kity, tiery, měsíční, hodiny),
- **automaticky zaregistruje nový kit** – když kit v `/result` není v
  `data/kits.json`, přidá se (jako přes `/addkit`) a hned se objeví
  v autocomplete, HT3+ panelu a u turnajů (potvrzení toto přizná hláškou),
- **automaticky dá hráči roli tieru kitu** – podle mapy `data/kit_roles.json`
  (nastaví se `/setkitrole`): roli nového tieru přidá a ostatní tier role
  stejného kitu odebere; na nemapovaný tier/tier upozorní v potvrzení,
- **volitelně `add_role` / `remove_role`** – přidá/odebere hráči roli
  (např. roli nového kitu/tieru), aplikuje se tiše jako v originále,
- **pošle výsledek do určeného výsledkového kanálu podle tieru** – HT3 a výš
  (HT3/LT2/HT2/LT1/HT1) jdou do `RESULT_CHANNEL_UPPER`, LT3 a níž do
  `RESULT_CHANNEL_LOWER`. Tester dostane jen soukromé potvrzení,
- **volitelně** zapíše roli dle `/setkitrole` (viz výše). Na GitHub už
  **nic neposílá** – web (players.json na GitHubu) se aktualizuje výhradně
  přes `/sync web`.

### 💸 HT3+ tickety
| Příkaz | Popis |
|---|---|
| `/sendht3` | Pošle panel „Žádost o TierTest“ s výběrem kitu do určeného kanálu. |
| `/cooldown hrac` | Zobrazí HT3+ cooldowny hráče. |
| `/add hrac` | Přidá hráče do aktuálního HT ticketu / roomky (přístup do kanálu; po `/result` odebrán). |
| `/seteval ign kit` *(tester)* | Nastaví hráči status „LT3 + eval“ pro kit – může otevírat HT3+ tickety (role zůstává LT3). |
| `/uneval ign kit` *(tester)* | Odebere hráči „LT3 + eval“ pro kit – HT3+ tickety otevírat nemůže. |

Výběr kitu → kontrola 7denního cooldownu → modál (IGN + cílový tier) →
vytvoření ticket roomky → tlačítko **🔒 Close Ticket** (nastaví cooldown a za 3 s smaže roomku).

**Žádný bypass cooldownu přes reopen:** zavřený ticket zároveň chrání HT3+
cooldown – znovuotevření ticketu se kontrolou cooldownu **zablokuje**, dokud
7denní HT3+ cooldown neskončí (chyba ukáže zbývající čas a kit; stejná
kontrola jako u vytvoření nového ticketu). Výjimky (delegated admin zákrok)
zůstávají na ruční úpravě vlastníka ticketu.

**Automatická kontrola limitu tieru:** při odeslání modálu bot najde hráče podle IGN
v `players.json`, vezme jeho aktuální tier pro daný kit a spočítá „další tier“
(následník v žebříčku **LT5 < HT5 < LT4 < HT4 < LT3 < LT3+eval < HT3 < LT2 < HT2 < LT1 < HT1**).
Ticket na lepší tier, než je hráčův limit, je **zablokovaný** s vysvětlením
(např. hráč s evalu („LT3+eval“) může jít max. na HT3 – ticket na HT1 se nevytvoří).
Retest na aktuálním tieru projde. Aktuální tier z databáze je vidět i v embedu ticketu.

**Brána „Bez evalu“:** HT3+ ticket otevřou jen hráči, kteří mají pro daný kit status
**LT3 + eval** (přes `/seteval`, uložený v `data/evals.json`) nebo jsou už tierem
**HT3 a výš**. Hráčům bez evalu (včetně čistě LT3) se ticket rovnou zablokuje
s vysvětlením – i když nemají v databázi žádný tier. Eval = status „mezi LT3 a HT3“:
role zůstává stejná jako LT3, ale hráč smí otevírat HT3+ tickety.

Kategorie ticket roomky je **nastavitelná pro každý kit** (`HT3_TICKET_CATEGORIES_JSON`),
např. `{"randompot":"...","ironaxe":"..."}`. Priorita: kit → tier
(HT3/LT2/HT2/LT1/HT1) → výchozí `HT3_TICKET_CATEGORY_ID`.

### 🏆 Turnaje
| Příkaz | Popis |
|---|---|
| `/createturnaj role skupiny hodiny kit tier` | Vytvoří turnaj (kategorie + přihlašovací kanál s tlačítkem). |
| `/turnajresult kit hrac z_tieru na_tier` | Pošle výsledek turnaje do určeného kanálu (`TOURNAMENT_RESULT_CHANNEL_ID`). Zapinguje **jen** `TOP_RESULT_ROLE_ID` (jako `/topresult`) – nikdy `@everyone`. |
| `/deleteturnaj kit` | Smaže turnaj i všechny jeho kanály. |

Po uplynutí deadlinu se přihlašování automaticky ukončí, hráči se zamíchají,
rozdělí do skupin, vytvoří se skupinové roomky (viditelné jen dané skupině)
a vylosují se 1v1 zápasy.

### 🗂️ Správa kitů
| Příkaz | Popis |
|---|---|
| `/addkit kit` *(admin)* | Přidá nový kit do seznamu (`data/kits.json`). |
| `/removekit kit` *(admin)* | Odebere kit ze seznamu. |
| `/kits` | Vypíše všechny registrované kity. |
| `/verze` | Diagnostika – commit, stav `/result add_role/remove_role` a přehled slash příkazů (lokální tree / Discord globální / Discord guild / duplicity). |

Přidaný/odebraný kit se hned promítne do:
- HT3+ panelu („Žádost o TierTest“ – select menu s kity, panel se automaticky
  aktualizuje, pokud už byl odeslán),
- autocomplete kitu u `/createturnaj`, `/turnajresult` a `/result`.

**Typický postup pro nový kit:**
1. `/addkit kit:NázevKitu` – zaregistruje kit (HT3+ panel, autocomplete,
   turnaje) – nebo stačí `/result`, který nový kit zaregistruje **automaticky**,
2. `/addqchannel kit:NázevKitu` v kanálu, kam má chodit panel fronty,
3. `/openq kit:NázevKitu` – panel se objeví na určeném místě,
4. vytvoř si role tierů (Server Settings → Roles, např. `NázevKitu S`) a
   namapuj je přes `/setkitrole` – `/result` je pak hráčům dává sám.

> `/openq` a `/queue ...` berou název kitu jako volný text. Naopak kit **bez
> určeného kanálu** (přes `/addqchannel` nebo env) nejde pomocí `/openq`
> otevřít – bot odpoví `❌ Neznámý kit`.

## Struktura projektu

```
bot.py                # jádro bota (vstup: python bot.py)
main.py               # vstupní bod pro hosting (startup file = main.py)
config.py             # konfigurace (.env) + runtime kanály front (/addqchannel)
storage.py            # načítání/ukládání JSON databáze (./data)
panel.py              # živý waitlist panel
views.py              # tlačítka, select menu, modály
utils.py              # pomocné funkce
cogs/
  queues.py           # fronty
  results.py          # výsledky + statistiky + auto role
  roles.py            # /setkitrole, /unsetkitrole, /kitrole + auto-grant rolí
  sync.py             # /sync (check | discord | web | data) + deprecated aliasy
  _shared.py          # sdílené orchestrace helpery (členové guild, role akce, admin gate, save)
  topresult.py        # /topresult (HT Fight výsledky – stejná historie jako /result)
  edituser.py         # /edituser (úpravy hráčů, deleguje na sdílené helpery)
  info.py             # /verze (diagnostika běžící verze)
  ht3.py              # HT3+ tickety
  tournaments.py      # turnaje
  kits.py             # správa kitů (/addkit, /removekit, /kits, /addqchannel)
data/                 # JSON databáze (vytvoří se za běhu)
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

### Důležité nastavení bota na Discord Developer Portalu
- Bot potřebuje intents: **Guilds**, **Guild Messages**, **Message Content**.
- Pro vytváření kanálů/pokojů a otevírání ticketů potřebuje oprávnění
  *Manage Channels*, *Manage Roles* (permice) a *Send Messages*.
- Role testera stačí, aby **obsahovala** „tester“ v názvu (case-insensitive).

## Konfigurace kanálů a kategorií

Výchozí kanály/kategorie odpovídají původnímu botovi. Můžeš je změnit
v `.env` (viz `.env.example`):

| Proměnná | Význam |
|---|---|
| `HT3_PANEL_CHANNEL_ID` | Kanál, kam `/sendht3` pošle panel „Žádost o TierTest“. |
| `HT3_TICKET_CATEGORY_ID` | Výchozí kategorie pro HT3+ ticket roomky. |
| `HT3_TICKET_CATEGORIES_JSON` | Mapa „kit/tier → kategorie“ pro HT3+ tickety, např. `{"randompot":"...","ironaxe":"..."}`. |
| `RESULT_CHANNEL_LOWER` | Výsledkový kanál pro LT3 a níž (`/result`). |
| `RESULT_CHANNEL_UPPER` | Výsledkový kanál pro HT3 a výš (`/result`). |
| `TOP_RESULT_CHANNEL_ID` | Vyhrazený kanál pro veřejné HT Fight výsledky (`/topresult`). |
| `TOP_RESULT_ROLE_ID` | Role, kterou `/topresult` pinguje (`<@&ID>`) – jen tato. |
| `QUEUE_CHANNELS_JSON` | Mapa „kit → určený kanál panelu fronty“, např. `{"randompot":"..."}`. |
| `TESTER_ROOM_CATEGORY_ID` | Kategorie pro tester roomky (`/mktesterroom`); `0` = bez kategorie. |
| `TOURNAMENT_RESULT_CHANNEL_ID` | Kanál pro `/turnajresult`. |
| `GUILD_ID` | Scope registrace příkazů. **Nastaveno** → příkazy se synchronizují jen do této guildy (produkce na jednom serveru). **Prazdné** → jen globálně (dev/testing). Scopy se nikdy nemíchají, takže duplicity nevznikají. |
| `TESTER_ROLE_FRAGMENT` | Fragment názvu tester role (default `tester`). |
| `GITHUB_*` | Volitelná synchronizace `players.json` na GitHub. |

> Kanál panelu fronty pro kit se dá nastavit i za běhu přes `/addqchannel`
> (ukládá se do `data/queue_channels.json` a má přednost před env i defaulty).

## Data

Všechny databáze jsou JSON soubory ve složce `data/` (stejný formát jako
v originále):
`queue.json`, `active_queues.json`, `queue_messages.json`, `queue_channels.json`
(spravuje `/addqchannel`), `testers.json`, `players.json`, `cooldowns.json`,
`testers_stats.json`, `ht3_cooldowns.json`, `tournaments.json`,
`pulled_players.json`, `kits.json`. `players.json` obsahuje pro každého hráče
`modes` (aktuální tiery), `history` (archiv) a volitelně `discordId`
(permanentní identita pro párování s Discord účtem při změně IGN). Kanonická historie výsledků (jak `/result`,
tak `/topresult` s `resultType=ht_fight`) je v `data/ht_results.json`
(append-only, **necommituje se**). Auditní logy synchronizací se uchovávají
v `data/playersync_log.json` (tier role), `data/websync_log.json` (web),
`data/checkweb_log.json` (porovnání Discord × DB × web) a
`data/datacheck_log.json` (kontrola integrity; **necommitují se**; zapisují je
podpříkazy `/sync discord | web | check | data`). Server-specific mapování rolí je
v `data/kit_roles.json` (spravuje `/setkitrole`; **necommituje se** – obsahuje
ID rolí daného serveru).

**Odolnost proti poškozeným souborům:** čtení přes `storage.load_data`
vrací default a zaloguje chybu; **transakce** (zápisy/povýšení – `/result`,
`/topresult`, tickety, sync) čtou v **strict režimu** a poškozený/nečitelný
JSON soubor **nikdy nepřepíšou defaultními daty** – operace se přeruší
(`DataCorruptionError`), původní soubor zůstává nedotčený. Zápis je atomický
(dočasný soubor + `os.replace`).