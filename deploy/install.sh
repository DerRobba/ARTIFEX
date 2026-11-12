#!/usr/bin/env bash
# =============================================================================
#  ARTIFEX installer for Raspberry Pi OS (Bookworm/Trixie, 64-bit)
#
#  Prerequisite: Klipper + Moonraker installed with KIAUH
#     https://github.com/dw-0/kiauh   (Mainsail/Fluidd optional)
#
#  What it does:
#   1. system packages (OpenSCAD, Chromium, cage kiosk compositor, ...)
#   2. Python venv ~/artifex-env with the ARTIFEX host software
#   3. Klipper config: links klipper/artifex -> ~/printer_data/config/artifex,
#      copies printer.cfg + artifex.yaml if they do not exist yet
#   4. Moonraker: update manager entry + allowed services
#   5. systemd: artifex.service (web/API on :8080) and artifex-kiosk.service
#      (fullscreen touch UI on the attached display)
#
#  Usage:  ./deploy/install.sh [--no-kiosk]
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$HOME"
PRINTER_DATA="${PRINTER_DATA:-$HOME_DIR/printer_data}"
VENV="$HOME_DIR/artifex-env"
KIOSK=1
[[ "${1:-}" == "--no-kiosk" ]] && KIOSK=0

info() { printf '\e[1;33m==> %s\e[0m\n' "$*"; }
warn() { printf '\e[1;31m!!  %s\e[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  warn "Please run as the normal user (e.g. pi), not as root."
  exit 1
fi

# ----------------------------------------------------------------- packages
info "Installing system packages"
CHROMIUM_PKG=chromium
if apt-cache show chromium-browser >/dev/null 2>&1 && ! apt-cache show chromium >/dev/null 2>&1; then
  CHROMIUM_PKG=chromium-browser
fi
PKGS=(python3-venv python3-dev build-essential git openscad fonts-liberation libgl1 libglib2.0-0)
if [[ $KIOSK -eq 1 ]]; then
  PKGS+=(cage "$CHROMIUM_PKG" seatd)
fi
sudo apt-get update
sudo apt-get install -y "${PKGS[@]}"

# ----------------------------------------------------------------- python
info "Creating Python environment in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -e "$REPO_DIR"
# optional accelerators for mesh repair/decimation (wheels exist for aarch64)
"$VENV/bin/pip" install mapbox_earcut fast_simplification scikit-image manifold3d rtree \
  || warn "Optional mesh packages failed - ARTIFEX still works, mesh repair is less capable."

# ----------------------------------------------------------------- klipper config
info "Installing Klipper configuration"
CFG="$PRINTER_DATA/config"
mkdir -p "$CFG" "$PRINTER_DATA/gcodes" "$PRINTER_DATA/artifex"
if [[ -e "$CFG/artifex" && ! -L "$CFG/artifex" ]]; then
  warn "$CFG/artifex exists and is not a symlink - moving it to artifex.bak"
  mv "$CFG/artifex" "$CFG/artifex.bak.$(date +%s)"
fi
ln -sfn "$REPO_DIR/klipper/artifex" "$CFG/artifex"
if [[ -f "$CFG/printer.cfg" ]] && ! grep -q "include artifex/" "$CFG/printer.cfg"; then
  cp "$CFG/printer.cfg" "$CFG/printer.cfg.before-artifex"
  warn "Existing printer.cfg saved as printer.cfg.before-artifex and replaced."
  cp "$REPO_DIR/klipper/printer.cfg" "$CFG/printer.cfg"
elif [[ ! -f "$CFG/printer.cfg" ]]; then
  cp "$REPO_DIR/klipper/printer.cfg" "$CFG/printer.cfg"
fi
[[ -f "$CFG/artifex.yaml" ]] || cp "$REPO_DIR/deploy/artifex.example.yaml" "$CFG/artifex.yaml"

# ----------------------------------------------------------------- moonraker
info "Configuring Moonraker"
MCONF="$CFG/moonraker.conf"
if [[ -f "$MCONF" ]] && ! grep -q "update_manager artifex" "$MCONF"; then
  cat >> "$MCONF" <<EOF

# --- ARTIFEX ------------------------------------------------------------------
[update_manager artifex]
type: git_repo
path: $REPO_DIR
origin: $(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo https://github.com/DerRobba/ARTIFEX.git)
primary_branch: main
virtualenv: $VENV
requirements: deploy/requirements-pi.txt
managed_services: artifex
EOF
fi
ASVC="$PRINTER_DATA/moonraker.asvc"
touch "$ASVC"
for svc in artifex artifex-kiosk; do
  grep -qx "$svc" "$ASVC" || echo "$svc" >> "$ASVC"
done

# ----------------------------------------------------------------- systemd
info "Installing systemd services"
render() {
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@HOME@|$HOME_DIR|g" -e "s|@REPO@|$REPO_DIR|g" \
      -e "s|@VENV@|$VENV|g" -e "s|@PRINTER_DATA@|$PRINTER_DATA|g" -e "s|@CHROMIUM@|$(command -v chromium || command -v chromium-browser || echo /usr/bin/chromium)|g" "$1"
}
render "$REPO_DIR/deploy/artifex.service" | sudo tee /etc/systemd/system/artifex.service >/dev/null
if [[ $KIOSK -eq 1 ]]; then
  render "$REPO_DIR/deploy/artifex-kiosk.service" | sudo tee /etc/systemd/system/artifex-kiosk.service >/dev/null
  sudo usermod -aG video,render,input,tty "$USER_NAME" || true
  sudo systemctl enable seatd 2>/dev/null || true
fi
sudo systemctl daemon-reload
sudo systemctl enable --now artifex.service
if [[ $KIOSK -eq 1 ]]; then
  sudo systemctl disable getty@tty1.service 2>/dev/null || true
  sudo systemctl enable artifex-kiosk.service
  sudo systemctl set-default graphical.target
fi
sudo systemctl restart moonraker 2>/dev/null || true

info "Done!"
echo "  Web UI / API : http://$(hostname -I | awk '{print $1}'):8080"
echo "  Config       : $CFG/artifex.yaml  (LLM endpoint, GPU inference server)"
echo "  Klipper      : $CFG/printer.cfg   (set the MCU serial, calibrate!)"
[[ $KIOSK -eq 1 ]] && echo "  Display      : starts on next boot (or: sudo systemctl start artifex-kiosk)"
