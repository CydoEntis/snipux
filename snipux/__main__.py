"""Entry point for `python -m snipux`."""

import sys

from snipux.app import main, run_resident_app

if __name__ == "__main__":
    # With arguments (e.g. --list-backends), keep the display-free CLI
    # diagnostic path. With none, launch the resident, tray-icon app --
    # bare `python -m snipux` is how it's actually meant to be run.
    if sys.argv[1:]:
        sys.exit(main())
    else:
        sys.exit(run_resident_app())
