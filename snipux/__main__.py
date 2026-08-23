"""Entry point for `python -m snipux`."""

import sys

from snipux.app import cli

if __name__ == "__main__":
    # cli() holds the same dispatch rule the `snipux` console script uses
    # (see snipux/app.py): arguments present -> the display-free CLI
    # diagnostic path, none -> the resident, tray-icon app. Kept in one
    # place rather than duplicated here.
    sys.exit(cli())
