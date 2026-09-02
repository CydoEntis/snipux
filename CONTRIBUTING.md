# Contributing to Snipux

Issues and pull requests are welcome. This page is short on process and long on
the two or three things that will actually get a change rejected here.

## Getting set up

There is no system PyQt6 on most distributions, so development needs a virtual
environment:

```sh
python -m venv .venv
source .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Run the suite the way CI does — headless, because a build machine has no
display:

```sh
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

It must also stay green under fractional scaling:

```sh
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python -m pytest -q
```

**1.5 is the factor to keep green.** 1.25 and 2.0 leave one or two failures that
are inherent to sampling a 1px antialiased line at a fractional boundary, not
defects.

Widgets are testable headless: `QWidget.grab()` runs a full `paintEvent` into an
offscreen pixmap without showing anything, and that's the preferred way to test
painting code.

## The one architectural rule

**Capture the entire virtual desktop in a single shot, then run selection
against that frozen frame in our own overlay.** The OS is involved for exactly
one instant. This is not negotiable.

A capture backend that asks the OS for pixels *while the user is dragging* is
platform-specific at the one moment that matters, because every OS's
live-interaction APIs differ. Everything downstream of the grab — selection,
chrome, annotation — is ordinary drawing on an image already in memory, and
ordinary Qt runs unchanged wherever PyQt6 does. That split is the reason the
same overlay/marks/shapes code behaves identically on X11 and Wayland, and the
reason the Windows port touched roughly 210 lines rather than the whole
codebase.

Recording follows the same rule: the frozen frame is still how you choose, and
recording starts once you have chosen.

## A green test suite is weak evidence here

This matters more than anything else in this file, and it is not a platitude —
it is the repeated, measured experience of this codebase.

**1,444 tests passed while the primary Linux and Wayland recording path could
not have worked even once.** Every one of its four independent faults was
invisible to a suite that mocked the D-Bus connection. Worse, the tests actively
*defended* two of them: one had a test named for the wrong belief, with a
comment explaining it, and two clipboard tests computed their expected value by
calling the same function the implementation called — comparing the
implementation to itself, so they would have passed whatever it did.

Habits that actually catch things here:

- **Measure the artifact, not the assertion.** Record for a known interval and
  check the file's real duration with `ffprobe`.
- **Never compute a test's expected value with the call under test.** That is
  not a test, it is a restatement.
- **Read the thing back out of the real system.** `xclip` on the actual
  clipboard, `ffprobe` on the actual file, D-Bus introspection on the actual
  session — each of those has found a bug that mocks had hidden.
- **Don't seed state the app never sets.** Start from what `app.py` builds.
- **For anything interaction-shaped, photograph or record the screen.** Twelve
  real faults in the capture flow were found by using the app and none by the
  suite. One chooser bug took four wrong guesses from reading code and two
  minutes from a video.
- **A sweep that varies one thing still has to be run in more than one order.**
  A size sweep once "proved" 640×360 broken and 1280×720 fine, then proved the
  exact opposite when run in reverse. Nothing about the sizes mattered.

## Conventions

- **Python 3.10+, PyQt6.** Qt6 enums are fully scoped: `Qt.PenStyle.DashLine`,
  never `Qt.DashLine`.
- **Dependencies are PyQt6, jeepney and pytest.** Adding a fourth is a decision
  worth raising in the issue, not a detail. Notably: no numpy, no Pillow, no
  OpenCV — Qt already does image work, and a screenshot tool that drags in a
  numerical stack has made a bad trade. `ffmpeg` is the one *optional*
  exception and is not a dependency: never import it, never require it, never
  install it.
- **Comments say why, not what.** A comment restating the line above it is
  noise; a comment explaining a compositor quirk or a non-obvious ordering
  constraint is the reason the file is maintainable.
- **Coordinates are the sharp edge here.** Be explicit about which space a value
  is in — logical vs physical pixels, screen-local vs virtual-desktop — and say
  so in the name or a comment. Most bugs in a tool like this are a value used in
  the wrong space, and fractional scaling makes them invisible on a developer's
  machine.
- **Platform-specific code lives in `snipux/platform/`.** That package is the
  one place `sys.platform` is read. Everything else is portable PyQt6.
- **A capture backend that fails must not stop the next one.** Backends are
  tried in order; each failure is collected and reported together.
- **Never leave a `QPainter` open across a read of the pixmap it is painting.**
  Reading a pixmap mid-paint is not guaranteed to see pending strokes. The
  obscuring tools depend on this and it has already caused one bug.

## Before you open a pull request

1. Both test runs above are green.
2. New behaviour is verified against something real, not only against the suite
   — see above.
3. If you changed anything with a locked design handoff under `docs/design/`,
   check that directory's `divergences.md` first. A "fix" back toward a handoff
   may be undoing a deliberate, recorded decision.
4. If you changed a decision recorded in `TODO.md` under "Decisions — deliberate,
   do not revert", say why in the PR.

## Adding a platform

`snipux/platform/` is an ABC plus one implementation per OS. `linux.py` and
`windows.py` are real; `darwin.py` is a stub that raises
`UnimplementedPlatformError` naming both the platform and the operation for
everything not built yet. Its module docstring is the guide.

Desktop integration, global-shortcut binding, the default save folder, and which
capture and recording backends an OS may even try are all decided there. A new
OS should need changes nowhere else — if it does, that's worth raising in the
issue, because it usually means something downstream of the frozen frame has
picked up a platform assumption it shouldn't have.

New platform code must pass headless too, the same way `tests/test_platform.py`
already runs against `windows.py` and `darwin.py` without a display.

## Licence

By contributing you agree that your contributions are licensed under the
[MIT Licence](LICENSE), the same as the rest of the project.
