"""``python -m src`` runs the same CLI as ``python -m telegram_archive``."""

import sys

from telegram_archive.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
