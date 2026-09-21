# 🐕 DACHSHUNDTIERS – Discord Bot (Python)

Pythonový port Discord bota pro **DACHSHUNDTIERS** (tier testy, fronty, HT3+
tickety a turnaje). Původně napsaný v JavaScriptu (discord.js) v repozitáři
[`kransagen/DACHSHUNDTIERSQBOT`](https://github.com/kransagen/DACHSHUNDTIERSQBOT) –
tento repozitář obsahuje stejné funkce postavené na **discord.py**.

## Funkce

### 🎯 Fronty na tier testy
| Příkaz | Popis |
|---|---|
| `/openq kit` | Otevře frontu pro kit – vytvoří živý panel (embed + tlačítka) a pošle `@everyone`. |
| `/closeq kit` | Zavře frontu (jen pokud v ní nejsou další testeri). |
| `/queue join ign kit` | Přidá se do otevřené fronty (kontrola 4denního cooldownu). |
| `/queue joinastester` | Zaregistruje testera jako globálně aktivního. |
| `/queue joinasqueue kit` | Tester se přidá do už otevřené fronty jako další tester. |
| `/queue leaveq kit` | Tester opustí frontu (při odchodu otevíratele převezme frontu další tester). |
| `/queue list` | Přehled všech front a aktivních testerů. |
| `/queue pull` | Automaticky vytáhne prvního hráče z fronty. |
| `/removeq hrac` | Ručně odstraní hráče z fronty. |

Tlačítka na panelu: **Join Queue** (otevře modál pro Minecraft IGN), **Leave Queue**,
**Pull Player ⚔️** (výběr roomky, hráč dostane práva a ping). Panel se aktualizuje
automaticky (sleduje příchody/odchody).

### 📝 Výsledky tier testů
| Příkaz | Popis |
|---|---|
| `/result hrac ign kit tier score outcome` | Zápis výsledku testu. |
| `/testerstats tester` | Portfolio testera (celkem testů, oblíbený kit, tier, průměrný čas). |
| `/testersstats current\|all` | Žebříček testerů (tento měsíc / všechny časy). |
| `/addtest tester amount month` *(admin)* | Ruční přidání historických testů. |
| `/removetest user amount` *(admin)* | Odečtení testů (upraví i aktuální měsíc, nikdy pod nulu). |
| `/removeplayertiers ign` *(admin)* | Smazání hráče z `players.json`. |

`/result`:
- nastaví hráči 4denní cooldown (`cooldowns.json`),
- odebere hráče z fronty a práva z roomky,
- uloží tier + historii do `players.json` (modes/history),
- započítá statistiky testerovi (celkem, kity, tiery, měsíční, hodiny),
- **volitelně** synchronizuje `players.json` na GitHub (ekvivalent původní Octokit integrace).

### 💸 HT3+ tickety
| Příkaz | Popis |
|---|---|
| `/sendht3` | Pošle panel „Žádost o TierTest“ s výběrem kitu do určeného kanálu. |
| `/cooldown hrac` | Zobrazí HT3+ cooldowny hráče. |

Výběr kitu → kontrola 7denního cooldownu → modál (IGN + cílový tier) →
vytvoření ticket roomky → tlačítko **🔒 Close Ticket** (nastaví cooldown a za 3 s smaže roomku).

### 🏆 Turnaje
| Příkaz | Popis |
|---|---|
| `/createturnaj role skupiny hodiny kit tier` | Vytvoří turnaj (kategorie + přihlašovací kanál s tlačítkem). |
| `/turnajresult kit hrac z_tieru na_tier` | Pošle výsledek turnaje do určeného kanálu. |
| `/deleteturnaj kit` | Smaže turnaj i všechny jeho kanály. |

Po uplynutí deadlinu se přihlašování automaticky ukončí, hráči se zamíchají,
rozdělí do skupin, vytvoří se skupinové roomky (viditelné jen dané skupině)
a vylosují se 1v1 zápasy.

## Struktura projektu

```
bot.py                # vstupní bod, registrace příkazů/view, retry gateway
config.py             # konfigurace (.env)
storage.py            # načítání/ukládání JSON databáze (./data)
panel.py              # živý waitlist panel
views.py              # tlačítka, select menu, modály
utils.py              # pomocné funkce
cogs/
  queues.py           # fronty
  results.py          # výsledky + statistiky + GitHub sync
  ht3.py              # HT3+ tickety
  tournaments.py      # turnaje
data/                 # JSON databáze (vytvoří se za běhu)
```

## Instalace a spuštění

```bash
# 1. Závislosti
pip install -r requirements.txt

# 2. Konfigurace (token a volitelně další hodnoty)
cp .env.example .env   # doplň DISCORD_TOKEN
# nebo: export DISCORD_TOKEN=...

# 3. Spuštění
python bot.py
```

> `.env` je automaticky načten přes `python-dotenv` (viz `config.py`).

### Důležité nastavení bota na Discord Developer Portalu
- Bot potřebuje intents: **Guilds**, **Guild Messages**, **Message Content**.
- Pro vytváření kanálů/pokojů a otevírání ticketů potřebuje oprávnění
  *Manage Channels*, *Manage Roles* (permice) a *Send Messages*.
- Role testera stačí, aby **obsahovala** „tester“ v názvu (case-insensitive).

## Úprava ID kanálů

Výchozí kanály/kategorie odpovídají původnímu botovi. Můžeš je změnit
v `.env` (viz `.env.example`):
- `HT3_PANEL_CHANNEL_ID` – kanál, kam `/sendht3` pošle panel
- `HT3_TICKET_CATEGORY_ID` – kategorie pro HT3+ ticket roomky
- `TOURNAMENT_RESULT_CHANNEL_ID` – kanál pro `/turnajresult`

## Data

Všechny databáze jsou JSON soubory ve složce `data/` (stejný formát jako
v originále):
`queue.json`, `active_queues.json`, `queue_messages.json`, `testers.json`,
`players.json`, `cooldowns.json`, `testers_stats.json`, `ht3_cooldowns.json`,
`tournaments.json`, `pulled_players.json`.