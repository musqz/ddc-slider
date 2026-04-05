#!/usr/bin/env python3

"""
ddc-slider — DDC/CI Brightness & Contrast Slider

GTK3 tray icon / standalone window to control monitor brightness and contrast
via ddcutil (DDC/CI protocol over I2C).

Features:
  - Auto-detect all DDC-capable monitors with proper model names
  - State cache for instant startup (background hardware refresh)
  - Per-monitor and master brightness/contrast sliders
  - Tray icon with scroll-wheel support
  - Color temperature presets via redshift
  - Configurable presets via JSON config

Requirements:
  - ddcutil
  - redshift (optional, for color temperature)
  - python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1
  - User must be in the 'i2c' group: sudo usermod -aG i2c $USER

Original: Vladimir Krasnov (MIT License)
Fork/rewrite: Musduz, 2025
License: MIT
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version('Gtk', '3.0')

from gi.repository import Gtk, Gdk, GLib

import subprocess
import argparse
import dataclasses
import json
import os
import re
import sys
import signal
import threading
import time

APP_NAME = "ddc-slider"
APP_VERSION = "unknown"

# Read version from release.txt (next to script, or /usr/local/share/ddc-slider/)
for _vpath in [
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "release.txt"),
    "/usr/local/share/ddc-slider/release.txt",
]:
    try:
        with open(_vpath) as _f:
            APP_VERSION = _f.read().strip()
            break
    except FileNotFoundError:
        continue

DEFAULT_CONFIG_DIR = os.path.expanduser(f"~/.config/{APP_NAME}")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.json")
DEFAULT_STATE_PATH = os.path.join(DEFAULT_CONFIG_DIR, "state.json")
DEFAULT_CONFIG = {
    "scroll_step": 1,
    "presets": [
        {"name": "Movie", "brightness": 30, "contrast": 60, "color_temp": 3500},
        {"name": "Reading", "brightness": 80, "contrast": 40, "color_temp": 5500},
    ],
}
VCP_BRIGHTNESS = 10   # VCP feature code 0x10
VCP_CONTRAST = 12     # VCP feature code 0x12
DEFAULT_MIN = 0
DEFAULT_MAX = 100
DEFAULT_STEP = 5
DEFAULT_SCROLL_STEP = 1
STATE_MAX_AGE = 86400  # 24 hours
ICON_NAME_FALLBACK = "display-brightness-symbolic"

# Embedded SVG brightness icons (sun symbol) for light and dark panels
_ICON_SVG_DARK = '''<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <circle cx="12" cy="12" r="4.5" fill="#222" fill-opacity="0.9"/>
  <g stroke="#222" stroke-width="2" stroke-linecap="round" opacity="0.9">
    <line x1="12" y1="1.5" x2="12" y2="4"/>
    <line x1="12" y1="20" x2="12" y2="22.5"/>
    <line x1="1.5" y1="12" x2="4" y2="12"/>
    <line x1="20" y1="12" x2="22.5" y2="12"/>
    <line x1="4.6" y1="4.6" x2="6.4" y2="6.4"/>
    <line x1="17.6" y1="17.6" x2="19.4" y2="19.4"/>
    <line x1="4.6" y1="19.4" x2="6.4" y2="17.6"/>
    <line x1="17.6" y1="6.4" x2="19.4" y2="4.6"/>
  </g>
</svg>'''

_ICON_SVG_LIGHT = '''<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <circle cx="12" cy="12" r="4.5" fill="#eee" fill-opacity="0.95"/>
  <g stroke="#eee" stroke-width="2" stroke-linecap="round" opacity="0.95">
    <line x1="12" y1="1.5" x2="12" y2="4"/>
    <line x1="12" y1="20" x2="12" y2="22.5"/>
    <line x1="1.5" y1="12" x2="4" y2="12"/>
    <line x1="20" y1="12" x2="22.5" y2="12"/>
    <line x1="4.6" y1="4.6" x2="6.4" y2="6.4"/>
    <line x1="17.6" y1="17.6" x2="19.4" y2="19.4"/>
    <line x1="4.6" y1="19.4" x2="6.4" y2="17.6"/>
    <line x1="17.6" y1="6.4" x2="19.4" y2="4.6"/>
  </g>
</svg>'''


def _get_icon_path(icon_style: str | None = None) -> str:
    """Get the path to the appropriate tray icon.
    icon_style: 'light', 'dark', or None for auto-detect."""
    icon_dir = os.path.join(DEFAULT_CONFIG_DIR, "icons")
    os.makedirs(icon_dir, exist_ok=True)

    dark_path = os.path.join(icon_dir, "brightness-dark.svg")
    light_path = os.path.join(icon_dir, "brightness-light.svg")

    # Write icons if missing
    if not os.path.exists(dark_path):
        with open(dark_path, "w") as f:
            f.write(_ICON_SVG_DARK)
    if not os.path.exists(light_path):
        with open(light_path, "w") as f:
            f.write(_ICON_SVG_LIGHT)

    if icon_style == "dark":
        return dark_path
    if icon_style == "light":
        return light_path

    # Auto-detect: check GTK theme background luminance
    try:
        win = Gtk.Window()
        ctx = win.get_style_context()
        bg = ctx.get_background_color(Gtk.StateFlags.NORMAL)
        luminance = 0.299 * bg.red + 0.587 * bg.green + 0.114 * bg.blue
        win.destroy()
        # Dark theme → use light icon, light theme → use dark icon
        choice = light_path if luminance < 0.5 else dark_path
        icon = "light" if luminance < 0.5 else "dark"
        print(f"[{APP_NAME}] Theme luminance={luminance:.2f}, using {icon} icon",
              file=sys.stderr)
        return choice
    except Exception:
        return light_path


# ---------------------------------------------------------------------------
#  Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class MonitorInfo:
    bus: int               # I2C bus number (e.g. 3)
    device: str            # "/dev/i2c-3"
    name: str              # "Dell U2515H"
    brightness: 'DDCController'
    contrast: 'DDCController'


# ---------------------------------------------------------------------------
#  State cache — instant startup
# ---------------------------------------------------------------------------

def load_state() -> list[dict] | None:
    """Load cached monitor state. Returns None if missing or stale (>24h)."""
    try:
        with open(DEFAULT_STATE_PATH) as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > STATE_MAX_AGE:
            print(f"[{APP_NAME}] State cache too old ({age:.0f}s), will re-probe", file=sys.stderr)
            return None
        monitors = data.get("monitors", [])
        if not monitors:
            return None
        print(f"[{APP_NAME}] Loaded cached state ({len(monitors)} monitors, {age:.0f}s old)",
              file=sys.stderr)
        return monitors
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_state(monitors: list[MonitorInfo], values: dict[int, dict]):
    """Save monitor list and current values to state cache.
    values = {bus: {"brightness": int, "contrast": int}}
    """
    data = {
        "timestamp": time.time(),
        "monitors": [
            {
                "bus": m.bus,
                "name": m.name,
                "brightness": values.get(m.bus, {}).get("brightness", 50),
                "contrast": values.get(m.bus, {}).get("contrast", 50),
            }
            for m in monitors
        ],
    }
    try:
        os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)
        with open(DEFAULT_STATE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"[{APP_NAME}] Error saving state: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
#  Monitor detection via ddcutil
# ---------------------------------------------------------------------------

def detect_monitors() -> list[tuple[int, str]]:
    """Probe for DDC-capable monitors via ddcutil detect.
    Returns list of (bus_number, model_name) tuples.
    Skips 'Invalid display' entries (no DDC/CI support)."""
    monitors = []
    try:
        result = subprocess.run(
            ["ddcutil", "detect"],
            capture_output=True, text=True, timeout=15
        )
        bus = None
        name = ""
        valid = False
        for line in result.stdout.splitlines():
            # Valid display block
            if re.match(r'^Display\s+\d+', line):
                if bus is not None and valid:
                    monitors.append((bus, name))
                bus = None
                name = ""
                valid = True
            # Invalid display block — skip
            elif re.match(r'^Invalid display', line):
                if bus is not None and valid:
                    monitors.append((bus, name))
                bus = None
                name = ""
                valid = False
            # I2C bus line
            m = re.search(r'I2C bus:\s*/dev/i2c-(\d+)', line)
            if m:
                bus = int(m.group(1))
            # Model name from EDID
            m = re.search(r'Model:\s*(.+)', line)
            if m:
                name = m.group(1).strip()
        # Don't forget the last display
        if bus is not None and valid:
            monitors.append((bus, name))
    except FileNotFoundError:
        print(f"[{APP_NAME}] ERROR: ddcutil not found. Install it: pacman -S ddcutil",
              file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[{APP_NAME}] ERROR: ddcutil detect timed out", file=sys.stderr)
    except Exception as e:
        print(f"[{APP_NAME}] Error probing monitors: {e}", file=sys.stderr)
    return monitors


def build_monitors(detected: list[tuple[int, str]], vcp_brightness: int,
                   vcp_contrast: int) -> list[MonitorInfo]:
    """Build MonitorInfo list from detected (bus, name) tuples."""
    monitors = []
    for bus, name in detected:
        device = f"/dev/i2c-{bus}"
        label = f" ({name})" if name else ""
        print(f"[{APP_NAME}] Found: bus {bus} {device}{label}", file=sys.stderr)
        monitors.append(MonitorInfo(
            bus=bus, device=device, name=name,
            brightness=DDCController(bus, vcp_brightness),
            contrast=DDCController(bus, vcp_contrast),
        ))
    return monitors


def build_monitors_from_cache(cached: list[dict], vcp_brightness: int,
                              vcp_contrast: int) -> list[MonitorInfo]:
    """Build MonitorInfo list from cached state data."""
    monitors = []
    for entry in cached:
        bus = entry["bus"]
        name = entry.get("name", "")
        monitors.append(MonitorInfo(
            bus=bus, device=f"/dev/i2c-{bus}", name=name,
            brightness=DDCController(bus, vcp_brightness),
            contrast=DDCController(bus, vcp_contrast),
        ))
    return monitors


# ---------------------------------------------------------------------------
#  DDC controller via ddcutil
# ---------------------------------------------------------------------------

class DDCController:
    """Read/write a single VCP feature on a specific I2C bus via ddcutil.
    Uses per-bus locking to prevent I2C contention."""

    _bus_locks: dict[int, threading.Lock] = {}
    _locks_lock = threading.Lock()

    def __init__(self, bus: int, feature: int):
        self.bus = bus
        self.feature = feature   # 10 = brightness, 12 = contrast
        # Create a shared lock per bus (brightness + contrast on same bus share it)
        with DDCController._locks_lock:
            if bus not in DDCController._bus_locks:
                DDCController._bus_locks[bus] = threading.Lock()
        self._lock = DDCController._bus_locks[bus]

    def get_value(self) -> int | None:
        """Read current VCP value from hardware."""
        with self._lock:
            try:
                result = subprocess.run(
                    ["ddcutil", "getvcp", str(self.feature),
                     "--bus", str(self.bus), "--terse"],
                    capture_output=True, text=True, timeout=10
                )
                # Terse output: "VCP 10 C 70 100" (feature, type, current, max)
                m = re.search(r'VCP\s+\w+\s+\w+\s+(\d+)\s+(\d+)', result.stdout)
                if m:
                    return int(m.group(1))
                # Fallback: verbose output
                m = re.search(r'current value\s*=\s*(\d+)', result.stdout)
                if m:
                    return int(m.group(1))
                return None
            except subprocess.TimeoutExpired:
                print(f"[{APP_NAME}] Timeout reading VCP {self.feature} on bus {self.bus}",
                      file=sys.stderr)
                return None
            except Exception as e:
                print(f"[{APP_NAME}] Error reading VCP {self.feature}: {e}", file=sys.stderr)
                return None

    def set_value(self, value: int) -> bool:
        """Write VCP value to hardware."""
        value = max(0, min(100, int(value)))
        with self._lock:
            try:
                cmd = ["ddcutil", "setvcp", str(self.feature), str(value),
                       "--bus", str(self.bus), "--noverify"]
                print(f"[{APP_NAME}] Running: {' '.join(cmd)}", file=sys.stderr)
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    print(f"[{APP_NAME}] setvcp failed (rc={result.returncode}): "
                          f"{result.stderr.strip()}", file=sys.stderr)
                else:
                    print(f"[{APP_NAME}] setvcp OK: VCP {self.feature}={value} bus {self.bus}",
                          file=sys.stderr)
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                print(f"[{APP_NAME}] Timeout setting VCP {self.feature} to {value} on bus {self.bus}",
                      file=sys.stderr)
                return False
            except Exception as e:
                print(f"[{APP_NAME}] Error setting VCP {self.feature} to {value}: {e}",
                      file=sys.stderr)
                return False


# ---------------------------------------------------------------------------
#  Slider group widget (brightness + contrast for one monitor)
# ---------------------------------------------------------------------------

class _SliderGroup:
    """A pair of brightness/contrast sliders for one monitor (or master)."""

    def __init__(self, vbox, monitor: MonitorInfo | None, min_val, max_val, step,
                 on_brightness, on_contrast, show_presets=False):
        self.monitor = monitor
        self.is_applying_brightness = False
        self.is_applying_contrast = False
        self._brightness_debounce = None
        self._contrast_debounce = None
        self._on_brightness = on_brightness
        self._on_contrast = on_contrast

        # --- Brightness ---
        title = Gtk.Label()
        title.set_markup("<b>☀ Brightness</b>")
        vbox.pack_start(title, False, False, 0)
        vbox.pack_start(Gtk.Separator(), False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, True, True, 0)

        adj = Gtk.Adjustment(value=50, lower=min_val, upper=max_val,
                             step_increment=step, page_increment=step * 2, page_size=0)
        self.brightness_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.brightness_scale.set_digits(0)
        self.brightness_scale.set_draw_value(False)
        self.brightness_scale.set_size_request(200, -1)
        for tick in range(min_val, max_val + 1, 25):
            self.brightness_scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)
        self.brightness_scale.connect("value-changed", self._on_brightness_changed)
        hbox.pack_start(self.brightness_scale, True, True, 0)

        self.brightness_label = Gtk.Label(label="50%")
        self.brightness_label.set_width_chars(5)
        hbox.pack_start(self.brightness_label, False, False, 0)

        if show_presets:
            vbox.pack_start(Gtk.Separator(), False, False, 0)
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            btn_box.set_homogeneous(True)
            vbox.pack_start(btn_box, False, False, 0)
            for preset in [1, 10, 25, 50, 75, 100]:
                btn = Gtk.Button(label=f"{preset}%")
                btn.connect("clicked", self._on_preset_clicked, preset)
                btn_box.pack_start(btn, True, True, 0)

        # --- Contrast ---
        vbox.pack_start(Gtk.Separator(), False, False, 0)
        contrast_title = Gtk.Label()
        contrast_title.set_markup("<b>◑ Contrast</b>")
        vbox.pack_start(contrast_title, False, False, 0)
        vbox.pack_start(Gtk.Separator(), False, False, 0)

        contrast_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(contrast_hbox, True, True, 0)

        contrast_adj = Gtk.Adjustment(value=50, lower=min_val, upper=max_val,
                                      step_increment=step, page_increment=step * 2, page_size=0)
        self.contrast_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=contrast_adj)
        self.contrast_scale.set_digits(0)
        self.contrast_scale.set_draw_value(False)
        self.contrast_scale.set_size_request(200, -1)
        for tick in range(min_val, max_val + 1, 25):
            self.contrast_scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)
        self.contrast_scale.connect("value-changed", self._on_contrast_changed)
        contrast_hbox.pack_start(self.contrast_scale, True, True, 0)

        self.contrast_label = Gtk.Label(label="50%")
        self.contrast_label.set_width_chars(5)
        contrast_hbox.pack_start(self.contrast_label, False, False, 0)

    def refresh(self):
        """Read actual hardware values (blocking — use from background thread)."""
        if self.monitor is None:
            return
        val = self.monitor.brightness.get_value()
        if val is not None:
            GLib.idle_add(self.set_brightness, val)
        con = self.monitor.contrast.get_value()
        if con is not None:
            GLib.idle_add(self.set_contrast, con)

    def set_brightness(self, value):
        self.is_applying_brightness = True
        self.brightness_scale.set_value(value)
        self.brightness_label.set_text(f"{value}%")
        self.is_applying_brightness = False

    def set_contrast(self, value):
        self.is_applying_contrast = True
        self.contrast_scale.set_value(value)
        self.contrast_label.set_text(f"{value}%")
        self.is_applying_contrast = False

    def _on_brightness_changed(self, scale):
        if self.is_applying_brightness:
            return
        value = int(scale.get_value())
        self.brightness_label.set_text(f"{value}%")
        if self._brightness_debounce:
            GLib.source_remove(self._brightness_debounce)
        self._brightness_debounce = GLib.timeout_add(300, self._apply_brightness, value)

    def _apply_brightness(self, value):
        self._brightness_debounce = None
        self._on_brightness(self, value)
        return False

    def _on_contrast_changed(self, scale):
        if self.is_applying_contrast:
            return
        value = int(scale.get_value())
        self.contrast_label.set_text(f"{value}%")
        if self._contrast_debounce:
            GLib.source_remove(self._contrast_debounce)
        self._contrast_debounce = GLib.timeout_add(300, self._apply_contrast, value)

    def _apply_contrast(self, value):
        self._contrast_debounce = None
        self._on_contrast(self, value)
        return False

    def _on_preset_clicked(self, button, value):
        self.brightness_label.set_text(f"{value}%")
        self.is_applying_brightness = True
        self.brightness_scale.set_value(value)
        self.is_applying_brightness = False
        self._on_brightness(self, value)


# ---------------------------------------------------------------------------
#  Brightness popup (for tray icon mode)
# ---------------------------------------------------------------------------

class BrightnessPopup(Gtk.Window):

    COLOR_TEMP_PRESETS = [
        (3000, "3000K"),
        (4000, "4000K"),
        (5500, "5500K"),
        (6500, "6500K"),
    ]

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int,
                 on_color_temp=None, on_value_changed=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.monitors = monitors
        self._on_color_temp = on_color_temp
        self._on_value_changed = on_value_changed
        self._visible = False
        self._position = (0, 0)
        self._monitor_groups: list[_SliderGroup] = []
        self._master_group: _SliderGroup | None = None

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_border_width(8)
        self.set_accept_focus(True)
        self.set_can_focus(True)
        self.set_gravity(Gdk.Gravity.NORTH_WEST)
        self.set_position(Gtk.WindowPosition.NONE)

        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)
        self.connect("show", lambda w: self._set_visible(True))
        self.connect("hide", lambda w: self._set_visible(False))
        self.connect("realize", self._on_realize)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        multi = len(monitors) > 1

        if multi:
            header = Gtk.Label()
            header.set_markup("<b>🖥 All Monitors</b>")
            vbox.pack_start(header, False, False, 0)
            master_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(master_box, False, False, 0)
            self._master_group = _SliderGroup(
                master_box, None, min_val, max_val, step,
                on_brightness=self._on_master_brightness,
                on_contrast=self._on_master_contrast,
                show_presets=True)

        for mon in monitors:
            if multi:
                vbox.pack_start(Gtk.Separator(), False, False, 4)
                header = Gtk.Label()
                header.set_markup(f"<b>▪ {GLib.markup_escape_text(mon.name or mon.device)}</b>")
                vbox.pack_start(header, False, False, 0)

            mon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(mon_box, False, False, 0)
            group = _SliderGroup(
                mon_box, mon, min_val, max_val, step,
                on_brightness=self._on_monitor_brightness,
                on_contrast=self._on_monitor_contrast,
                show_presets=not multi)
            self._monitor_groups.append(group)

        if on_color_temp is not None:
            vbox.pack_start(Gtk.Separator(), False, False, 0)
            temp_title = Gtk.Label()
            temp_title.set_markup("<b>🌡 Color Temp</b>")
            vbox.pack_start(temp_title, False, False, 0)
            temp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            temp_box.set_homogeneous(True)
            for temp, label in self.COLOR_TEMP_PRESETS:
                btn = Gtk.Button(label=label)
                btn.connect("clicked", self._on_temp_clicked, temp)
                temp_box.pack_start(btn, True, True, 0)
            vbox.pack_start(temp_box, False, False, 0)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: @theme_bg_color;
                border: 1px solid @borders;
            }
            button {
                min-height: 24px;
                padding: 2px 4px;
                font-size: 11px;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _notify_changed(self):
        """Notify parent that slider values changed (for state cache save)."""
        if self._on_value_changed:
            self._on_value_changed()

    def _on_temp_clicked(self, button, temp: int):
        if self._on_color_temp:
            self._on_color_temp(temp)

    def _on_monitor_brightness(self, group: _SliderGroup, value: int):
        threading.Thread(target=group.monitor.brightness.set_value, args=(value,), daemon=True).start()
        self._notify_changed()

    def _on_monitor_contrast(self, group: _SliderGroup, value: int):
        threading.Thread(target=group.monitor.contrast.set_value, args=(value,), daemon=True).start()
        self._notify_changed()

    def _on_master_brightness(self, group: _SliderGroup, value: int):
        def _do():
            for mg in self._monitor_groups:
                mg.monitor.brightness.set_value(value)
        for mg in self._monitor_groups:
            mg.set_brightness(value)
        threading.Thread(target=_do, daemon=True).start()
        self._notify_changed()

    def _on_master_contrast(self, group: _SliderGroup, value: int):
        def _do():
            for mg in self._monitor_groups:
                mg.monitor.contrast.set_value(value)
        for mg in self._monitor_groups:
            mg.set_contrast(value)
        threading.Thread(target=_do, daemon=True).start()
        self._notify_changed()

    def _on_realize(self, widget):
        self.get_window().move_resize(
            self._position[0], self._position[1],
            self.get_allocated_width(), self.get_allocated_height()
        )

    def refresh_value(self):
        """Refresh all sliders from hardware in background thread."""
        def _do_refresh():
            results = {}
            for group in self._monitor_groups:
                mon = group.monitor
                b = mon.brightness.get_value()
                c = mon.contrast.get_value()
                results[mon.bus] = (b, c)
            GLib.idle_add(self._apply_hw_results, results)

        threading.Thread(target=_do_refresh, daemon=True).start()

    def _apply_hw_results(self, results: dict):
        """Apply hardware-read values to sliders (main thread)."""
        for group in self._monitor_groups:
            vals = results.get(group.monitor.bus)
            if vals:
                b, c = vals
                if b is not None:
                    group.set_brightness(b)
                if c is not None:
                    group.set_contrast(c)
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))
        self._notify_changed()
        return False

    def apply_cached_values(self, cached: list[dict]):
        """Set slider positions from cached state (no hardware read)."""
        cache_map = {entry["bus"]: entry for entry in cached}
        for group in self._monitor_groups:
            entry = cache_map.get(group.monitor.bus)
            if entry:
                group.set_brightness(entry.get("brightness", 50))
                group.set_contrast(entry.get("contrast", 50))
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))

    def update_all(self, brightness: int, contrast: int):
        """Update all sliders to given values."""
        for group in self._monitor_groups:
            group.set_brightness(brightness)
            group.set_contrast(contrast)
        if self._master_group:
            self._master_group.set_brightness(brightness)
            self._master_group.set_contrast(contrast)

    def update_value(self, value):
        """Update all monitor brightness sliders (used by scroll)."""
        for group in self._monitor_groups:
            group.set_brightness(value)
        if self._master_group:
            self._master_group.set_brightness(value)

    def _set_visible(self, visible):
        self._visible = visible

    def _on_focus_out(self, widget, event):
        GLib.timeout_add(100, self._check_focus)
        return False

    def _check_focus(self):
        if not self.is_active():
            self.hide()
        return False

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def toggle_at(self, x: int, y: int):
        if self._visible:
            self.hide()
            self.unrealize()
        else:
            self.refresh_value()
            self._position = (x, y)
            self.move(x, y)
            self.show_all()
            self.move(x, y)
            self.present()
            if self.get_window():
                self.get_window().focus(Gdk.CURRENT_TIME)


# ---------------------------------------------------------------------------
#  Config file
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Load config from JSON file. Returns parsed config or empty dict on error."""
    try:
        with open(path) as f:
            cfg = json.load(f)
        presets = [{
            "name": p.get("name", "Preset"),
            "brightness": p.get("brightness", 50),
            "contrast": p.get("contrast", 50),
            "color_temp": p.get("color_temp"),
        } for p in cfg.get("presets", [])]
        result = {
            "scroll_step": cfg.get("scroll_step", DEFAULT_SCROLL_STEP),
            "presets": presets,
        }
        if presets:
            print(f"[{APP_NAME}] Config loaded: {len(presets)} presets", file=sys.stderr)
        return result
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"[{APP_NAME}] Error loading config: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
#  Tray icon app
# ---------------------------------------------------------------------------

