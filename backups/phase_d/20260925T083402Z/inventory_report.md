# Phase D inventory — 2026-09-25T08:34:02.648117+00:00

- data dir: `/tmp/opencode/dachshundtiers-bot/data`
- files: cooldowns.json, ht3_cooldowns.json, kits.json, players.json, testers.json, testers_stats.json

## players.json

- total: 79
- s Discord ID: 0
- bez username: 0
- prázdný IGN: 0
- duplicitní IGN (casefold): 0
- kity: 7 (AnchorPvP, GoldSMP, IronAxe, NetheriteSword, RandomPot, ShieldlessSMP, UHCMace)
- history záznamů: 322
- tier kódy v datech: HT2, HT3, HT4, HT5, LT1, LT2, LT3, LT3 EVAL, LT4, LT5, RLT2
- nevalidní data (DD.MM.YYYY): 0

## cooldowns.json (waitlist)
- records: 4
- **všechny klíče jsou Discord ID bez relačního hráče** — players.json nese žádná Discord ID a relační players tabulka startuje prázdná; klíče zůstávají unresolved (nepřiřazené), dokud neproběhne /linkdiscord.

## ht3_cooldowns.json
- players: 31
- kit counts: {'AnchorPvP': 9, 'GoldSMP': 7, 'IronAxe': 12, 'NetheriteSword': 13, 'RandomPot': 3, 'ShieldlessSMP': 2, 'UHCMace': 9}

## kits.json
- 8 kitů: AnchorPvP, NetheriteSword, IronAxe, GoldSMP, UHCMace, RandomPot, ShieldlessSMP, MolePVP

## testers.json
- 5 ID (Discord, zatím neresolvovaná)

## testers_stats.json
- players: 15 (export-only)

## Do relačního schématu se NEimportuje

- `testers_stats.json` — agregované statistiky – žádný relační cíl (zůstává export-only)
- `players.json modes` — aktuální tiers z JSON NEJSOU autoritativní (Discord je jediná autorita); do mirroru ani tier_history se nekopírují
- `queue_channels.json / panel message soubory` — runtime konfigurace, ne hráčská data
