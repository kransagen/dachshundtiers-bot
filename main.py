"""Vstupní bod pro hosting (startup soubor = ``main.py``).

Většina hostingových platforem (Bot-Hosting, Pterodactyl + Python egg, ...)
má výchozí startup soubor ``main.py``. Tento soubor jen spouští stejnou
logiku jako ``python bot.py``.
"""

from bot import run

if __name__ == "__main__":
    run()