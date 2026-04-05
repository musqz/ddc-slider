#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_PATH="/usr/local/bin/ddc-slider"
MAN_PATH="/usr/local/share/man/man1/ddc-slider.1"
DESKTOP_FILE="ddc-slider.desktop"
AUTOSTART_PATH="$HOME/.config/autostart/$DESKTOP_FILE"
APPS_PATH="$HOME/.local/share/applications/$DESKTOP_FILE"

### Todo auto release.

# Detect distro family
detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            arch|manjaro|endeavouros|garuda|artix|cachyos|mabox)
                echo "arch"
                ;;
            debian|ubuntu|linuxmint|pop|elementary|zorin)
                echo "debian"
                ;;
            *)
                if [ -n "$ID_LIKE" ]; then
                    case "$ID_LIKE" in
                        *arch*) echo "arch" ;;
                        *debian*|*ubuntu*) echo "debian" ;;
                        *) echo "unknown" ;;
                    esac
                else
                    echo "unknown"
                fi
                ;;
        esac
    elif command -v pacman &>/dev/null; then
        echo "arch"
    elif command -v apt-get &>/dev/null; then
        echo "debian"
    else
        echo "unknown"
    fi
}

DISTRO=$(detect_distro)

echo "== ddc-slider Installer =="
echo "  Detected distro family: $DISTRO"
echo

install_packages_arch() {
    local pkgs=("$@")
    local repo_pkgs=()
    local aur_pkgs=()
    for pkg in "${pkgs[@]}"; do
        if pacman -Si "$pkg" &>/dev/null; then
            repo_pkgs+=("$pkg")
        else
            aur_pkgs+=("$pkg")
        fi
    done
    if [ ${#repo_pkgs[@]} -gt 0 ]; then
        sudo pacman -S --needed --noconfirm "${repo_pkgs[@]}"
    fi
    if [ ${#aur_pkgs[@]} -gt 0 ]; then
        if command -v yay &>/dev/null; then
            yay -S --needed --noconfirm "${aur_pkgs[@]}"
        elif command -v paru &>/dev/null; then
            paru -S --needed --noconfirm "${aur_pkgs[@]}"
        else
            echo "  AUR packages needed: ${aur_pkgs[*]}"
            echo "  Please install an AUR helper (yay or paru) or install them manually."
            exit 1
        fi
    fi
}

install_packages_debian() {
    sudo apt-get update -qq
    sudo apt-get install -y "$@"
}

echo "[1/5] Checking dependencies..."
MISSING_CMDS=""
MISSING_PY_GI=0

if ! command -v ddcutil &>/dev/null; then
    MISSING_CMDS="$MISSING_CMDS ddcutil"
fi

if ! python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null; then
    MISSING_PY_GI=1
fi

if [ -n "$MISSING_CMDS" ] || [ "$MISSING_PY_GI" -eq 1 ]; then
    echo "  Missing dependencies detected. Installing..."
    case "$DISTRO" in
        arch)
            PKGS=()
            [[ "$MISSING_CMDS" == *ddcutil* ]] && PKGS+=("ddcutil")
            [ "$MISSING_PY_GI" -eq 1 ] && PKGS+=("python-gobject" "gtk3" "libayatana-appindicator")
            install_packages_arch "${PKGS[@]}"
            ;;
        debian)
            PKGS=""
            [[ "$MISSING_CMDS" == *ddcutil* ]] && PKGS="$PKGS ddcutil"
            [ "$MISSING_PY_GI" -eq 1 ] && PKGS="$PKGS python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1"
            install_packages_debian $PKGS
            ;;
        *)
            echo "  Unsupported distro. Please install manually:"
            echo "    - ddcutil"
            echo "    - python3 GTK3 bindings (python-gobject / python3-gi)"
            echo "    - libayatana-appindicator"
            exit 1
            ;;
    esac
else
    echo "  All dependencies satisfied."
fi

if ! command -v redshift &>/dev/null; then
    echo "  Optional: 'redshift' not found (needed for color temperature presets)."
    read -rp "  Install redshift? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        case "$DISTRO" in
            arch)   install_packages_arch redshift ;;
            debian) install_packages_debian redshift ;;
            *)      echo "  Please install redshift manually." ;;
        esac
    fi
fi

echo "[2/5] Checking I2C permissions..."

if ! lsmod | grep -q i2c_dev; then
    echo "  Loading i2c-dev kernel module..."
    sudo modprobe i2c-dev
    echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf > /dev/null
fi

if ! getent group i2c > /dev/null 2>&1; then
    echo "  Creating 'i2c' group..."
    sudo groupadd i2c
fi

UDEV_RULE="/etc/udev/rules.d/99-i2c.rules"
if [ ! -f "$UDEV_RULE" ]; then
    echo "  Creating udev rule for I2C device permissions..."
    echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "  Created $UDEV_RULE"
fi

if ! groups "$USER" | grep -q '\bi2c\b'; then
    echo "  Adding $USER to 'i2c' group..."
    sudo usermod -aG i2c "$USER"
    echo "  You'll need to log out and back in for group changes to take effect."
    echo "  (or run: newgrp i2c)"
else
    echo "  User is already in 'i2c' group."
fi

echo "[3/5] Installing ddc-slider..."
sudo cp "$SCRIPT_DIR/ddc-slider.py" "$INSTALL_PATH"
sudo chmod +x "$INSTALL_PATH"
echo "  Installed: $INSTALL_PATH"

if [ -f "$SCRIPT_DIR/ddc-slider.1" ]; then
    sudo mkdir -p "$(dirname "$MAN_PATH")"
    sudo cp "$SCRIPT_DIR/ddc-slider.1" "$MAN_PATH"
    echo "  Man page: $MAN_PATH"
fi

echo "[4/5] Setting up autostart..."
mkdir -p "$(dirname "$AUTOSTART_PATH")"
cp "$SCRIPT_DIR/$DESKTOP_FILE" "$AUTOSTART_PATH"

mkdir -p "$(dirname "$APPS_PATH")"
cp "$SCRIPT_DIR/$DESKTOP_FILE" "$APPS_PATH"

echo "[5/5] Detecting DDC-capable monitors..."
echo
if command -v ddcutil &>/dev/null; then
    ddcutil detect 2>/dev/null | while IFS= read -r line; do
        if echo "$line" | grep -qP '^Display\s+\d+'; then
            echo "  $line"
        elif echo "$line" | grep -q 'I2C bus:'; then
            echo "  $line"
        elif echo "$line" | grep -q 'Model:'; then
            echo "  $line"
            echo
        elif echo "$line" | grep -q 'Invalid display'; then
            echo "  $line (skipped — no DDC/CI support)"
        fi
    done
else
    echo "  ddcutil not found, skipping monitor detection."
fi

echo
echo "Installation complete!"
echo
echo "Usage:"
echo "  ddc-slider                    # Tray icon (default)"
echo "  ddc-slider --standalone       # Floating window"
echo "  ddc-slider --icon light       # White tray icon (for dark panels)"
echo "  ddc-slider --bus 3            # Specific I2C bus"
echo "  man ddc-slider                # Full documentation"
echo
echo "The app will auto-start on next login."
echo "To launch now:  ddc-slider &"
