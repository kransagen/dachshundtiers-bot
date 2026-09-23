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
- **`/verze`** – diagnostika: commit běžícího bota, stav `/result` a počet
  slash příkazů (rozliší „staré nasazení" od „cache Discordu").
- **`/setkitrole` / `/unsetkitrole` / `/kitrole`** – mapa „kit → role tieru"
  v `data/kit_roles.json` (necommituje se).
- **Opraven bug „Synchronizováno 0"** – `tree.copy_global_to(guild=...)` před
  `sync(guild=...)`, takže slash příkazy na serveru nikdy nezmizí.
- **`/checkweb` nově zapisuje na web** – projede hráče u **každého kitu**
  (podle tier rolí z `/setkitrole`) a chybějící / změněné tiery **automaticky
  zapíše do `players.json`** (modes + history) a synchronizuje na GitHub, takže
  web se sám doplní. Data čte primárně z GitHubu (aby nepřepsal novější změny
  od `/result`), jinak z lokální kopie.

- **`/playersync` – synchronizace tier rolí s players.json** – porovná Discord
  tier role (`/setkitrole`) s kanonickou `players.json` a detekuje chybějící
  role, špatné role, víc tier rolí, neznámé hráče, chybějící hráče a neplatné
  tiery. **Nikdy neřeší konflikty automaticky** – `/playersync preview` ukáže
  rozdíly, `/playersync apply` vyžaduje explicitní potvrzení tlačítkem.
  Každé použití se zapisuje do `data/playersync_log.json` (audit).

- **`/websync` – synchronizace webu s kanonickou `players.json`** – web
  (players.json na GitHubu) je jen kopie, jediný zdroj pravdy zůstává lokální
  kanonická DB. `/websync preview` stáhne web a detekuje chybějící hráče,
  špatné tiery, zastaralá data, duplicitní hráče a neplatné záznamy.
  `/websync apply` po **explicitním potvrzení** (tlačítko) nahradí players.json
  na webu kanonickou DB; čtení i zápis se opakují s retry. Každý běh se
  zapisuje do `data/websync_log.json` (timestamp, počet záznamů,
  úspěch/selhání a chyby).

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
| `/removeplayertiers ign` *(admin)* | Smazání hráče z `players.json`. |
| `/setkitrole kit tier role` *(admin)* | Namapuje roli tieru pro kit – po `/result` ji hráč dostane automaticky. |
| `/unsetkitrole kit tier` *(admin)* | Zruší mapování role tieru pro kit. |
| `/kitrole` | Vypíše všechna namapovaná role (kit → tier). |
| `/checkweb` *(tester)* | Projede hráče u **každého kitu** (podle tier rolí z `kit_roles.json`, nastavených přes `/setkitrole`) a hráče, kteří nemají tier zapsaný na webu (`players.json`), **tam automaticky zapíše** – včetně historie s dnešním datem. Následně synchronizuje `players.json` na GitHub (web). V odpovědi ukáže přehled per kit (✅ už zapsáno / ➕ nově / ✏️ aktualizováno). |
| `/playersync preview` *(admin)* | Porovná tier role na Discordu s `players.json` a ukáže přehled rozdílů – **nic nemění**. |
| `/playersync apply` *(admin)* | Ukáže stejný přehled a vyžaduje **explicitní potvrzení** (tlačítko) před aplikací změn. Stav se mezi náhledem a potvrzením ověřuje. Každé použití se zapisuje do `data/playersync_log.json`. |
| `/websync preview` *(admin)* | Stáhne players.json z webu (GitHub) a porovná ho s kanonickou `players.json` – detekuje chybějící hráče, špatné tiery, zastaralá data, duplicitní hráče a neplatné záznamy. **Nic neposílá.** |
| `/websync apply` *(admin)* | Ukáže stejný přehled a po **explicitním potvrzení** (tlačítko) nahradí players.json na webu kanonickou databází. Kanonická DB se mezi náhledem a potvrzením ověřuje; čtení i zápis mají retry. Výsledek se zapisuje do `data/websync_log.json` (timestamp, počet záznamů, úspěch/selhání a chyby). |

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
- **volitelně** synchronizuje `players.json` na GitHub (ekvivalent původní Octokit integrace).

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
| `/turnajresult kit hrac z_tieru na_tier` | Pošle výsledek turnaje do určeného kanálu. |
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
| `/verze` | Diagnostika – commit běžícího bota a stav `/result add_role/remove_role`. |

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
  results.py          # výsledky + statistiky + GitHub sync + auto role
  roles.py            # /setkitrole, /unsetkitrole, /kitrole, /checkweb + auto-grant rolí
  playersync.py       # /playersync (porovnání tier rolí s players.json, audit)
  websync.py          # /websync (porovnání webu s players.json + zápis na web, audit)
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
| `QUEUE_CHANNELS_JSON` | Mapa „kit → určený kanál panelu fronty“, např. `{"randompot":"..."}`. |
| `TESTER_ROOM_CATEGORY_ID` | Kategorie pro tester roomky (`/mktesterroom`); `0` = bez kategorie. |
| `TOURNAMENT_RESULT_CHANNEL_ID` | Kanál pro `/turnajresult`. |
| `GUILD_ID` | Registrace příkazů jen na tomto serveru (rychlejší vývoj). |
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
`pulled_players.json`, `kits.json`. Auditní logy synchronizací se uchovávají
v `data/playersync_log.json` (tier role) a `data/websync_log.json` (web;
**necommitují se**). Server-specific mapování rolí je
v `data/kit_roles.json` (spravuje `/setkitrole`; **necommituje se** – obsahuje
ID rolí daného serveru).