class TrayApp:

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int,
                 scroll_step: int = DEFAULT_SCROLL_STEP, presets: list | None = None,
                 cached_state: list[dict] | None = None, icon_style: str | None = None):
        self.monitors = monitors
        self.min_val = min_val
        self.max_val = max_val
        self.scroll_step = scroll_step
        self.presets = presets or []
        self.popup = BrightnessPopup(monitors, min_val, max_val, step,
                                     on_color_temp=self._on_color_temp,
                                     on_value_changed=self._save_current_state)
        self._cached_brightness = None
        self._scroll_debounce_id = None
        self._redshift_paused = False
        self._icon_path = _get_icon_path(icon_style)

        # Apply cached slider values if available
        if cached_state:
            self.popup.apply_cached_values(cached_state)

        self.status_icon = None
        self.indicator = None

        n = len(monitors)
        tooltip = f"DDC Brightness ({n} monitors)" if n > 1 else "DDC Brightness"

        if not self._setup_status_icon(tooltip):
            try:
                gi.require_version('AyatanaAppIndicator3', '0.1')
                from gi.repository import AyatanaAppIndicator3
                self._setup_appindicator(AyatanaAppIndicator3, tooltip)
            except (ValueError, ImportError):
                try:
                    gi.require_version('AppIndicator3', '0.1')
                    from gi.repository import AppIndicator3
                    self._setup_appindicator(AppIndicator3, tooltip)
                except (ValueError, ImportError):
                    print(f"[{APP_NAME}] ERROR: No tray icon backend available!", file=sys.stderr)
                    sys.exit(1)

    def _save_current_state(self):
        """Collect current slider values and write state cache."""
        values = {}
        for group in self.popup._monitor_groups:
            mon = group.monitor
            values[mon.bus] = {
                "brightness": int(group.brightness_scale.get_value()),
                "contrast": int(group.contrast_scale.get_value()),
            }
        save_state(self.monitors, values)

    def _setup_status_icon(self, tooltip: str) -> bool:
        try:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_file(self._icon_path)
            self.status_icon.set_tooltip_text(tooltip)
            self.status_icon.set_visible(True)
            self.status_icon.connect("activate", self._on_left_click)
            self.status_icon.connect("popup-menu", self._on_right_click)
            self.status_icon.connect("scroll-event", self._on_scroll_event)
            print(f"[{APP_NAME}] Using GtkStatusIcon tray icon", file=sys.stderr)
            return True
        except Exception:
            return False

    def _on_left_click(self, icon):
        success, screen, area, orientation = icon.get_geometry()
        if success:
            display = Gdk.Display.get_default()
            monitor = display.get_monitor_at_point(area.x, area.y)
            popup_width = 280
            popup_height = 210 * len(self.monitors) + (60 if len(self.monitors) > 1 else 0)

            x = area.x + area.width // 2 - popup_width // 2

            if monitor:
                workarea = monitor.get_workarea()
                x = max(workarea.x, min(x, workarea.x + workarea.width - popup_width))

                if area.y > workarea.y + workarea.height // 2:
                    y = area.y - popup_height - 4
                else:
                    y = area.y + area.height + 4
            else:
                y = area.y + area.height + 4

            self.popup.toggle_at(x, y)
        else:
            display = Gdk.Display.get_default()
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            _, x, y = pointer.get_position()
            self.popup.toggle_at(x - 140, y + 10)

    @staticmethod
    def _apply_redshift(temp: int):
        """Apply color temperature via redshift one-shot."""
        try:
            subprocess.Popen(
                ["redshift", "-P", "-O", str(temp)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            print(f"[{APP_NAME}] redshift not found, skipping color_temp", file=sys.stderr)

    def _on_color_temp(self, temp: int):
        self._apply_redshift(temp)

    def _on_apply_preset(self, widget, preset_index):
        """Apply a preset immediately."""
        if preset_index >= len(self.presets):
            return
        preset = self.presets[preset_index]
        self.popup.update_all(preset["brightness"], preset["contrast"])
        if preset.get("color_temp"):
            self._apply_redshift(preset["color_temp"])
        def _do():
            for mon in self.monitors:
                mon.brightness.set_value(preset["brightness"])
                mon.contrast.set_value(preset["contrast"])
            GLib.idle_add(self._save_current_state)
        threading.Thread(target=_do, daemon=True).start()

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        if self.presets:
            for i, preset in enumerate(self.presets):
                label = f"{preset['name']}  ☀{preset['brightness']}% ◑{preset['contrast']}%"
                if preset.get("color_temp"):
                    label += f" 🌡{preset['color_temp']}K"
                item = Gtk.MenuItem(label=label)
                item.connect("activate", self._on_apply_preset, i)
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        if self._is_redshift_running():
            label = "▶ Resume Redshift" if self._redshift_paused else "⏸ Pause Redshift"
            item_rs = Gtk.MenuItem(label=label)
            item_rs.connect("activate", self._on_toggle_redshift)
            menu.append(item_rs)
            menu.append(Gtk.SeparatorMenuItem())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        menu.append(item_quit)
        menu.show_all()
        return menu

    def _on_right_click(self, icon, button, time):
        menu = self._build_menu()
        menu.popup(None, None, None, None, button, time)

    @staticmethod
    def _is_redshift_running() -> bool:
        try:
            result = subprocess.run(["pgrep", "-x", "redshift"], capture_output=True)
            return result.returncode == 0
        except Exception:
            return False

    def _on_toggle_redshift(self, widget):
        try:
            subprocess.run(["pkill", "-USR1", "redshift"], capture_output=True)
            self._redshift_paused = not self._redshift_paused
        except Exception:
            pass

    def _setup_appindicator(self, AppIndicatorLib, tooltip: str):
        icon_dir = os.path.dirname(self._icon_path)
        icon_name = os.path.splitext(os.path.basename(self._icon_path))[0]
        self.indicator = AppIndicatorLib.Indicator.new(
            APP_NAME,
            icon_name,
            AppIndicatorLib.IndicatorCategory.HARDWARE
        )
        self.indicator.set_icon_theme_path(icon_dir)
        self.indicator.set_status(AppIndicatorLib.IndicatorStatus.ACTIVE)
        self.indicator.set_title(tooltip)

        menu = self._build_menu()

        item_slider = Gtk.MenuItem(label="☀ Brightness Slider")
        item_slider.connect("activate", self._on_indicator_activate)
        item_slider.show()
        sep = Gtk.SeparatorMenuItem()
        sep.show()
        menu.prepend(sep)
        menu.prepend(item_slider)
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(item_slider)
        self.indicator.connect("scroll-event", self._on_indicator_scroll)

        print(f"[{APP_NAME}] Using AppIndicator tray icon", file=sys.stderr)

    def _on_indicator_activate(self, widget):
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        _, x, y = pointer.get_position()
        self.popup.toggle_at(x, y)

    def _on_scroll_event(self, icon, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self._adjust_brightness(self.scroll_step)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-self.scroll_step)

    def _on_indicator_scroll(self, indicator, delta, direction):
        if direction == Gdk.ScrollDirection.UP:
            self._adjust_brightness(self.scroll_step)
        elif direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-self.scroll_step)

    def _adjust_brightness(self, delta):
        if self._cached_brightness is None:
            self._cached_brightness = self.monitors[0].brightness.get_value()
            if self._cached_brightness is None:
                return
        new_val = max(self.min_val, min(self.max_val, self._cached_brightness + delta))
        if new_val == self._cached_brightness:
            return
        self._cached_brightness = new_val
        if self.popup._visible:
            self.popup.update_value(new_val)
        if self._scroll_debounce_id:
            GLib.source_remove(self._scroll_debounce_id)
        self._scroll_debounce_id = GLib.timeout_add(100, self._apply_scroll_brightness, new_val)

    def _apply_scroll_brightness(self, value):
        self._scroll_debounce_id = None
        def _set_all():
            for mon in self.monitors:
                mon.brightness.set_value(value)
            GLib.idle_add(self._save_current_state)
        threading.Thread(target=_set_all, daemon=True).start()
        return False


# ---------------------------------------------------------------------------
#  Standalone window
# ---------------------------------------------------------------------------

class StandaloneWindow(Gtk.Window):

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int,
                 cached_state: list[dict] | None = None):
        super().__init__(title="DDC Brightness & Contrast")
        self.monitors = monitors
        self._monitor_groups: list[_SliderGroup] = []
        self._master_group: _SliderGroup | None = None

        self.set_default_size(350, 160)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("destroy", Gtk.main_quit)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(12)
        self.add(vbox)

        multi = len(monitors) > 1

        if multi:
            header = Gtk.Label()
            header.set_markup("<b>🖥 All Monitors</b>")
            vbox.pack_start(header, False, False, 0)
            master_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(master_box, False, False, 0)
            self._master_group = _SliderGroup(
                master_box, None, min_val, max_val, step,
                on_brightness=self._on_master_brightness,
                on_contrast=self._on_master_contrast,
                show_presets=True)

        for mon in monitors:
            if multi:
                vbox.pack_start(Gtk.Separator(), False, False, 4)
                header = Gtk.Label()
                header.set_markup(f"<b>▪ {GLib.markup_escape_text(mon.name or mon.device)}</b>")
                vbox.pack_start(header, False, False, 0)

            mon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(mon_box, False, False, 0)
            group = _SliderGroup(
                mon_box, mon, min_val, max_val, step,
                on_brightness=self._on_monitor_brightness,
                on_contrast=self._on_monitor_contrast,
                show_presets=not multi)
            self._monitor_groups.append(group)

        # Apply cached values first (instant), then refresh from hardware in background
        if cached_state:
            self._apply_cached(cached_state)
        self._refresh_async()

    def _apply_cached(self, cached: list[dict]):
        """Set slider positions from cache (no hardware read)."""
        cache_map = {entry["bus"]: entry for entry in cached}
        for group in self._monitor_groups:
            entry = cache_map.get(group.monitor.bus)
            if entry:
                group.set_brightness(entry.get("brightness", 50))
                group.set_contrast(entry.get("contrast", 50))
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))

    def _refresh_async(self):
        """Non-blocking hardware refresh in background thread."""
        def _do_refresh():
            results = {}
            for group in self._monitor_groups:
                mon = group.monitor
                b = mon.brightness.get_value()
                c = mon.contrast.get_value()
                results[mon.bus] = (b, c)
            GLib.idle_add(self._apply_hw_results, results)

        threading.Thread(target=_do_refresh, daemon=True).start()

    def _apply_hw_results(self, results: dict):
        """Apply hardware-read values to sliders (called on main thread)."""
        for group in self._monitor_groups:
            vals = results.get(group.monitor.bus)
            if vals:
                b, c = vals
                if b is not None:
                    group.set_brightness(b)
                if c is not None:
                    group.set_contrast(c)
        self._sync_master()
        self._save_current_state()
        return False

    def _sync_master(self):
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))

    def _on_monitor_brightness(self, group: _SliderGroup, value: int):
        print(f"[{APP_NAME}] Setting brightness={value} on bus {group.monitor.bus}", file=sys.stderr)
        threading.Thread(target=group.monitor.brightness.set_value, args=(value,), daemon=True).start()
        self._save_current_state()

    def _on_monitor_contrast(self, group: _SliderGroup, value: int):
        print(f"[{APP_NAME}] Setting contrast={value} on bus {group.monitor.bus}", file=sys.stderr)
        threading.Thread(target=group.monitor.contrast.set_value, args=(value,), daemon=True).start()
        self._save_current_state()

    def _on_master_brightness(self, group: _SliderGroup, value: int):
        print(f"[{APP_NAME}] Setting master brightness={value}", file=sys.stderr)
        def _do():
            for mg in self._monitor_groups:
                mg.monitor.brightness.set_value(value)
        for mg in self._monitor_groups:
            mg.set_brightness(value)
        threading.Thread(target=_do, daemon=True).start()
        self._save_current_state()

    def _on_master_contrast(self, group: _SliderGroup, value: int):
        print(f"[{APP_NAME}] Setting master contrast={value}", file=sys.stderr)
        def _do():
            for mg in self._monitor_groups:
                mg.monitor.contrast.set_value(value)
        for mg in self._monitor_groups:
            mg.set_contrast(value)
        threading.Thread(target=_do, daemon=True).start()
        self._save_current_state()

    def _save_current_state(self):
        """Collect current slider values and write state cache."""
        values = {}
        for group in self._monitor_groups:
            mon = group.monitor
            values[mon.bus] = {
                "brightness": int(group.brightness_scale.get_value()),
                "contrast": int(group.contrast_scale.get_value()),
            }
        save_state(self.monitors, values)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="DDC Brightness & Contrast Slider",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Examples:
  %(prog)s                          # Tray icon mode (default)
  %(prog)s --standalone             # Floating window mode
  %(prog)s --bus 3                  # Use a specific I2C bus
  %(prog)s --get                    # Print current brightness
  %(prog)s --set 70                 # Set brightness to 70
  %(prog)s --get-contrast           # Print current contrast
  %(prog)s --set-contrast 50        # Set contrast to 50
  %(prog)s --no-cache               # Force hardware probe (skip cache)
  %(prog)s --config config.json     # Custom config with presets

