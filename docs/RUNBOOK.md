# Provozní runbook

Postupy pro nasazení a řešení incidentů. Konfigurace je výhradně přes env
proměnné (viz [README](../README.md#konfigurace) a `.env.example`).

## Nasazení

**Docker**

```bash
docker build -t dachshundtiers-bot .
docker run -d --name dachshundtiers --restart unless-stopped \
  --env-file .env dachshundtiers-bot
```

`.env` se do image nekopíruje (`.dockerignore`). Healthcheck v image jen ověří,
že jde aplikaci naimportovat – nekontroluje spojení s Discordem ani DB. Skutečný
stav DB/zrcadla ukazuje `db/services/health.py` (health report mirroru).

**Bez Dockeru (hosting / systemd)**: `pip install -r requirements.txt`, spustit
`python main.py` (stejné jako `python bot.py`).

**Postup aktualizace**
1. Zálohuj databázi (viz níže).
2. Nasaď novou verzi kódu a restartuj bota. Při `AUTO_MIGRATE=1` (default)
   spustí bot `alembic upgrade head` sám.
3. Zkontroluj log: úspěšná migrace, registrace příkazů, žádné chyby ve validaci rolí.

> `AUTO_MIGRATE=1` při více instancích najednou (např. překryv při deployi)
> může spustit migraci souběžně. Při rolling deployi nech zapnuté jen u jedné
> instance, ostatním dej `AUTO_MIGRATE=0`.

## Zálohy a obnova

**PostgreSQL (zdroj pravdy).** Standardní zálohu dělej na úrovni databáze:

```bash
pg_dump --format=custom --file=backup-$(date -u +%Y%m%dT%H%M%SZ).dump "$DATABASE_URL"
```

Dumpy obsahují osobní údaje – neukládej je do gitu (`*.sql` a `backups/` jsou
v `.gitignore`).

Obnova do prázdné databáze:

```bash
createdb dachshundtiers_restore
pg_restore --no-owner --dbname=dachshundtiers_restore backup-XXXX.dump
# přepni DATABASE_URL na obnovenou databázi a restartuj bota
```

Po obnově spusť v Discordu `/sync discord`: Discord je autorita aktuálních
tierů, zrcadlo se z něj přepočítá.

**Legacy JSON záloha** (`services/phase_d/backup.py`) zálohuje jen adresář
`data/` se starými JSON soubory (SHA-256 manifest), ne PostgreSQL:

```bash
python -m services.phase_d.cli backup            # záloha do backups/phase_d/<UTC čas>/
python -m services.phase_d.cli list-backups
python -m services.phase_d.cli restore --latest --verify-only   # ověření bez zápisu
```

`tools.legacy_import` si před importem dělá vlastní zálohu do
`backups/legacy_import/<UTC čas>/`.

## Rollback migrace

```bash
alembic current                  # aktuální revize
alembic history --verbose        # řetěz migrací
alembic downgrade -1             # o jednu zpět (nebo konkrétní revize)
```

Před downgradem vždy `pg_dump`. Downgrade může smazat sloupce/tabulky přidané
migrací (např. `tester_credits`, `tournaments`, Minecraft identity). Po
downgradu nasaď zpět odpovídající verzi kódu a nastav `AUTO_MIGRATE=0`, jinak
ji bot při startu hned upgraduje.

## Výpadek databáze

1. Bot bez DB nenastartuje (`DATABASE_URL` je povinné). Běžící bot při výpadku
   hlásí chyby u příkazů; data se nezapisují, nic se neztrácí.
2. Ověř dostupnost: `psql "$DATABASE_URL" -c 'select 1'`.
3. Po obnovení DB restartuj bota, aby se znovu registrovala persistentní tlačítka
   a obnovil stav (fronty, tickety, turnaje).
4. Outbox události, které selhaly během výpadku, zpracuje hodinová reconciliace
   (po 5 minutách se znovu převezmou rozdělané). Událost, která vyčerpala pokusy,
   skončí ve stavu `dead_letter` – prohlédni tabulku `outbox_events`.
5. Po výpadku spusť `/sync check` a `/sync discord`.
6. Při `Network is unreachable` na IPv6 nastav `DB_HOSTADDR` (IPv4).

## Rotace Discord tokenu

1. Developer Portal → Bot → *Reset Token*.
2. Aktualizuj `DISCORD_TOKEN` v prostředí nasazení (secret manager / `.env`).
3. Restartuj bota. Starý token je okamžitě neplatný.
4. Pokud token unikl (log, git), proveď rotaci hned a zkontroluj audit log serveru.

Stejně rotuj `GITHUB_TOKEN` (GitHub → Settings → Developer settings) a heslo DB
(`DATABASE_URL`/`DB_PASSWORD`). Tokeny nikdy neukládej do gitu ani do logů.

## Tipy na diagnostiku

- `/dbstatus` – stav připojení k DB.
- `/verze` – verze/commit běžícího bota.
- `/sync check` – read-only diagnostika Discord × DB × web.
- Při neznámém stavu zrcadla: `/sync discord` (observe-only, role nemění).
