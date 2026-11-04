#!/usr/bin/env bash
# Runs the ARTIFEX Klipper configuration + real slicer output through Klipper's
# batch mode (no printer needed). Builds the RP2040 firmware dictionary once.
#
#   ./scripts/validate-klipper.sh            # uses ./.klipper-check as work dir
#
# Needs: git, python3-venv, gcc-arm-none-eabi (+ libnewlib-arm-none-eabi)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$ROOT/.klipper-check}"
mkdir -p "$WORK"

if [[ ! -d "$WORK/klipper" ]]; then
  git clone --depth 1 https://github.com/Klipper3d/klipper.git "$WORK/klipper"
fi
if [[ ! -f "$WORK/klipper/out/klipper.dict" ]]; then
  cat > "$WORK/klipper/.config" <<'EOF'
CONFIG_MACH_RPXXXX=y
CONFIG_MACH_RP2040=y
CONFIG_RPXXXX_USB=y
EOF
  make -C "$WORK/klipper" olddefconfig >/dev/null
  make -C "$WORK/klipper" -j"$(nproc)"
fi
if [[ ! -x "$WORK/kenv/bin/python" ]]; then
  python3 -m venv "$WORK/kenv"
  "$WORK/kenv/bin/pip" install -q -r "$WORK/klipper/scripts/klippy-requirements.txt"
fi

KLIPPER_DIR="$WORK/klipper" KLIPPY_PYTHON="$WORK/kenv/bin/python" \
  python3 -m pytest -q "$ROOT/tests/test_klipper_config.py"
