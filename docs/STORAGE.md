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
| Snips                  | `save_folder` setting, default `~/Pictures/snipux`       |
| Recordings             | `recording_folder` setting, default `~/Videos/snipux`    |
| Linux desktop entries  | `$XDG_DATA_HOME/applications`, `$XDG_CONFIG_HOME/autostart`, `$XDG_DATA_HOME/icons/hicolor` |
| Windows shortcuts      | `%APPDATA%\Microsoft\Windows\Start Menu\Programs` (+ Startup) |
| Windows exe + icon     | `%LOCALAPPDATA%\snipux`                                  |

The settings folder is `$XDG_CONFIG_HOME/snipux`, falling back to
`~/.config/snipux` -- on every OS, Windows included, so one folder backs up
everything. `setup_desktop.config_path()` is the one function that knows it.

## config.json

One flat JSON object. Keys in use today:

```text
after_capture               native_resolution           setup_complete
bar_position                recent_captures             shortcut
default_ink                 recording_after             tool_defaults
default_tool                recording_draw_cursor       tray_toggles
filename_pattern            recording_filename_pattern  watermark_backing
hide_sensitive              recording_folder            watermark_color
hints_enabled               recording_frame_rate        watermark_font
instant_saves               remember_tool               watermark_image
last_region                 reuse_last_region           watermark_kind
last_tool                   save_folder                 watermark_text
```

### Rules

- **Read and write only through `setup_desktop.load_<key>` /
  `save_<key>`.** Nothing else opens `config.json`.
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

## Tests

Tests never touch the real config folder or the real Pictures/Videos
folders. Pass `config_dir=tmp_path` or monkeypatch the environment. A test
that leaves a file in a developer's home directory is a bug.