Config: {DEFAULT_CONFIG_PATH}
State:  {DEFAULT_STATE_PATH}
"""
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("-b", "--bus", type=int, default=None,
                        help="I2C bus number (default: auto-detect)")
    parser.add_argument("--min", type=int, default=DEFAULT_MIN,
                        help=f"Minimum brightness value (default: {DEFAULT_MIN})")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX,
                        help=f"Maximum brightness value (default: {DEFAULT_MAX})")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP,
                        help=f"Slider step size (default: {DEFAULT_STEP})")
    parser.add_argument("--scroll-step", type=int, default=DEFAULT_SCROLL_STEP,
                        help=f"Scroll wheel step size (default: {DEFAULT_SCROLL_STEP})")
    parser.add_argument("--standalone", action="store_true",
                        help="Show as a floating window instead of tray icon")
    parser.add_argument("--set", type=int, metavar="VALUE",
                        help="Set brightness to VALUE and exit (no GUI)")
    parser.add_argument("--get", action="store_true",
                        help="Print current brightness and exit (no GUI)")
    parser.add_argument("--set-contrast", type=int, metavar="VALUE",
                        help="Set contrast to VALUE and exit (no GUI)")
    parser.add_argument("--get-contrast", action="store_true",
                        help="Print current contrast and exit (no GUI)")
    parser.add_argument("--config", metavar="FILE",
                        help=f"JSON config file with presets (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--no-config", action="store_true",
                        help="Disable config file loading")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip state cache, force hardware probe")
    parser.add_argument("--icon", choices=["light", "dark", "auto"], default="auto",
                        help="Tray icon color: light (white), dark (black), "
                             "auto = detect from GTK theme (default)")

    args = parser.parse_args()

    # --- Build monitor list ---
    cached_state = None

    if args.bus is not None:
        # Manual bus specified — no detection needed
        monitors = [MonitorInfo(
            bus=args.bus, device=f"/dev/i2c-{args.bus}",
            name=f"Monitor (bus {args.bus})",
            brightness=DDCController(args.bus, VCP_BRIGHTNESS),
            contrast=DDCController(args.bus, VCP_CONTRAST),
        )]
    else:
        # Auto-detect: try cache first, then probe
        if not args.no_cache:
            cached_state = load_state()

        if cached_state:
            monitors = build_monitors_from_cache(cached_state, VCP_BRIGHTNESS, VCP_CONTRAST)
        else:
            detected = detect_monitors()
            if not detected:
                print(f"[{APP_NAME}] ERROR: No DDC-capable monitor found. "
                      "Use --bus to specify manually.", file=sys.stderr)
                sys.exit(1)
            monitors = build_monitors(detected, VCP_BRIGHTNESS, VCP_CONTRAST)

    # --- CLI commands (no GUI) ---
    if args.get:
        val = monitors[0].brightness.get_value()
        if val is not None:
            print(val)
            sys.exit(0)
        else:
            print("Error: could not read brightness", file=sys.stderr)
            sys.exit(1)

    if args.get_contrast:
        val = monitors[0].contrast.get_value()
        if val is not None:
            print(val)
            sys.exit(0)
        else:
            print("Error: could not read contrast", file=sys.stderr)
            sys.exit(1)

    if args.set is not None:
        ok = monitors[0].brightness.set_value(args.set)
        sys.exit(0 if ok else 1)

    if args.set_contrast is not None:
        ok = monitors[0].contrast.set_value(args.set_contrast)
        sys.exit(0 if ok else 1)

    # --- GUI mode ---
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    config = {}
    if not args.no_config:
        config_path = args.config or DEFAULT_CONFIG_PATH
        if not os.path.exists(config_path) and not args.config:
            os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)
            with open(config_path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            print(f"[{APP_NAME}] Created default config: {config_path}", file=sys.stderr)
        if os.path.exists(config_path):
            config = load_config(config_path)

    scroll_step = config.get("scroll_step", args.scroll_step)
    presets = config.get("presets", [])

    if args.standalone:
        win = StandaloneWindow(monitors, args.min, args.max, args.step,
                               cached_state=cached_state)
        win.show_all()
    else:
        icon_style = None if args.icon == "auto" else args.icon
        app = TrayApp(monitors, args.min, args.max, args.step,
                      scroll_step=scroll_step, presets=presets,
                      cached_state=cached_state, icon_style=icon_style)

    Gtk.main()


if __name__ == "__main__":
    main()
