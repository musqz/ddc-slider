# Changelog

All notable changes to ddc-slider are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-04-25

### Added
- Per-monitor color-temperature slider (3000K–6500K, marks at 3000 / 4000 /
  5500 / 6500) in each monitor row of the popup and standalone window.
- Master color-temperature slider in multi-monitor mode that broadcasts to
  all monitors, mirroring the brightness/contrast master pattern.
- Quick-preset color-temperature buttons (3000K / 4000K / 5500K / 6500K) in
  single-monitor mode and on the master row.
- I2C bus → DRM connector → X randr CRTC mapping, used to scope each
  `redshift` invocation to a single monitor via `redshift -m randr:crtc=N`.
  Connector names are read from `/sys/class/drm/<connector>/ddc` and matched
  against `xrandr --verbose`, with a `HDMI-A-1` → `HDMI-1` style fallback.
- Multi-monitor popup headers display the resolved randr output name
  (e.g. `Dell — DP-1`) so users can see when per-monitor mapping succeeded.
- Per-monitor color temperatures persist in `state.json` and are re-applied
  on launch, since X gamma resets per session.
- `COLOR TEMPERATURE` section in the man page documenting the slider, the
  bus → CRTC mapping, and the global-redshift fallback.

### Changed
- Removed the global four-button color-temperature row at the bottom of the
  popup; per-monitor sliders (plus the master in multi-monitor mode) now
  cover the same use case with finer granularity.
- Manual presets defined in `config.json` with a `color_temp` field now
  apply per-monitor instead of as a single global tint.
- README "Color temperature" feature line updated to reflect per-monitor
  control and the global-redshift fallback.

### Fixed
- Color temperature is no longer lost across X sessions on multi-monitor
  setups: the cached value is reapplied per monitor at launch.

### Compatibility
- Wayland sessions, headless setups, and any monitor whose I2C bus cannot
  be resolved to a CRTC fall back to the previous global `redshift -O`
  behavior — no regression for those users.
- `state.json` and `config.json` from v1.0.0 are read unchanged; new fields
  default in via `dict.get()`.
- No CLI flag changes; `--help` output is identical.

### Requires
- `redshift` for the per-monitor color-temperature slider (already an
  optional dependency in v1.0.0).

## [1.0.0] - 2026-04-05

Initial release after fork/rewrite of `xfce4-ddc-brightness-slider`,
switching the backend from `ddccontrol` to `ddcutil`.

### Added
- Per-monitor brightness and contrast sliders with a master row for
  multi-monitor setups.
- GTK3 tray icon (GtkStatusIcon and AppIndicator backends) and standalone
  floating window (`--standalone`).
- Auto-detection of DDC-capable monitors with EDID-based names.
- State cache (`state.json`) for instant startup with background hardware
  refresh.
- Configurable user presets via `config.json` (brightness, contrast,
  color temperature, scroll step).
- Global color-temperature presets via `redshift`.
- Light and dark embedded SVG tray icons with auto-detection or manual
  override (`--icon light|dark`).
- Translated UI strings for English, Dutch, Polish, German, Spanish,
  French, Brazilian Portuguese, Italian, Russian, Turkish, Simplified
  Chinese, and Japanese.
- CLI commands `--get`, `--set`, `--get-contrast`, `--set-contrast`,
  `--bus`, `--no-cache`, `--config`, `--no-config`.
