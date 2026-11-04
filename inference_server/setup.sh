#!/usr/bin/env bash
# Sets up the ARTIFEX GPU inference server on a Linux machine with an NVIDIA GPU.
# Usage: ./setup.sh [cuda-index-url]   (default: CUDA 12.4 wheels)
set -euo pipefail
cd "$(dirname "$0")"

TORCH_INDEX="${1:-https://download.pytorch.org/whl/cu124}"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip wheel
pip install torch torchvision --index-url "$TORCH_INDEX"
pip install -r requirements.txt

if [ ! -d Hunyuan3D-2.1 ]; then
  git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git
fi
# Only the shape-generation part is needed for printing (no texture painting).
pip install -r Hunyuan3D-2.1/requirements.txt || echo "WARN: some Hunyuan3D requirements failed - shape generation may still work"

cat <<'EOF'

Done. Start the server with:
  source .venv/bin/activate
  ARTIFEX_INFERENCE_KEY=<secret> python server.py --port 8765 [--low-vram]

Then set on the printer (artifex.yaml):
  inference:
    url: http://<this-machine>:8765
    api_key: <secret>
EOF
