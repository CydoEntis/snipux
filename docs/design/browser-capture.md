# Browser-aware capture

Capture what a browser tab is showing, and — later — the whole page it can
scroll to.

This is the plan and the decisions behind it. Status lives in the tracker;
what is here is the shape of the work, the constraints already measured, and
what would make each piece not worth doing.

---

## Why it is two features, not one

They read as one request — "capture this tab, or the whole page" — and share
almost no machinery.

**Capturing what a tab is showing** is a rectangle lookup. The browser is a
window, the viewport is a child window inside it, and the existing capture
path takes a rectangle. It fits the architecture as it stands.

**Capturing a whole page** is a scroll loop: move the page, wait, grab, repeat,
stitch. The OS is involved N times over several seconds, and the result is
assembled rather than captured.

Shipping the first does not commit us to the second, and the second is worth
starting only if the first proves the detection is reliable.

---

## What the architecture already says

CLAUDE.md's one rule is that we capture the entire virtual desktop **in a
single shot** and run selection against that frozen frame — "the compositor/OS
is involved for exactly one instant. This is not negotiable."

Scroll capture cannot obey that, and the project already knew:

> Scrolling / full-page capture is a later milestone. Do not build toward it
> speculatively, but do not make it impossible either — the capture layer
> should not assume one frame per session.

So SNX-2 below is a *sanctioned exception*, not a violation — but it is an
exception, and it earns a capture mode of its own rather than being smuggled
into the existing one.

---

## What has been measured

Spiked on a real three-monitor Windows desktop with Brave running, against the
existing `WindowsWindowGeometryProvider`.

**Browser windows are identifiable.** `EnumWindows` + `GetWindowTextW` (which
`capture.py` already calls for the Window-mode hover preview) plus
`QueryFullProcessImageNameW` for the process name:

```
brave.exe    (3) Wrong Spot, Wasted Stone: A Castle Placement Fail — AoE2 …
brave.exe    Amazon.com : bathroom side sink organizer - Brave
Discord.exe  @Ape - Discord
```

The window title **is** the focused tab's title. Nothing extra is needed to
know which tab is in front.

**The viewport rect is available exactly.** Chromium creates a child window of
class `Chrome_RenderWidgetHostHWND` for the page. Measured:

```
frame  (2552,-8) 2576x1408
  render widget  visible=False  rect=(2560,0)   chrome_height=8
  render widget  visible=True   rect=(2560,81)  chrome_height=89
```

Two of them exist and **`IsWindowVisible` picks the right one** — the visible
one starts below the tab strip and toolbar (89px of chrome here). Confirmed
identical on a second window. No UI Automation, no COM, no new dependency:
`EnumChildWindows` + `GetClassNameW` + `GetWindowRect`, all of which the
codebase already uses.

**What was not measured, and matters:** Firefox is not Chromium and has no
`Chrome_RenderWidgetHostHWND`. Every number above is Chromium-family only.

**And one thing the spike found by accident.** `WindowsWindowGeometryProvider`
performs no physical-to-logical conversion anywhere — grepped, zero
references to a pixel ratio or `GetDpiForWindow` — while `GeometryProvider`
is documented to return *absolute logical* rects. `GetWindowRect` returns
physical pixels. On this 1.0-scale desk the two are numerically identical,
so every number above is right by coincidence and Window mode already works.
On a 125%/150% display it should be wrong today, before any of this. That is
SNX-3, and SNX-1 is blocked on its answer, because SNX-1 reads rects from the
same provider.

---

## The constraint that shapes everything: Wayland

Wayland compositors do not expose window titles, window geometry, or other
applications' windows to a client. That is why `GeometryProvider` already
degrades to `UnsupportedGeometryProvider` there and why Window mode is not
offered.

Browser detection is the same class of problem, so **none of this can work on
Wayland** — which is snipux's *primary* Linux target. It is Windows and X11
only, and the feature must be absent rather than broken there: the chooser
already knows how to grey a mode out with a reason, and that is the required
behaviour, not a nice-to-have.

This is the strongest argument for keeping the scope small. A headline feature
that is missing on the platform the project cares most about should not also
be the most expensive thing in the codebase.

---

## SNX-1 — Capture the focused browser tab

**Value.** "Capture this tab" without framing a rectangle by hand, and without
the browser's own chrome in the shot. This is most of what people mean, and it
needs no new capture machinery.

### Scope

- Extend the geometry provider to report a window's **process name** alongside
  the title it already returns.
- Add viewport lookup: for a Chromium-family process, the visible
  `Chrome_RenderWidgetHostHWND` child rect; otherwise `None`.
- A chooser mode — working name **Tab** — that selects that rect immediately,
  the way Full screen selects a monitor. No drag.
- Greyed out with a reason when there is no browser in front, when the platform
  cannot answer (Wayland), or when the browser is not Chromium-family.

### Not in scope

- The page **URL**. It needs UI Automation on Chromium and is not required to
  capture anything. If a filename or an annotation ever wants it, that is its
  own ticket with its own justification.
- Firefox. It has no equivalent child window; `None` and a greyed row is the
  correct answer until someone measures a route.
