#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read version from release.txt
VERSION="unknown"
if [ -f "$SCRIPT_DIR/release.txt" ]; then
    VERSION=$(tr -d '[:space:]' < "$SCRIPT_DIR/release.txt")
fi

# Defaults
PREFIX="/usr/local"
ACTION="install"

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"
            shift
            ;;
        --uninstall)
            ACTION="uninstall"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo
            echo "Options:"
            echo "  --prefix PATH    Install prefix (default: /usr/local)"
            echo "  --uninstall      Remove ddc-slider"
            echo "  -h, --help       Show this help"
            echo
            echo "Examples:"
            echo "  $0                         # Install to /usr/local"
            echo "  $0 --prefix ~/.local       # Install to ~/.local (no sudo)"
            echo "  $0 --uninstall             # Remove from /usr/local"
            echo "  $0 --uninstall --prefix ~/.local"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Try: $0 --help"
            exit 1
            ;;
    esac
done

# Derived paths
BIN_DIR="$PREFIX/bin"
MAN_DIR="$PREFIX/share/man/man1"
SHARE_DIR="$PREFIX/share/ddc-slider"
INSTALL_PATH="$BIN_DIR/ddc-slider"
MAN_PATH="$MAN_DIR/ddc-slider.1"
DESKTOP_FILE="ddc-slider.desktop"
AUTOSTART_PATH="$HOME/.config/autostart/$DESKTOP_FILE"
APPS_PATH="$HOME/.local/share/applications/$DESKTOP_FILE"

# Use sudo only if PREFIX requires it
SUDO=""
if [ ! -w "$PREFIX" ] 2>/dev/null; then
    if [ "$ACTION" = "install" ]; then
        if ! mkdir -p "$BIN_DIR" 2>/dev/null; then
            SUDO="sudo"
        fi
    else
        if [ -f "$INSTALL_PATH" ] && [ ! -w "$INSTALL_PATH" ]; then
            SUDO="sudo"
        fi
    fi
fi

# =========================================================================
#  Uninstall
# =========================================================================
if [ "$ACTION" = "uninstall" ]; then
    echo "== ddc-slider $VERSION Uninstaller =="
    echo "  Prefix: $PREFIX"
    echo

    removed=0

    if [ -f "$INSTALL_PATH" ]; then
        $SUDO rm -f "$INSTALL_PATH"
        echo "  Removed: $INSTALL_PATH"
        removed=$((removed + 1))
    fi

    if [ -f "$MAN_PATH" ]; then
        $SUDO rm -f "$MAN_PATH"
        echo "  Removed: $MAN_PATH"
        removed=$((removed + 1))
    fi

    if [ -d "$SHARE_DIR" ]; then
        $SUDO rm -rf "$SHARE_DIR"
        echo "  Removed: $SHARE_DIR"
        removed=$((removed + 1))
    fi

    if [ -f "$AUTOSTART_PATH" ]; then
        rm -f "$AUTOSTART_PATH"
        echo "  Removed: $AUTOSTART_PATH"
        removed=$((removed + 1))
    fi

    if [ -f "$APPS_PATH" ]; then
        rm -f "$APPS_PATH"
        echo "  Removed: $APPS_PATH"
        removed=$((removed + 1))
    fi

    echo
    if [ "$removed" -eq 0 ]; then
        echo "Nothing to remove. Was ddc-slider installed with --prefix $PREFIX?"
    else
        echo "Uninstall complete ($removed items removed)."
        echo
        echo "User config left at ~/.config/ddc-slider/ (delete manually if desired)."
    fi
    exit 0
fi

# =========================================================================
#  Install
# =========================================================================

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

echo "== ddc-slider $VERSION Installer =="
echo "  Distro: $DISTRO"
echo "  Prefix: $PREFIX"
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

echo "[3/5] Installing ddc-slider $VERSION..."

$SUDO mkdir -p "$BIN_DIR"
$SUDO cp "$SCRIPT_DIR/ddc-slider.py" "$INSTALL_PATH"
$SUDO chmod 755 "$INSTALL_PATH"
echo "  Script:  $INSTALL_PATH"

# Install release.txt so --version works at runtime
$SUDO mkdir -p "$SHARE_DIR"
$SUDO cp "$SCRIPT_DIR/release.txt" "$SHARE_DIR/release.txt"
$SUDO chmod 644 "$SHARE_DIR/release.txt"
echo "  Version: $SHARE_DIR/release.txt"

# Install man page with version placeholder replaced
if [ -f "$SCRIPT_DIR/ddc-slider.1" ]; then
    $SUDO mkdir -p "$MAN_DIR"
    sed "s/__VERSION__/$VERSION/g" "$SCRIPT_DIR/ddc-slider.1" | $SUDO tee "$MAN_PATH" > /dev/null
    $SUDO chmod 644 "$MAN_PATH"
    echo "  Man:     $MAN_PATH"
fi

echo "[4/5] Setting up autostart..."
mkdir -p "$(dirname "$AUTOSTART_PATH")"
if [ -f "$SCRIPT_DIR/$DESKTOP_FILE" ]; then
    cp "$SCRIPT_DIR/$DESKTOP_FILE" "$AUTOSTART_PATH"
    mkdir -p "$(dirname "$APPS_PATH")"
    cp "$SCRIPT_DIR/$DESKTOP_FILE" "$APPS_PATH"
    echo "  Desktop: $AUTOSTART_PATH"
else
    echo "  Skipped: $DESKTOP_FILE not found"
fi

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
echo "  ddc-slider --version          # Show version"
echo "  man ddc-slider                # Full documentation"
echo
echo "Uninstall:"
echo "  $0 --uninstall"
echo
echo "The app will auto-start on next login."
echo "To launch now:  ddc-slider &"
