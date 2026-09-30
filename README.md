# 🐕 DACHSHUNDTIERS – Discord Bot (Python)

Discord bot pro **DACHSHUNDTIERS** (tier testy, fronty, HT3+ tickety a
turnaje). Port původního JS bota
([`kransagen/DACHSHUNDTIERSQBOT`](https://github.com/kransagen/DACHSHUNDTIERSQBOT))
na **discord.py**.

## 📖 Pro koho je tahle dokumentace

| Jestliže jsi… | Čti |
|---|---|
| **Admin na Discordu** (řešíš tickety, fronty, výsledky) | **[Handbook pro administrátory](#handbook-pro-administrátory)** – hned níže |
| Vývojář / provozovatel hostingu | [Architektura](#architektura) níž a dál |

---

# 📖 Handbook pro administrátory

Vše, co potřebuješ k běžnému provozu. Žádné `alembic`, žádné `.env` –
pokud neřešíš pády bota, tohle nepotřebuješ.

## Kdo co může

Oprávnění se nekontroluje přes Discord „Manage Roles“, ale přes **roli**:

| Kdo | Jak se pozná | Co může |
|---|---|---|
| **Hráč** | propojený účet | `/link`, `/linkign`, `/linked`, `/unlink`, `/retire`, `/peaktier`, `/join`, `/list`, `/leaveq` + tlačítka v panelech |
| **Tester** | má tester roli | + `/claim`, `/unclaim`, `/add`, `/remove`, `/seteval`, `/uneval`, `/result`, `/topresult`, `/turnajresult`, `/joinastester`, `/joinasqueue`, `/pull`, `/mktesterroom` |
| **Admin** | má Discord **Administrator**, nebo roli z `ADMIN_ROLE_IDS` | + `/edituser`, `/linkdiscord`, `/addtest`, `/removetest`, `/removeplayertiers`, `/setkitrole`, `/unsetkitrole`, `/addkit`, `/removekit`, `/createturnaj`, `/deleteturnaj`, celé `/sync *`, `/dbstatus` |

Tester roli určuje `TESTER_ROLE_IDS` (přesná ID rolí) — je-li nastaven, funguje
**jen** přes ID, ne podle názvu. Bez něj stačí, že název tester role obsahuje
`TESTER_ROLE_FRAGMENT` (default `tester`, case-insensitive — „Head Tester“ se
pozná).

> Když ti příkaz odpoví „❌ Pouze pro administrátory“, chybí ti **Discord
> Administrator** nebo role z `ADMIN_ROLE_IDS`. Tester role nestačí.

### ⚠️ Příkazy, které NEjsou v kódu zabezpečené

Tady buď opatrní. Tyto příkazy **nemají v kódu žádnou kontrolu oprávnění** –
může je použít kdokoli, kdo vidí příkaz (pokud jde o ne-guild registraci):

| Příkaz | Proč je to problém |
|---|---|
| `/openq kit` | **vyčistí kanál kitu** a pošle panel, v jehož textu je `@everyone` (`cogs/queues.py`). Bez `allowed_mentions` to Discord zpracuje jako skutečný ping → kdokoli může spamovat ping serveru |
| `/closeq kit` | kdokoli může zavřít frontu testerům |
| `/addqchannel kit` | mění konfiguraci bota (kam chodí panely) |
| `/sendht3` | spam panelu do kanálu |
| `/pull kit` | vytáhne hráče z fronty do roomky |
| `/mktesterroom` | vytvoří tester roomku |

Pokud to chceš omezit, je to chyba, kterou je potřeba opravit v kódu –
`has_admin_role()` jako první věc v callbacku (viz `/addkit`). Do té doby
předpokládej, že je může spustit kdokoli, a hlaste to jako známé omezení.

## ⚠️ Tři pravidla, která nesmíš porušit

### 1. Nikdy nesahej na tier role ručně v Discordu

Toto je nejdůležitější pravidlo a porušení je těžké odhalit.

Bot **každou hodinu** zrcadlí Discord → databázi (`bot.py`, hodinová
reconciliation). Takže když role na Discordu upravíš ručně, **nejpozději do
hodiny se to propíše do databáze jako pravda** – bez záznamu v historii a bez
záznamu, kdo to udělal. Výsledek je hráč s tichou historií, u které se nedozvíš,
že jsi ji změnil ty.

Upravuj tiery **pouze** přes:

- `/result` – běžný tier test
- `/topresult` – HT Fight
- `/edituser` – ruční zásah s historií
- `/sync discord-rollback` – vrácení botem provedené změny

### 2. Cooldowny se neobchází

| Cooldown | Doba | Kde |
|---|---|---|
| Mezi tier testy (libovolný kit) | **4 dny** | `PLAYER_COOLDOWN_MS` |
| Mezi HT3+ tickety | **7 dní na kit** | `HT3_COOLDOWN_MS` |

Znovuotevření ticketu (`🔓 Reopen Ticket`) podléhá **stejné** kontrole jako nový
ticket. Není to výjimka. Pokud hráč tvrdí, že cooldown neplatí, nepomlčkej to –
`/cooldown hráč` ukáže přesný zbývající čas.

### 3. Neprováděj opravy dat, dokud nevíš, co se děje

Každá oprava má **preview**. Před `apply` vždy pust preview a přečti si výstup:

```
/sync check                -> read-only diagnostika, nic nemění
/sync web mode:preview     -> co přesně se zapíše na web
/sync importdiscord mode:preview  -> jaké jsou rozdíly Discord vs DB
/sync discord-rollback     -> defaultně dry run
```

## Tier žebříček

Od nejhoršího po nejlepší:

```
LT5 → HT5 → LT4 → HT4 → LT3 → HT3 → LT2 → HT2 → LT1 → HT1
```

| Tier | Poznámka |
|---|---|
| `LT3 + eval` | **virtuální status**, ne žebříčkový stupeň. Hráč má pořád roli `LT3`, ale smí otevírat HT3+ tickety. V kódu `LT3E`. |
| `R` + kód (např. `RLT2`) | **retired** – vyměněný tier. Role se zásadně nemaže ani nepřepisuje, hráči se přesměruje. |

Když hráč v žebříčku postoupí o stupeň, dostane roli podle mapování
(`/setkitrole`). Bez namapované role mu tier propíše v databázi, ale roli na
Discordu nedostane.

## Co bot hlídá sám

Tyhle kontroly nejdou obejít a **nemá je smysl obcházet** – když ti bot
zapře, hráč nesplňuje podmínky:

- **Cooldown** – 4 dny mezi testy, 7 dní na kit mezi HT3+ tickety.
- **Limit tieru** – ticket na tier *vyšší* než hráčův aktuální se odmítne
  (LT5 hráč si neotevře HT5 ticket). **Retest na aktuálním tieru projde** vždy.
- **Brána „bez evalu“** – HT3+ ticket otevřou jen hráči se statusem
  `LT3 + eval` nebo s tierem HT3 a vyšším.

## Běžný den: tier test

| Krok | Kdo | Jak |
|---|---|---|
| 1. Otevřít frontu | admin | `/openq kit` |
| 2. Hráč se zapíše | hráč | `/join kit`, nebo tlačítko **Join Queue** v panelu |
| 3. Vybrat hráče | tester | `/pull kit` → vytvoří tester roomku |
| 4. Zapsat výsledek | tester | `/result hrác ign kit tier score outcome` |
| 5. Aktualizovat web | admin | `/sync web mode:preview` → `mode:apply` |

**`/result` udělá všechno najednou:** nastaví 4denní cooldown, vyhodí hráče
z fronty i roomky, uloží tier a zápis do historie, přičte test testerovi a pošle
zprávu do výsledkového kanálu (HT3+ → `RESULT_CHANNEL_UPPER`, LT3 a níž →
`RESULT_CHANNEL_LOWER`).

- `tier` – `LT5`, `HT5`, `LT4`, `HT4`, `LT3`, `LT3 + eval`
- `outcome` – `Tester Won` / `Tester Lost`
- roli hráč dostane **podle mapování** `/setkitrole`, ne vlastním parametrem

> Web (`players.json`) se po `/result` **neaktualizuje**. To dělá výhradně
> `/sync web`. Když to zapomeneš, web bude ukazovat starý stav — to je
> zamýšlené, ne chyba.

## Běžný den: HT3+ ticket

| Krok | Kdo | Jak |
|---|---|---|
| 1. Panel | admin | `/sendht3` |
| 2. Hráč si otevře ticket | hráč | tlačítko v panelu → výběr kitu → modál (IGN + cílový tier) |
| 3. Převzít ticket | tester | `/claim` nebo tlačítko **✅ Claim HT** |
| 4. Vydat ticket | tester | tlačítko **🔒 Close Ticket** |
| 5. Cooldown | bot | nastaví se sám, 7 dní na daný kit |

Bot v modálu automaticky ověří **eval bránu**, **limit tieru** a **cooldown**.
Když nepustí, napiš hráči *proč* – zpráva to říká.

Tlačítka v ticket roomce: **✅ Claim HT**, **↩️ Unclaim**, **🔒 Close Ticket**,
**🔓 Reopen Ticket**.

Přidat do ticketu dalšího hráče: `/add hráč` (přístup + sledování), odebrat:
`/remove hráč`.

Eval (brána k HT3+):

| Příkaz | Kdo | Efekt |
|---|---|---|
| `/seteval ign kit` | tester | hráč smí otevírat HT3+ tickety na ten kit |
| `/uneval ign kit` | tester | odebere |

## HT Fight: `/topresult`

Jen pro testery. **Ne** je to žebříček testů.

| Pole | Význam |
|---|---|
| `fight_tier` | tier, o který se hraje (např. `HT3`) |
| `outcome` | `vyhrál` / `prohrál` |
| `score` | skóre, např. `0-4` |
| `opponent` | soupeř / tester |
| `tier_status` | textový stav, např. `Zůstává Low Tier 3` |
| `bridge` | **jen při výhře** – přeskočení o více stupňů (např. z LT3 rovnou na LT2) |

Uvnitř HT Fight ticketu se hráč, IGN i kit vezmou automaticky z ticketu, takže
je tam zadávat nemusíš. Výhra hráče **povyšuje**, prohra **tier nemění**.
Zpráva jde veřejně do `TOP_RESULT_CHANNEL_ID` s pingem `TOP_RESULT_ROLE_ID`.
V ticketu příkaz zároveň ticket zavře a nastaví cooldown.

> `bridge` je skutečné přeskočení žebříčku. Používej ho jen tam, kde to opravdu
> platí – bez něj se hráč posune jen o jeden stupeň.

## Turnaje

| Příkaz | Kdo | Popis |
|---|---|---|
| `/createturnaj role skupiny hodiny kit tier` | admin | vytvoří turnaj, kategorii a přihlašovací kanál |
| `/turnajresult kit hráč z_tieru na_tier` | tester | zapíše zápas do `TOURNAMENT_RESULT_CHANNEL_ID` |
| `/deleteturnaj kit` | admin | **smaže turnaj i jeho kanály** |

Po deadlinu se přihlašování uzavře, hráči se rozdělí do skupin (skupinové
roomky) a vylosují se 1v1 zápasy.

## Správa hráčů

### `/edituser hráč` – hlavní nástroj

Otevře editor s pěti akcemi:

| Tlačítko | Kdy použít |
|---|---|
| 🎭 **Změnit Discord ID** | hráč si změnil Discord účet. **17–19 číslic.** Konflikt (stejný IGN, jiné ID) se nikdy neřeší automaticky – uvidíš ho v historii |
| ⚔️ **Změnit IGN** | překlep nebo změna jména ve hře |
| 🏆 **Změnit tier kitu** | oprava tieru. Zapíše se do historie |
| ⏳ **Cooldowny** | zkrácení / prodloužení cooldownu (např. omluva) |
| 📜 **Historie** | jen zobrazení, nemění nic |

Bez historie se nepoužívá – použij `/sync data` nebo `/edituser`, ne přímé
zásahy do role.

### Propojení Discord ↔ Minecraft

| Příkaz | Kdo | Popis |
|---|---|---|
| `/link` | hráč | vygeneruje jednorázový kód (platí **15 minut**) |
| `/linkign ign` | hráč | propojí rovnou podle zadaného IGN. Vrátí, jestli propojení nově vytvořil, převzal cizí záznam, sloučil, nebo jen přejmenoval |
| `/linked` | hráč | ukáže stav propojení a případný čekající kód |
| `/unlink` | hráč | zruší propojení (záznam hráče v DB zůstává) |
| `/linkdiscord hrác ign` | **admin** | nucené propojení, když hráč neprojde sám |

Dokončení kódu z `/link` se ověřuje **na straně Minecraftu** – proto
Discordový příkaz nikdy nepřijímá UUID od volajícího, jinak by si mohl
propojit kdokoli cizí účet. Když hráč tvrdí, že propojení nefunguje,
nech ho vygenerovat nový `/link` (starý kód vypršel) a ověř v `/linked`, že
čeká kód.

### Když `/linkign` odmítne

`/linkign` samo sebe poctivě odmítne, a to je vždy dobře znamení – **nepomáhej
hráci tím, že to obejdeš**. Každá zpráva říká, koho kontaktovat:

| Odmítnutí | Co znamená | Kdo to spraví |
|---|---|---|
| „IGN už je propojené s jiným Discord účtem“ | cizí účet drží ten IGN. **Nikdy nepřepisuj** | admin přes `/edituser` → 🎭 Změnit Discord ID |
| „Už jsi propojený jako X a IGN Y má vlastní historii“ | dva záznamy se nesmí spojit automaticky | admin – přesun historie ručně |
| „Záznamy nejde sloučit (oba mají cooldown na stejný kit)“ | konflikt při sloučení | admin |
| „Neplatné IGN“ | Minecraft jméno má 3–16 znaků (písmena, čísla, `_`) | hráč |

## Když něco nefunguje

Začni **read-only** příkazem. `/sync check` nic nemění, ale řekne, kde je
problém.

| Co pozoruješ | Co spustit | Co zjistíš |
|---|---|---|
| „Nefunguje to“ / „nesouhlasí to“ | `/sync check` | Discord × DB × web + integrita, agregováno OK/WARNING/CONFLICT/ERROR |
| Chceš zúžit problém | `/sync check area:roles` | jen jedna oblast |
| „Co bot právě běží?“ | `/verze` | commit + stav `/result` |
| „Jak dlouho má hráč cooldown?“ | `/cooldown hráč` | přesné cooldowny, read-only |
| Hráč má jiný Discord účet než IGN | `/edituser` → 🎭 Změnit Discord ID | 17–19 číslic, jde do historie |
| Web ukazuje starý stav | `/sync web mode:preview` | co přesně chybí; pak `mode:apply` |
| Zmizela role / rozdíl Discord vs DB | `/sync importdiscord mode:preview` | náhled rozdílů, **nic nezapisuje** |
| „Udělal jsem chybu v rolích“ | `/sync discord-rollback` | **dry run defaultně** – nejdřív si přečti, až pak `mode:apply` |
| Změna se nepropisuje | `/dbstatus` | dostupnost DB a počty (nikdy neukáže host/heslo) |

`/sync check` umí filtrovat podle oblasti — hodnoty pro `area` jsou:
`all` (výchozí), `identity`, `tiers`, `roles`, `web`, `data`, `db`.

### `/sync discord-rollback` – tlačí po souvislosti

Rollback vrátí Discord do stavu **před** posledním aplikovaným `/sync discord`
a jde **výhradně** přes `memberId`/`roleId` z auditu. Před změnou kontroluje,
jestli hráč nebyl mezitím znovu povýšen – když ano, rollback ho přeskočí a
upozorní (aby ti to neshodil zpět).

Vždy nejdřív defaultní `mode:preview`, výstup si přečti, a teprve pak
potvrď. Cílový sync lze vybrat přes `target_ts`.

## ⚠️ Co je obtížné vzít zpět

| Operace | Co se stane | Bezpečnější varianta |
|---|---|---|
| `/sync discord-rollback mode:apply` | vrací role do stavu před syncem | nejdřív `mode:preview` |
| `/removeplayertiers ign` | hráči zmizí **všechny** aktuální tiery. **Historie zůstává.** | `/edituser` a změň jen dotčený kit |
| `/deleteturnaj kit` | **smaže kanály turnaje** | – |
| `/sync data` (po potvrzení) | opraví neautoritativní nesrovnalosti | nejdřív `/sync check` |
| ruční editace tier role | propíše se do DB do hodiny, bez historie | `/result` / `/edituser` |
| `/sync web mode:apply` | přepíše web soubor | `mode:preview` |

## Nový kit od nuly

```
/addkit kit        →  /addqchannel kit   →  /openq kit
      ↓                                   ↑
vytvoř role tierů na serveru                │
      ↓                                   │
/setkitrole kit tier role  (opakuj pro každý tier)
```

Bez `/setkitrole` hráč dostane tier v DB, ale žádnou roli na Discordu.

---

# 🛠️ Pro vývojáře a provozovatele

Zbytek této dokumentace. Pokud jsi admin a došel jsi sem omylem, vrať se k
[handbooku](#handbook-pro-administrátory).

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
- **Normální synchronizace je read-only.** `/sync discord` i automatická
  reconciliation (při startu a každou hodinu) jen čtou role z Discordu a
  zapisují je do PostgreSQL zrcadla; nikdy neopačně. Jediné příkazy, které
  smí měnit Discord role, jsou vypsané výše.
- **Žádné JSON úložiště za běhu.** Stará data z `data/*.json` a z tabulky
  `dachshundtiers_data` se jednorázově převádějí do normalizovaných tabulek
  příkazem `python -m tools.legacy_import` (viz sekce [Data](#data)).

Podrobnosti a auditní důkaz viz `docs/PHASE_G0_FINAL_REPORT.md` a
`docs/PHASE_H_FINAL_REPORT.md`.

### Pravidla identity a historie

- **Retired tier** – prefix `R` (např. `RLT2`) je v `tier_definitions`
  samostatný tier typu `retired`, který odkazuje na svůj základní tier
  (`RLT2` → `LT2`). Nikdy se nemaže ani nepřepisuje.
- **`discordId`** je permanentní identita hráče (páruje se přes změnu IGN).
  Konflikt (stejný IGN, jiné `discordId`) se nikdy neřeší automaticky.
- **`/removeplayertiers`** odebírá jen aktuální tiery (`modes`); historie
  zůstává.

## Funkce

### 🧓 Retire a peak tier
| Příkaz | Kdo | Popis |
|---|---|---|
| `/retire kit` | hráč | odchod do retire z LT2/HT2/LT1/HT1 v daném kitu. Bot ověří nárok, po potvrzení sundá roli tieru (a přidá roli retired tieru, je-li namapovaná), zapíše `R…` tier a uloží peak |
| `/peaktier` | hráč | ukáže zapsané peaky a kolik dní / výher zbývá do dalších (nic nezapisuje) |

Prahy: **LT2/HT2** 60 dní na tieru *nebo* 2 výhry, **LT1/HT1** 90 dní *nebo* 3 výhry
(počítají se jen HT výsledky – `/topresult`: výhra nad testovaným o rank níže či
stejným od doby, co máš současný tier). **HT3** dává peak po 60 dnech.
Peak zapisuje bot sám (kontrola každou hodinu), nikdy se nemaže ani nesnižuje.
Unretire zatím v botovi není.

### 🎯 Fronty na tier testy
| Příkaz | Popis |
|---|---|
| `/openq kit` | Otevře frontu pro kit – vyčistí kanál kitu a pošle živý panel (embed + tlačítka) s `@everyone`. |
| `/closeq kit` | Zavře frontu (jen bez čekajících testerů). |
| `/addqchannel kit [kanal]` *(admin)* | Nastaví kanál panelu fronty pro kit. |
| `/join kit` | Přidá se do fronty (kontrola 4denního cooldownu). |
| `/joinastester` | Zaregistruje testera jako globálně aktivního. |
| `/joinasqueue kit` | Tester se přidá do fronty jako další tester. |
| `/leaveq kit` | Tester opustí frontu. |
| `/list` | Přehled front a aktivních testerů. |
| `/pull kit` | Vytáhne prvního hráče z fronty do vybrané roomky. |
| `/removeq hrac` | Ručně odstraní hráče z fronty. |
| `/skip hrac` | Skipne AFK hráče – vyhodí ho z roomky i z fronty. |
| `/mktesterroom kit [hrac] [kategorie]` | Soukromá tester roomka pro pullnutí hráče. |

Tlačítka panelu: **Join Queue** (modál s Minecraft IGN), **Leave Queue**,
**Pull Player ⚔️** (výběr roomky). Panel se aktualizuje automaticky.

> `/openq` vždy posílá panel do kanálu určeného pro daný kit (priorita:
> `/addqchannel` → env `QUEUE_CHANNELS_JSON` → výchozí kanál), ne do kanálu,
> kde byl příkaz zadán.

### 📝 Výsledky tier testů
| Příkaz | Popis |
|---|---|
| `/result hrac ign kit tier score outcome [notes]` | Zápis výsledku. Tier: `LT5/HT5/LT4/HT4/LT3/LT3+eval`. Outcome: `Tester Won`/`Tester Lost`. Nový kit se zaregistruje automaticky; roli hráč dostane dle mapování `/setkitrole` (příkaz žádnou roli nenastavuje). |
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
| `/sync discord` *(admin)* | Přečte tier role všech členů a zapíše je do PostgreSQL zrcadla (Discord → DB). Hlásí anomálie: více tier rolí pro jeden kit, neznámé hráče, tier v DB bez role na Discordu. | Jen DB zrcadlo; Discord role se nemění. |
| `/sync discord-rollback [mode] [target_ts]` *(admin)* | Bezpečná inverze posledního `/sync discord apply` – vrátí Discord do stavu před syncem, výhradně přes `memberId`/`roleId` z auditu. Kontroluje, jestli hráč nebyl mezitím znovu povýšen. Výchozí `mode:preview` (dry run). | Jen Discord role, po explicitním potvrzení. |
| `/sync importdiscord mode:preview\|apply` *(admin)* | Náhled jednoznačných rozdílů Discord × DB – **nic nezapisuje** (ani DB, ani web); nasměruje k `/sync discord` a `/sync web`. | Nic. |
| `/sync web mode:preview\|apply` *(admin)* | Export kanonických dat na GitHub/web. `apply` po potvrzení nahradí web. Selhání GitHubu se nikdy nehlásí jako úspěch. | Jen GitHub soubor. |
| `/sync data` *(admin)* | Kontrola integrity: duplicity, neplatné tiery, konfliktní role, osamocené tickety. Opravy jen po potvrzení tlačítkem. | Jen po potvrzení, jen neautoritativní opravy. |

Audit: `/sync discord` a automatická reconciliation zapisují do tabulek
`sync_runs` / `sync_actions` (jen skutečné změny a anomálie). Ostatní
podpříkazy zatím zapisují audit ještě přes JSON dokumenty v tabulce
`dachshundtiers_data` – jejich přesun do `audit_logs` je rozpracovaný.

### 💸 HT3+ tickety
| Příkaz | Popis |
|---|---|
| `/sendht3` | Pošle panel „Žádost o TierTest“ s výběrem kitu. |
| `/cooldown hrac` | Zobrazí HT3+ cooldowny hráče. |
| `/claim` *(tester)* | Převzme aktuální HT ticket (také tlačítko **✅ Claim HT**). |
| `/unclaim` *(tester)* | Vzdá se ticketu (také tlačítko **↩️ Unclaim**). |
| `/add hrac` | Přidá hráče do aktuálního HT ticketu/roomky. |
| `/remove hrac` | Odebere hráče z ticketu (zruší přístup). |
| `/seteval ign kit` *(tester)* | Nastaví „LT3 + eval“ – hráč smí otevírat HT3+ tickety. |
| `/uneval ign kit` *(tester)* | Odebere „LT3 + eval“. |

Tok: výběr kitu → kontrola 7denního cooldownu → modál (IGN + cílový tier) →
ticket roomka → tlačítko **🔒 Close Ticket** (nastaví cooldown, smaže roomku).

- **Bez bypassu cooldownu:** znovuotevření ticketu (**🔓 Reopen Ticket**) se
  stejnou kontrolou cooldownu jako nový ticket.
- **Limit tieru:** ticket na tier vyšší než hráčův aktuální se zablokuje s
  vysvětlením. Žebříček pro limit je
  `LT5 < HT5 < LT4 < HT4 < LT3 < LT3+eval < HT3 < LT2 < HT2 < LT1 < HT1` –
  „LT3+eval“ (`LT3E`) je zde virtuální status: hráč má pořád roli `LT3`, ale
  retest na `HT3` z něj projde. Retest na aktuálním tieru projde vždy.
  (Kanonický katalogový žebříček v `db/tier_catalog.py` `LT3E` neobsahuje –
  to je jen stav, ne stupeň.)
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
storage.py            # legacy JSON I/O (dožívá; nahrazuje ho db/)
db/                    # PostgreSQL: modely, repozitáře, služby
  tier_catalog.py       # žebříček tierů, pořadí (rank), retired varianty
migrations/            # Alembic migrace schématu
tools/
  legacy_import.py      # jednorázový import starých JSON dat do PostgreSQL
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
data/, backups/         # lokální JSON data a zálohy – NIKDY v gitu (.gitignore)
```

## Instalace a spuštění

```bash
# 1. Virtuální prostředí + závislosti
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Konfigurace
cp .env.example .env   # doplň DISCORD_TOKEN a DATABASE_URL

# 3. Schéma databáze (bot to při startu udělá sám, pokud AUTO_MIGRATE≠0)
.venv/bin/alembic upgrade head

# 4. Spuštění lokálně
.venv/bin/python bot.py
# nebo (hosting obvykle spouští main.py)
.venv/bin/python main.py
```

> Když `.venv` přestane fungovat po aktualizaci systémového Pythonu
> (odkazuje na verzi, která už neexistuje), obnov ho přes
> `python3 -m venv --clear .venv` a znovu nainstaluj závislosti.

Testy běží nad skutečnou PostgreSQL (`embedded-postgres` si stáhne binárky
sám, žádná instalace serveru není potřeba):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

> `.env` je automaticky načten přes `python-dotenv` (viz `config.py`).

### Nastavení bota na Discord Developer Portalu
- Intents: **Guilds**, **Guild Messages**, **Message Content**.
- Oprávnění: *Manage Channels*, *Manage Roles*, *Send Messages*.
- Tester roli stačí, aby obsahovala „tester" v názvu (case-insensitive) — **pokud
  není nastaven `TESTER_ROLE_IDS`**. S allowlistem rolí musí být její ID v
  `TESTER_ROLE_IDS`, jinak přihlášený tester nebude testerem.
- Admin příkazy vyžadují Discord **Administrator**, nebo roli z
  `ADMIN_ROLE_IDS`.

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
| `TESTER_ROLE_FRAGMENT` | Fragment názvu tester role (default `tester`, case-insensitive). Používá se **jen když není nastaven `TESTER_ROLE_IDS`**. |
| `TESTER_ROLE_IDS` | Allowlist ID tester rolí (čárkami). Je-li nastaven, tester se pozná **výhradně** podle přesného ID role – název se už nehledá. |
| `ADMIN_ROLE_IDS` | Allowlist ID admin rolí (čárkami). Když je nastaven, tyto role mají admin práva; bez něj (i s nimi) rozhoduje Discord oprávnění **Administrator**. |
| `GITHUB_*` | Volitelný export `players.json` na GitHub. |
| `DATABASE_URL` | **Povinné.** PostgreSQL připojení, např. `postgresql://user:heslo@host:5432/dachshundtiers`. Bez něj bot nenastartuje. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Alternativa k `DATABASE_URL` (bot URL sestaví sám). |
| `DB_HOSTADDR` | Volitelné vynucení IPv4 adresy DB (hodí se bez IPv6 trasy). |
| `LEGACY_IMPORT` | `preview` / `apply`: jednorázový import starých JSON dat při startu (viz [Data](#data)). Po importu odeber. |
| `AUTO_MIGRATE` | Default `1`: bot při startu sám spustí `alembic upgrade head` (hostingy bez konzole). `0` = migrace spouštíš ručně. |

> Kanál panelu fronty jde nastavit i za běhu přes `/addqchannel`
> (ukládá se do tabulky `bot_config`, má přednost před env i defaulty).

## Data

`DATABASE_URL` je **povinné** – bez něj bot start odmítne (žádný JSON-only
režim, žádný tichý fallback). Discord je autorita aktuálního tieru,
PostgreSQL drží zrcadlo aktuálních tierů, historii, výsledky, fronty,
tickety, cooldowny, statistiky a audit. Schéma spravuje Alembic
(`alembic upgrade head`); bot migrace při startu spouští sám, takže nové
nasazení na hostingu bez konzole (Bot-Hosting/Pterodactyl) nepotřebuje nic
ručně. Vypnout to jde přes `AUTO_MIGRATE=0`.

`players.json` vzniká jen jako **generovaný export** z PostgreSQL pro
GitHub/web (`/sync web`); nikdy se nečte zpět.

### Převod starých JSON dat (jednorázově)

Data z `data/*.json` a z tabulky `dachshundtiers_data` (JSON dokumenty z
dřívějšího režimu) převede do normalizovaných tabulek jeden příkaz:

```bash
.venv/bin/python -m tools.legacy_import --preview   # záloha + import naprázdno
.venv/bin/python -m tools.legacy_import --apply     # záloha + import
.venv/bin/python -m tools.legacy_import --apply     # kontrola: „Beze změn“
```

- **Záloha vždy jako první** do `backups/legacy_import/<UTC čas>/`: zdrojové
  dokumenty, dump všech tabulek a `manifest.json` se SHA-256. Vedle se uloží
  `preview_report.json` / `apply_report.json`.
- **Preview** projde celý import v jedné transakci a vrátí ji zpět – nic
  se nezapíše. **Apply** stejnou transakci potvrdí.
- **Idempotentní** – opakované spuštění nic nezmění (přirozené klíče,
  výsledky se stejnými klíči jako za běhu bota: `result:{id}`,
  `ht_fight:{id}`).
- **Doplňuje, nepřepisuje.** Existující stav v DB má vždy přednost: delší
  cooldown se nezkrátí, odebraný eval se nevrátí, testeři se doplní jen do
  prázdné tabulky, existující hráč se nepřejmenuje. Aktuální tiery se
  neimportují – ty patří Discordu (`/sync discord`).
- **Tabulka má přednost před souborem**, pokud existuje stejný klíč v obou
  (rozdíl se vypíše jako konflikt).
- **Nic se neztratí** – každý zdrojový dokument se archivuje beze změny do
  `audit_logs` (`action = 'legacy_archive'`), i když nemá relační cíl.

Příkaz načítá `.env`, takže stačí mít v něm `DATABASE_URL` (nebo
`DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`).

**Hosting bez konzole (Bot-Hosting/Pterodactyl):** import spustí bot sám
při startu, hned po migraci schématu. Do `.env` přidej:

1. `LEGACY_IMPORT=preview` → restart → v konzoli zkontroluj výpis (počty,
   problémy, cestu k záloze). Nic se nezapíše.
2. `LEGACY_IMPORT=apply` → restart → data se naimportují.
3. Proměnnou odeber. Další běh by nic nezměnil, jen by dělal další zálohy.

`data/` a `backups/` obsahují osobní údaje hráčů a jsou v `.gitignore` –
nikdy je necommituj.
