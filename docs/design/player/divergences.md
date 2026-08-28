# Player handoff — where the build departs from the spec

`docs/design/player/reference/Snipux Player.dc.html` is the authority. Each entry
below is a deliberate departure, with the reason. Anything not listed here is a
bug, not a decision.

---

## 1. Export uses the system `ffmpeg` when there is one

**Handoff:** four formats, MP4 (H.264) the default.
**Built:** exactly that -- *if* `ffmpeg` is on PATH and can encode
`libx264`, `libvpx-vp9` and `gif`. Otherwise MP4 falls back to MPEG-4
Part 2, and GIF and trimmed WebM become disabled rows.

snipux does not depend on ffmpeg, does not install it and does not require
it. `system_ffmpeg()` looks for one, asks the binary what it can encode
and caches the answer; everything works without it, one codec down.

The reason it is worth reaching for: **the bundled FFmpeg cannot encode
H.264 in software.** `libavcodec.so.61` as shipped with PyQt6 6.11 has
exactly `h264_nvenc` and `h264_vaapi` and no `libx264` -- an LGPL build,
and x264 is GPL. And Qt cannot reach the hardware either: on this box
(RTX 4060, driver 580.173.02) Qt's NVENC path fails at probe with `10 bit
encode not supported` / `No capable devices found`, and VAAPI fails the
same way, while **the same machine's system ffmpeg encodes H.264 through
`h264_nvenc` without complaint**. The hardware is fine; Qt's use of it is
not. When it fails Qt does not fall back -- `QMediaRecorder` reports
"Could not initialize encoder" and leaves a zero-length file, which is why
`snipux/__init__.py` disables hardware encoding for the Qt path.

The menu row says which one will run: "MP4 (H.264) -- plays anywhere" with
ffmpeg, "MP4 (MPEG-4) -- no H.264 encoder here, desktop players only"
without. A row must not promise a browser a file that will not play in
one.

Two things the real files taught us, both now covered by tests:

- **A snip is whatever rectangle was dragged, and half of them are odd.**
  The first real export attempted here was 983x680 and libx264 refused it
  outright -- H.264 in yuv420p cannot encode an odd dimension. Every MP4
  is scaled with `trunc(iw/2)*2`, losing at most one row rather than
  inventing one.
- **GNOME writes `r_frame_rate=1000/1`**, a millisecond timebase rather
  than a thousand frames a second, with no average rate at all. Copied
  through, ffmpeg duplicated ~31 real frames into 1,455 and stamped the
  output 1000fps -- 370KB where 61KB was correct. The output is forced to
  constant rate, and any "rate" outside 1-120 is treated as the timebase
  it actually is.

## 2. GIF and trimmed WebM are disabled rows when there is no ffmpeg

Not missing ones. `QImageWriter` has no GIF plugin and the bundled FFmpeg
no VP8/VP9 encoder, so without a system ffmpeg neither can be produced.
Both stay in the menu, greyed, with the reason in place of the size
estimate -- the handoff's own rule that an option which cannot work says
why, because a user who cannot see the reason has no way to tell a missing
feature from a broken one.

WebM is only ever disabled *when trimmed*: untrimmed it is a byte-for-byte
copy of what was recorded, which always works and needs no encoder at all.

## 3. Both menus are `flowbars.FlowMenu`

The handoff draws the speed menu on the transport's warm glass
(`rgba(26,28,24,.98)`) and the export menu on the window's neutral
`#1c1f25`. Both are built from the capture flow's `FlowMenu`, so both wear the
warm glass.

One menu widget rather than two: `FlowMenu` already does the disabled-row-with-a
-reason behaviour these need, and the two palettes differ by a few points of hue
on a surface that appears for a second. `FlowMenu` gained an optional `footnote`
for the export menu's standing note about re-encoding.

## 4. The audio track is not carried through an export

`Exporter` wires up `QAudioBufferInput` only when unmuted, but nothing feeds it
yet: syncing a separately-decoded audio stream against video frames that arrive
on the decoder's own thread is real work, and **the GNOME screencast backend
snipux records with produces no audio track at all** — measured with `ffprobe`
on our own recordings. So today every export is silent because every recording
is, and muting is honoured exactly (it drops a track that was never there).

This becomes real the moment a capture backend records audio, and the waveform
band, the mute button and its greyed-waveform promise are all already built
against it.

## 5. Export runs in real time

Playing the trimmed range through a `QMediaPlayer` is what produces the frames,
so a twenty-second export takes about twenty seconds. The window reports
progress rather than pretending otherwise. Decoding faster than real time would
mean driving the demuxer directly, which Qt does not expose.
