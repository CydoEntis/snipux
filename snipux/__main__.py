"""Entry point for `python -m snipux`."""

import sys

from snipux.handoff import cli

if __name__ == "__main__":
    # cli() holds the same dispatch rule the `snipux` console script uses
    # (see snipux/handoff.py, then snipux/app.py): a request forwarded to a
    # running snipux when one answers, otherwise arguments present -> the
    # display-free CLI diagnostic path, none -> the resident, tray-icon app.
    # Kept in one place rather than duplicated here.
    sys.exit(cli())
