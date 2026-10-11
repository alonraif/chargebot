"""Compatibility entry point, so existing deploys that run ``python chargingbot.py`` keep working.

The bot now lives in the ``chargebot`` package; see README.md.
"""

from chargebot.__main__ import main

if __name__ == "__main__":
    main()
