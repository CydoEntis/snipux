# Environment

snipux needs **no secrets and no API keys**. Its only network requests are
the daily update check and, if the user chooses to update, the download of
that release -- both unauthenticated, to a public API. There is no `.env`
file and there should not be one. User preferences belong in `config.json`
(see `docs/STORAGE.md`), not in environment variables.

## Setting up

```sh
python -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
python -m pip install -r requirements.txt
python -m snipux                   # run it
```

Or, with no venv at all (this is what Tugboat does):

```sh
uv run --no-project --with-requirements requirements.txt python -m pytest -q
```

Linux CI also needs Qt's system libraries: `libegl1 libgl1
libxkbcommon-x11-0 libdbus-1-3 libpulse0 libgstreamer1.0-0
libgstreamer-plugins-base1.0-0` (see `.github/workflows/ci.yml`).

## Variables snipux reads

Read at the edges only -- `capture.py`, `setup_desktop.py`, `platform/` --
never scattered through views.

| Variable            | Read by              | Used for                                   |
| ------------------- | -------------------- | ------------------------------------------ |
| `XDG_SESSION_TYPE`  | capture, setup, linux | Wayland vs X11 (never assumed)            |
| `WAYLAND_DISPLAY`, `DISPLAY` | setup_desktop | backing up the session guess            |
| `XDG_CONFIG_HOME`   | setup_desktop        | config folder, autostart entry             |
| `XDG_DATA_HOME`     | setup_desktop        | desktop entry, icons                       |
| `APPDATA`           | platform/windows     | Start Menu and Startup shortcuts           |
| `LOCALAPPDATA`      | platform/windows     | where the exe relocates itself, the `.ico` |

A missing variable falls back to the platform's documented default; it is
never an error.

## Variables for development and tests

| Variable                  | Why                                                        |
| ------------------------- | ---------------------------------------------------------- |
| `QT_QPA_PLATFORM=offscreen` | run with no display. `tests/conftest.py` sets it by default. |
| `QT_SCALE_FACTOR=1.5`     | the second required test run -- catches coordinate-space bugs |
| `QT_QPA_FONTDIR`          | `conftest.py` points it at `C:\Windows\Fonts` on Windows, where the offscreen plugin otherwise finds no fonts at all |

## Tests and the real machine

- Tests pass explicit, deterministic configuration: `config_dir=tmp_path`,
  a faked `platform.current`, faked backends.
- A test never reads the developer's real config, captures their real
  screen, uses the real clipboard, or runs real OCR, a real recorder or a
  real `ffmpeg`.
- The suite must pass headless on Ubuntu and on Windows. A local green run
  proves one platform only.

## Secrets

The only credential this project ever touches is a PyPI token, and only
when publishing (`docs/releasing.md`). It lives in the shell that runs
`twine`, as `TWINE_PASSWORD`, and never in a file in this repository. The
same goes for any GitHub token Tugboat or `gh` uses.

Screenshots and screen recordings can contain anything that was on screen.
Treat ones made while testing like secrets: don't commit them, don't attach
them to public issues without looking first.
