# Storage

snipux has **no database**, and that is deliberate. Everything it keeps is a
small JSON file and a few plain files beside it. Captures themselves are
ordinary image and video files in folders the user chose.

Don't add SQLite, an ORM, `QSettings` or a settings framework without an
issue that shows a problem the current approach cannot handle.

## Where things live

| What                   | Where                                                    |
| ---------------------- | -------------------------------------------------------- |
| Settings               | `config.json`                                            |
| Hide list              | `hide-list.txt`, beside `config.json`                    |
| Watermark image        | `watermark/`, beside `config.json`                       |
| Unexpected errors      | `crash.log`, beside `config.json`                        |
| Handled failures       | `snipux.log`, beside `config.json`                       |
| Snips                  | `save_folder` setting, default `~/Pictures/snipux`       |
| Recordings             | `recording_folder` setting, default `~/Videos/snipux`    |
| Linux desktop entries  | `$XDG_DATA_HOME/applications` (`snipux.desktop` and the two editors' `snipux-image-editor.desktop`, `snipux-video-editor.desktop`), `$XDG_CONFIG_HOME/autostart`, `$XDG_DATA_HOME/icons/hicolor` |
| Windows shortcuts      | `%APPDATA%\Microsoft\Windows\Start Menu\Programs` (`snipux.lnk`, `Snipux Image Editor.lnk`, `Snipux Video Editor.lnk`, + Startup) |
| Windows Open with      | `HKCU\Software\Classes`: `Snipux.ImageEditor`, `Snipux.VideoEditor`, `Applications\<launcher>.exe`, and one value under each suffix's `OpenWithProgids` |
| Windows exe + icon     | `%LOCALAPPDATA%\snipux`: `snipux.exe`, its libraries in `_internal\`, `snipux.ico` |

The settings folder is `$XDG_CONFIG_HOME/snipux`, falling back to
`~/.config/snipux` -- on every OS, Windows included, so one folder backs up
everything. `setup_desktop.config_path()` is the one function that knows it.

`%LOCALAPPDATA%\snipux` holds the same thing whichever download put it
there. The installer writes `snipux.exe`, `_internal\` and its own
`unins000.*`. The portable `snipux.exe` writes the first two itself on its
first run (`platform/windows_portable.py`), about 90 MB, and replaces them
when a different version of the portable exe is run; there is never more
than one version there. While it does, `.staging\` holds the copy being
prepared and `.replaced\` the one being swapped out; both are gone when it
finishes, and one left by a run that was killed is deleted by the next. A
folder with `unins000.exe` in it is the installer's, and the portable exe
writes nothing to it.

A snip is written as `<name>.png.part` beside where it is going and renamed
once it is complete (`output.write_image`), so the real name never holds
half a picture. A `.part` file left in the save folder is a write that was
cut off -- by a crash or a power loss -- and is safe to delete.

`crash.log` holds unhandled exceptions. `snipux.log` holds the failures
snipux dealt with and would once only have printed -- a shortcut that could
not be written, a snip that failed with no tray to say so -- one dated line
each, written through `log.note()`. Both are cut back to their newest half
once they pass 256 KB, both can contain paths with the user's name in them,
and "Report a problem" offers the end of each. Nothing else is logged, and
neither file ever leaves the machine unless the user posts that report.

**Not stored anywhere:** the tray's Copy history -- the last 10 things snipux
copied, and no more than 128 MB of pictures among them, oldest dropped
first -- lives in memory only (`output.copy_history()`) and is gone when
snipux quits. That is deliberate: a copied snip can show a password or a
private message, and saving every copy to disk without asking would keep it
after the user thought it was gone.

## config.json

One flat JSON object. Keys in use today:

```text
after_capture               last_tool                   save_folder
bar_position                native_resolution           setup_complete
default_ink                 recent_captures             shortcut
default_tool                recording_after             tool_defaults
filename_pattern            recording_draw_cursor       tray_toggles
hide_sensitive              recording_filename_pattern  update_checked_on
hints_enabled               recording_folder            watermark_backing
instant_saves               recording_frame_rate        watermark_color
integration_revision        remember_tool               watermark_font
integration_target          reuse_last_region           watermark_image
last_region                 recording_audio             watermark_kind
                                                        watermark_text
```

### Rules

- **Read and write only through `setup_desktop.load_<key>` /
  `save_<key>`.** Nothing else opens `config.json`.
- **A write never loses the file.** It goes to a temporary file beside
  `config.json` and is moved over it, and a `config.json` that exists but
  cannot be parsed is first renamed `config.json.unreadable` rather than
  replaced by the one key being written.
- **Reading never raises.** Missing file, unreadable file, bad JSON, a value
  of the wrong type -- each becomes the default. A hand-edited or truncated
  config must make snipux behave as if that setting were unset, never crash
  it. `install.sh` runs `--setup` on every install, so a corrupt config
  breaking setup would be a broken install.
- **Writing never raises.** `save_*` returns `False` on an `OSError`.
- **A write rewrites the whole document**, read-modify-write, so setting one
  key never drops another.
- **Validate on load, not only in the UI.** Paths that come from config (the
  watermark image, for one) must not be able to point outside the folder
  they belong in.
- **Keys are additive.** Renaming or re-typing a key needs a loader that
  still accepts the old form; users upgrade with their config in place.
- Coordinates stored in config (`last_region`, `bar_position`) say which
  space they are in, like everywhere else.
- `snipux --remove` deletes `config.json` along with the desktop integration;
  say so wherever a user might be surprised by it.

### Adding a setting

1. `load_<key>(config_dir=None)` with a sensible default and type checks.
2. `save_<key>(value, config_dir=None) -> bool` via `_write_config`.
3. A row in `settings.py`.
4. Tests with `tmp_path` as `config_dir`: default, round trip, and a
   corrupt or wrong-typed value.

## Captures

- Snips and recordings are the user's files. snipux creates the folder if it
  is missing and never deletes or rewrites a capture it did not just make,
  except "Open an image" saving back to the file the user opened.
- Filenames come from the user's `strftime` pattern.
- `recent_captures` stores paths only, newest first, capped. A path that no
  longer exists is dropped when the tray menu is rebuilt.
- Temporary files (OCR input, export intermediates) go in the system temp
  directory and are removed in a `finally`.
- A downloaded update goes in a folder of its own there, `snipux-update-*`
  (`updates.new_download_dir`), and only after it has matched the SHA-256
  the release published. The installer is running from that folder when
  snipux quits, so it cannot be removed then: the next daily check and the
  next download remove whatever earlier updates left
  (`updates.remove_old_downloads`).
- An AppImage that updates itself writes beside itself, and nowhere else
  (`platform/linux_appimage.py`). The new file is staged as
  `.<name>.new` in the same folder and renamed over the running one, never
  written into it. The version it replaces stays as
  `<name>.<version>.bak`; any older `<name>.*.bak` is deleted, so there is
  one backup, not one per update. Nothing that points at the AppImage --
  the `.desktop` and autostart entries, the shortcut -- is rewritten,
  because the path does not change.

## Tests

Tests never touch the real config folder or the real Pictures/Videos
folders. Pass `config_dir=tmp_path` or monkeypatch the environment. A test
that leaves a file in a developer's home directory is a bug.
