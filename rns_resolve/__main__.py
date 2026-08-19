"""Entry point: python -m rns_resolve <query> [options]. See client.py."""

import sys

from .client import main

if __name__ == "__main__":
    sys.exit(main())
