"""Služby na úrovni aplikační logiky (bez závislosti na discord.py).

Moduly v tomto balíčku se dají testovat bez nainstalovaného discord.py:
- ``store``        – async úložiště s per-souborovými zámky a transakcemi,
- ``queue_service``– čistá logika front + transakční operace nad nimi,
- ``tickets``      – HT ticket systém (vytvoření, claim, členové, close/reopen,
-                    log událostí) + čisté tier pomocné funkce,
- ``permissions``  – oprávnění testerů/adminů podle ID rolí.
"""