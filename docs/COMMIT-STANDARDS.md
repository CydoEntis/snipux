# Commit standards

How changes are committed in this repository. This applies to people, to
Claude/Codex sessions, and to Tugboat runs alike. A commit is never made just
because code changed; it is made when someone asks for one.

## Message format

```text
<type>(<scope>): <subject>

<body -- only when the reason is not obvious from the diff>
```

Allowed types:

| Type       | Use it for                                                      |
| ---------- | --------------------------------------------------------------- |
| `feat`     | something a user can now do that they could not before          |
| `fix`      | something that was wrong and now is not                         |
| `refactor` | a change with no behaviour change (tests untouched)             |
| `perf`     | faster or lighter, same behaviour                               |
| `test`     | tests only                                                      |
| `docs`     | Markdown, docstrings-only changes, design notes                 |
| `style`    | formatting only                                                 |
| `build`    | `pyproject.toml`, `requirements.txt`, `packaging/`              |
| `ci`       | `.github/workflows/`                                            |
| `chore`    | releases, tooling config (`tugboat.config.ts`, `.tugboat/`)     |

The subject is at most 50 characters, starts lowercase, is in the imperative
mood ("add", not "added"/"adds"), and has no trailing period.

### Scopes

The scope is the area the change is about, usually the module name without
`.py`. Use the one that matches what a reader would look for:

| Scope        | Covers                                                        |
| ------------ | ------------------------------------------------------------- |
| `overlay`    | `overlay.py` -- selection, in-place annotation, the frozen frame |
| `chooser`    | `chooser.py` -- the pre-snip row                              |
| `flowbars`   | `flowbars.py` -- the post-selection bars                      |
| `shapes`     | `shapes.py`, `marks.py` -- annotation model, ink, undo        |
| `capture`    | `capture.py` -- frames and capture backends                   |
| `recording`  | `recording.py` -- recording backends                          |
| `player`     | `player.py` -- the trim editor and export                     |
| `review`     | `review.py` -- the review window                              |
| `pin`        | `pin.py` -- pinned snips                                      |
| `ocr`        | `sensitive.py`, `textsnap.py`, `platform/windows_ocr.py`      |
| `settings`   | `settings.py` and the config it edits                         |
| `tray`       | `app.py`'s tray menu and controller                           |
| `cli`        | `handoff.py` and `app.py`'s command line                      |
| `setup`      | `setup_desktop.py`, installers, shortcuts, autostart          |
| `platform`   | `platform/` -- use `platform` even for one OS                 |
| `design`     | `design/`, `glass.py`, `winchrome.py`, `docs/design/`         |
| `release`    | version bump + changelog                                      |
| `deps`       | adding or bumping a dependency                                |

When a change is genuinely about one OS, say so in the subject, not the scope:
`fix(platform): register the hotkey after a windows sleep`.

### Examples

```text
feat(overlay): add a spotlight redaction
fix(capture): keep the toolbar on the region's monitor
fix(recording): pause and resume on windows
refactor(shapes): move callout geometry out of the painter
test(player): cover gif export without ffmpeg
docs(architecture): record the platform seam rules
build(deps): require pyqt6 6.8 for qvideoframeinput
chore(release): 0.9.0
```

### The body

Only when the *why* is not visible in the diff: a compositor quirk, a
measured number, a platform constraint, a decision that looks wrong but is
not. Wrap at 72 columns. Do not list the files you changed -- the diff does
that.

```text
fix(capture): take the frame before the overlay maps

GNOME's opening animation is still running when the window first maps
on X11, so a grab taken after it catches the half-faded overlay.
```

Issue references go in the body or the PR, never in the subject:
`Closes #106`.

### Merge commits and pull requests

A PR title follows the same format, because a squash merge turns it into the
commit on `dev`. Tugboat's "Merge dev into: ..." commits on its own branches
are squashed away and never reach `dev`.

## Attribution

Commits and pull requests never carry AI or tool attribution. Do not add:

- `Co-Authored-By: Claude` / `Codex` / `ChatGPT`
- `Claude-Session:` or any session link
- `Generated with ...` / `Generated-By:`
- any equivalent footer

The git author and committer fields are the only authorship record. This
overrides whatever a tool's defaults say.

## Before committing

1. Look at `git status`, the staged diff and the current branch.
2. Refuse anything that must not be committed (see below).
3. Run the suite: `QT_QPA_PLATFORM=offscreen python -m pytest -q`.
4. If the change is something a user would notice, add a line to
   `CHANGELOG.md` under Unreleased in the same commit.
5. Split unrelated concerns into separate commits. Stage explicit paths --
   never `git add .` or `git add -A`.
6. Show the proposed message(s) and files, and wait for a yes.
7. Never push automatically.

## Safety rules

Never commit: `.venv/`, `build/`, `dist/`, `*.egg-info`, `__pycache__/`,
`.tugboat/tasks|trees|runs/`, `snipux.exe`, screenshots or recordings taken
while testing (they can contain whatever was on screen), `config.json` or a
hide list copied from a real machine, tokens, or keys. The PyPI token in
particular lives only in the shell that runs `twine`.

Protected branches are `main` and `dev`. Work happens on a branch off `dev`
and lands by pull request. A direct commit to a protected branch needs the
owner's explicit, one-time say-so -- except `chore(release)` on `main`, which
is how releases are cut (see `docs/releasing.md`).

Never use `git commit --amend` on pushed work, `--no-verify`,
`git reset --hard`, `git clean -f`, or a force-push to `main`/`dev`.
