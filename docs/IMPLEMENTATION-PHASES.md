# Implementation phases

How work moves from an issue to a release, and the order the open issues
should be worked in. Update the queue below when issues are opened or closed.

## The flow

```text
GitHub issue (assigned to the owner, no needs-desk label)
  -> Tugboat branch  tugboat/<number>-<slug>  off dev
    -> verify: the full suite, headless
      -> pull request into dev  (title in commit format)
        -> owner reviews and squash-merges
          -> CI green on dev (Ubuntu + Windows)
            -> owner drives it on a real desktop
              -> release: dev -> main, version bump, changelog, tag
```

- `dev` is the integration branch. `main` moves only on a release, and `main`
  is what `pipx install git+...` installs, so it must always work.
- Tugboat runs with `concurrency: 1` and `chain: ""` -- one ticket at a time,
  no automatic merging. A ticket that depends on another starts only after
  that one is **merged into `dev`**, not merely opened as a PR.
- Issues labelled `needs-desk` need a person watching a real screen and are
  never pulled by Tugboat.

## Phase 0: a green baseline

Before an unattended run, the verify command passes on a clean `dev`:

```sh
uv run --no-project --with-requirements requirements.txt python -m pytest -q
```

Don't start a batch on a red base, and never weaken the verify command to get
green.

Run it again with `QT_SCALE_FACTOR=1.5`; CI runs 1.5 on Ubuntu only. Don't run two
suites at once from different worktrees: the single-instance socket tests
share one server name and will trip over each other.

## Phase 1: done -- the 2026-09-16 Tugboat run

Merged to `dev` (#94-#103), not yet released:

1. #82 Copy the text out of a snip
2. #83 Pin a snip on top of the screen
3. #84 Eyedropper
4. #85 Recent captures in the tray menu
5. #86 Land a recording as a GIF
6. #87 Pause and resume on Windows
7. #88 Nudge the selection with the arrow keys
8. #89 Callout
9. #90 Spotlight
10. #91 Open an image you already have

**Pause here for a hands-on review on Windows and on Linux**, then release
as 0.9.0.

## Phase 2: follow-ups from Phase 1

Small, and they touch the same code Phase 1 just changed, so do them before
anything new lands on top.

1. #106 Keep the eyedropper's loupe clear of the bar (bug)
2. #105 Point a callout at something, then place its words

## Phase 3: Linux parity

1. #93 Pause and resume a recording on Linux
2. #92 Read text on Linux with a system tesseract -- adds an optional
   system tool; the same rules as `ffmpeg` apply (found, never required)

Pause for a review in the Ubuntu VM, on both Wayland and X11.

## Phase 4: new surface

1. #107 Recent captures as thumbnails in a window (builds on #85)

## Needs a person

- #81 Find the page inside a browser window on Linux X11 (`needs-desk`)
- TODO.md "Windows: four jobs", job 3 -- watch the flow end to end

## Operating rules

- One worker. Shared files -- `overlay.py`, `app.py`, `flowbars.py` -- are
  changed serially.
- Automatic merging stays off until Tugboat can wait for each merge before
  starting the next ticket.
- Review and merge one PR at a time; drive the app at each pause.
- Every PR follows `docs/COMMIT-STANDARDS.md`, including its title.
