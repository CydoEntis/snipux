# Code standards

`CLAUDE.md` is the operating contract; this file explains the practices in
more depth. Where they disagree, `CLAUDE.md` wins.

## Language and toolkit

- **Python 3.10+.** `X | None`, `match`, and `from __future__ import
  annotations` are all fine.
- **PyQt6, with fully scoped enums**: `Qt.PenStyle.DashLine`, never
  `Qt.DashLine`.
- **Dependencies are PyQt6, jeepney and pytest.** A fourth is a decision for
  the issue, not a detail of the change. No numpy, Pillow or OpenCV -- Qt does
  image work. `ffmpeg` is optional, found on `PATH` if present, and never
  imported, required or installed.
- Talk to the OS with what ships with it: `ctypes` for Win32, `jeepney` for
  D-Bus, PowerShell for WinRT. No `pywin32`, no `comtypes`.

## Naming

- `snake_case` for modules, functions, variables; `PascalCase` for classes;
  `UPPER_SNAKE` for module constants. Private helpers start with `_`.
- Precise domain names over vague ones: `selection_rect`, `frozen_frame`,
  `recording_folder` -- not `data`, `item`, `thing`, `helper`, `manager`.
- Booleans read as assertions: `is_recording`, `has_selection`,
  `can_pin`.
- Functions read as verbs: `capture_frame`, `save_image`, `load_hide_list`.
  Config accessors come in `load_<key>` / `save_<key>` pairs.
- Qt signals are past-tense events: `copy_clicked`, `region_changed`.
- One file, one responsibility. A module that has grown two is a refactor
  ticket, not a reason to add a third.

## Coordinates

The sharpest edge in this codebase. Every value that is a position or size
says which space it is in, in its name or a comment:

- **logical** vs **physical** pixels (`devicePixelRatio`),
- **screen-local** vs **virtual-desktop**,
- **widget** vs **frame** (the frozen image).

`rect_physical`, `pos_in_frame`, `screen_local_x`. A value used in the wrong
space is the most common bug here, and fractional scaling hides it on a
developer's 1.0 display -- which is why the suite must also pass at
`QT_SCALE_FACTOR=1.5`.

## Functions and abstractions

- One level of abstraction per function. Guard edge cases early; avoid deep
  nesting and nested conditional expressions.
- Extract an abstraction when there are two real uses, or when it clearly
  removes complexity. No speculative layers, plugin systems or settings
  nobody reads.
- Prefer the version a new contributor can read.
- Side effects (files, clipboard, subprocesses, D-Bus, Win32) happen at the
  edges -- `app.py`, backends, `setup_desktop.py`, `platform/` -- where a test
  can swap them. Take a `runner=`/`config_dir=` style parameter rather than
  reaching for a global.

## Types and values

- Type-hint public functions and methods. Prefer precise types
  (`tuple[int, int, int, int]`, `Path`) over `Any`.
- `@dataclass` (frozen where it makes sense) for plain records; `enum.Enum`
  for fixed choices instead of bare strings.
- Name meaningful constants rather than repeating magic numbers -- and say
  where the number came from when it was measured.
- Don't mutate a caller's object in place unless that is the function's job.
- Paths are `pathlib.Path`. Never build one with string concatenation.

## Errors

- **A failure that has a safe outcome returns it**, rather than raising: a
  corrupt `config.json` reads as `{}`, OCR that cannot run finds no words, a
  config write that fails returns `False`.
- Catch the specific exceptions you expect (`OSError`, `ValueError`,
  `subprocess.SubprocessError`). No bare `except:`; `except Exception` only
  at a boundary that must keep the app alive, and it must log.
- **A capture or recording backend that fails must not stop the next one.**
  Collect every failure and report them together.
- A message a user sees says what happened and what to do, not a traceback.

## Qt specifics

- **Never leave a `QPainter` open across a read of the pixmap it paints.**
  End the painter first. The obscuring tools depend on this.
- Views emit signals; the controller in `app.py` decides what they mean.
- Keep work off the paint path: cache what is expensive (`glass.py` exists
  for this).
- Never block the event loop on a subprocess or D-Bus call longer than a
  frame without a reason written next to it.

## Comments and docstrings

- **Comments say why, not what.** A comment that restates the line is noise;
  one explaining a compositor quirk, a measured number or an ordering
  constraint is why the file is maintainable.
- Module docstrings explain what the module is for and anything a reader
  would get wrong without being told.
- No ticket numbers as the *only* explanation. `# SNX-102` tells a reader
  nothing; say what the bug was.

## Tests

- pytest, one test file per module, run headless:
  `QT_QPA_PLATFORM=offscreen python -m pytest -q`, and again with
  `QT_SCALE_FACTOR=1.5`.
- Test painting with `QWidget.grab()`; it runs a full `paintEvent` offscreen.
- Test behaviour, not implementation. **Never compute the expected value by
  calling the code under test.**
- Don't seed state the app never sets; start from what `app.py` builds.
- A test must not depend on this machine's fonts: measure with the
  `QFontMetrics` of the font in use and leave slack when counting text pixels.
- A path that only works on one OS is tested by faking `platform.current` and
  the backend, so it answers the same on Ubuntu and Windows CI. Never call real
  OCR, a real recorder or a real `ffmpeg` from a test.
- A bug fix comes with a test that failed before it.
- Never weaken or skip a legitimate test to get green. A skip states why.
- **A green suite is weak evidence here.** Anything interaction-shaped, or
  anything that produces a file, gets checked against the real thing too --
  see `CONTRIBUTING.md`.

## Change discipline

- One ticket, one change. Leave unrelated problems for their own ticket.
- A user-visible change adds a line to `CHANGELOG.md` under Unreleased.
- Before "fixing" something back toward a design handoff, read that
  directory's `divergences.md`.
- Don't revert anything under TODO.md's "Decisions -- deliberate, do not
  revert" without saying why in the PR.
- Before handing off, look at the diff and `git status`, and report what was
  actually run and what it showed.