- Picking a tab other than the focused one.

### Acceptance

- With a Chromium browser focused, the mode selects the page area only —
  no tab strip, no toolbar, no bookmarks bar.
- With no browser focused, the row is greyed and says why.
- On Wayland, and on any platform whose provider cannot answer, likewise.
- Headless tests: a fake provider drives every branch. Nothing in the suite may
  depend on a browser actually running — the same rule SNX-126 set.

### What would make this not worth doing

If the viewport rect turns out to be unreliable across window states — a
maximised window, a second monitor at a different scale factor, a browser in
full-screen — then "capture this tab" is a mode that sometimes silently crops
wrong, which is worse than framing it by hand. **Measure those three cases
before writing the mode**, not after.

---

## SNX-2 — Full-page (scrolling) capture

> **Status: built, then withdrawn from the UI in 0.5.0.** The engine
> (`snipux/stitch.py`, `snipux/scroll.py`) is finished and tested, and end
> to end it scrolled a real page and joined six frames into one clean
> 2811px image. It is off the chooser because that only holds for a page
> that sits still.
>
> `stitch.find_overlap` locates a join by finding runs of **pixel-identical**
> rows. Anything that redraws itself between grabs — a live chart, an
> animated ad, a playing video — breaks every run. Measured on a real site
> with live content, scrolls of 1, 2, 3, 5 and 9 notches *all* reported no
> overlap; measured on a page with playing video, only 3.5% of pixels had
> actually changed. The information is there. The matcher is too strict to
> use it.
>
> **What finishes this:** a tolerant match — accept an overlap when the
> great majority of rows line up, instead of demanding all of them. That is
> one function, not a rewrite, which is why the modules and their 36 tests
> are kept rather than deleted. Everything below still describes the
> intended feature.

**Value.** The whole page, not the part that fits. The single most-requested
thing screenshot tools get asked for.

**This is the expensive one.** It should not start until SNX-1 has shipped and
the detection has held up in real use.

### Why the obvious shortcut does not work

Chromium's DevTools Protocol has `Page.captureScreenshot(captureBeyondViewport:
true)`: a perfect full-page image, no scrolling, no stitching, no artifacts.

It requires the browser to have been started with `--remote-debugging-port`.
You cannot attach to an already-running browser that was not. The routes from
there are "ask users to restart their browser with a flag" (nobody will) or
ship a browser extension plus a native-messaging bridge (a second product,
store review, and a per-browser build). **Neither is a screenshot tool's job.**

So: scroll and stitch, and be honest that it is an approximation.

### Shape

A capture *session* rather than a capture: the frozen-frame overlay must stand
down while the page scrolls, because it is covering the thing being scrolled.
That is a new flow, and the reason this is not a variation on an existing mode.

1. Identify the viewport (SNX-1's work — the dependency is real).
2. Dismiss the overlay.
3. Scroll to the top; grab; scroll by less than a viewport; wait for the paint
   to settle; grab; repeat.
4. Stop when a grab is identical to the one before it — that is the bottom.
5. Stitch on measured overlap, not on the scroll amount requested: smooth
   scrolling, per-user scroll settings and page zoom all make the requested
   amount a lie.
6. Present the assembled image through the normal post-capture flow.

### The parts that will hurt

- **Stitching without numpy or OpenCV**, both banned by CLAUDE.md. Finding the
  vertical offset between two `QImage`s in pure Python is a search over
  candidate offsets comparing a few scanlines. It is tractable for the ~10–30
  frames a page needs, and it is the moment to reopen the dependency question
  honestly rather than quietly.
- **Sticky headers and footers** repeat in every frame and stitch into
  duplicate bands. Rows that never change across frames are the signal, and
  cropping them from all but the first is the fix. Heuristic, and it will be
  wrong sometimes.
- **Lazy loading, infinite scroll, animation, video.** Some pages cannot be
  captured this way at all. The mode must say so when it detects the page never
  stops growing, rather than scrolling forever.
- **Page zoom and fractional display scaling** change the pixel-to-scroll
  relationship. Coordinates are already this project's sharp edge.

### Acceptance

- A long static page stitches with no visible seam and no duplicated header.
- A page that fits in one viewport produces exactly the single-frame result.
- A page that never stops growing stops, and says why.
- Cancellable mid-scroll, leaving the page where it was found.
- Headless tests drive the stitcher on synthetic frames with known overlap,
  including a sticky band. The scroll driver is injected, the way
  `run_update`'s subprocess call is, so no test needs a browser.

### What would make this not worth doing

If the stitcher cannot beat "take three screenshots by hand and paste them
together" on the pages people actually capture, it is a large, permanently
fiddly surface that produces subtly wrong images. **Build the stitcher first,
against saved frames, and judge it before building the scroll driver.**

---

## Order

1. **SNX-1**, and the three window-state measurements its risk section names,
   before any mode is written.
2. Ship it. Use it. See whether tab detection is right often enough to trust.
3. **SNX-2**, stitcher first, against saved frames, judged before the scroll
   driver exists.

The dependency runs one way: B2 needs B1's viewport lookup. B1 needs nothing
from B2 and is worth having on its own.
