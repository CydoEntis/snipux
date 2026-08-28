# Player handoff — where the build departs from the spec

`docs/design/player/reference/Snipux Player.dc.html` is the authority. Each entry
below is a deliberate departure, with the reason. Anything not listed here is a
bug, not a decision.

---

## 1. "MP4 (H.264)" is labelled "MP4", and does not promise browsers

**Handoff:** `MP4 (H.264)` — *"Plays anywhere. Slack, Teams, browsers."*
**Built:** `MP4` — *"Re-encoded for the trim. Plays in desktop players."*

The bundled FFmpeg cannot encode H.264 in software. `libavcodec.so.61` as shipped
with PyQt6 6.11 contains exactly two H.264 encoders, `h264_nvenc` and
`h264_vaapi`, and no `libx264` — it is an LGPL build, and x264 is GPL. So H.264
depends entirely on a working hardware encoder.

On the development box — an RTX 4060 with driver 580.173.02 and
`libnvidia-encode.so.1` present — Qt's NVENC path still fails at probe time with
`10 bit encode not supported` / `No capable devices found`, and VAAPI fails the
same way. When it fails, Qt does **not** fall back: `QMediaRecorder` reports
"Could not initialize encoder" and leaves a zero-length file.

`snipux/__init__.py` therefore disables hardware encoding by default, which
trades H.264 for MPEG-4 Part 2 and, crucially, for an export that always
produces a playable file. The label follows the file we actually write. A
`QT_FFMPEG_ENCODING_HW_DEVICE_TYPES` set by the user is left alone, so anyone
with a working encoder keeps H.264 — the variable has to be read before Qt
Multimedia builds its encoder list, which is why the decision lives in the
package's `__init__` and cannot be revisited once an encode has failed.

**Open:** making "plays anywhere" true again needs either a Qt build with a
working hardware encoder or the system `ffmpeg` binary, and adding an external
binary is the kind of dependency CLAUDE.md says to raise rather than decide.

## 2. GIF and trimmed WebM are disabled rows, not missing ones

Same root cause. `QImageWriter` has no GIF plugin and the bundled FFmpeg has no
VP8/VP9 encoder, so neither can be produced. Both stay in the menu, greyed, with
the reason in place of the size estimate — the handoff's own rule that an option
which cannot work says why, because a user who cannot see the reason has no way
to tell a missing feature from a broken one.

WebM is only disabled *when trimmed*: untrimmed it is a byte-for-byte copy of
what was recorded, which always works and needs no encoder at all.

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
