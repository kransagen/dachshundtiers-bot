# 🐕 DACHSHUNDTIERS – Discord Bot (Python)

Pythonový port Discord bota pro **DACHSHUNDTIERS** (tier testy, fronty, HT3+
tickety a turnaje). Původně napsaný v JavaScriptu (discord.js) v repozitáři
[`kransagen/DACHSHUNDTIERSQBOT`](https://github.com/kransagen/DACHSHUNDTIERSQBOT) –
tento repozitář obsahuje stejné funkce postavené na **discord.py**.

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
| `/result hrac ign kit tier score outcome [add_role] [remove_role]` | Zápis výsledku testu (nový kit se automaticky zaregistruje, volitelně přidá/odebere roli). |
| `/testerstats tester` | Portfolio testera (celkem testů, oblíbený kit, tier, průměrný čas). |
| `/testersstats current\|all` | Žebříček testerů (tento měsíc / všechny časy). |
| `/addtest tester amount month` *(admin)* | Ruční přidání historických testů. |
| `/removetest user amount` *(admin)* | Odečtení testů (upraví i aktuální měsíc, nikdy pod nulu). |
| `/removeplayertiers ign` *(admin)* | Smazání hráče z `players.json`. |
| `/setkitrole kit tier role` *(admin)* | Namapuje roli tieru pro kit – po `/result` ji hráč dostane automaticky. |
| `/unsetkitrole kit tier` *(admin)* | Zruší mapování role tieru pro kit. |
| `/kitrole` | Vypíše všechna namapovaná role (kit → tier). |

`/result`:
- nastaví hráči 4denní cooldown (`cooldowns.json`),
- odebere hráče z fronty a práva z roomky,
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

Výběr kitu → kontrola 7denního cooldownu → modál (IGN + cílový tier) →
vytvoření ticket roomky → tlačítko **🔒 Close Ticket** (nastaví cooldown a za 3 s smaže roomku).

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
1. `/addkit kit:NázevKitu` – zaregistruje kit (HT3+ panel, autocomplete, turnaje)
2. `/addqchannel kit:NázevKitu` v kanálu, kam má chodit panel fronty
3. `/openq kit:NázevKitu` – panel se objeví na určeném místě

> `/openq` a `/queue ...` berou název kitu jako volný text. Naopak `kity`
> bez zadaného kanálu (přes `/addqchannel` nebo env) nejdou pomocí `/openq`
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
  results.py          # výsledky + statistiky + GitHub sync
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
`pulled_players.json`, `kits.json`. Server-specific mapování rolí je
v `data/kit_roles.json` (spravuje `/setkitrole`; **necommituje se** – obsahuje
ID rolí daného serveru).