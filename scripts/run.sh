#!/usr/bin/env bash
# video-context entry point. Makes sure everything is installed, then runs the script.
#
#   run.sh <video> [options]   set up if needed, then process the video
#   run.sh --latest [options]  same, newest screen recording on this machine
#   run.sh --setup             set up only, and pre-download the speech model
#
# Setup needs no sudo, Homebrew or apt: uv is installed into ~/.local/bin, and uv
# provides Python, the Python packages and a bundled ffmpeg in its own cache.
# Everything after the first run is instant.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/video_to_context.py"
STATE="${XDG_CACHE_HOME:-$HOME/.cache}/video-context"
mkdir -p "$STATE"
log() { echo "[video-context] $*" >&2; }

# ---------------------------------------------------------------- uv
ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$p" ]; then export PATH="$(dirname "$p"):$PATH"; return 0; fi
  done
  log "setup: installing uv into ~/.local/bin (one time, no sudo) …"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >&2
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh >&2
  else
    log "setup: neither curl nor wget is available to download uv"
    return 1
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1
}

# ------------------------------------------------ fallback: plain venv
venv_python() {
  local venv="$STATE/venv"
  if [ ! -x "$venv/bin/python" ]; then
    command -v python3 >/dev/null 2>&1 || return 1
    log "setup: creating a Python venv at $venv (one time) …"
    python3 -m venv "$venv" >&2 || {
      log "setup: python3 -m venv failed (on Ubuntu/WSL this needs: sudo apt install python3-venv)"
      rm -rf "$venv"; return 1; }
  fi
  if ! "$venv/bin/python" -c "import PIL, faster_whisper, imageio_ffmpeg" 2>/dev/null; then
    log "setup: installing Python packages into the venv (one time, 1-2 min) …"
    "$venv/bin/python" -m pip install -q --upgrade pip >&2
    "$venv/bin/python" -m pip install -q "pillow>=10" "faster-whisper>=1.1" "imageio-ffmpeg>=0.5" >&2 || return 1
  fi
  echo "$venv/bin/python"
}

# ------------------------------------------------------------- main
BASE=(--with "pillow>=10" --with "faster-whisper>=1.1" --with "imageio-ffmpeg>=0.5")
EXTRA=()
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] \
   && [ -z "${VIDEO_CONTEXT_NO_MLX:-}" ] && [ ! -f "$STATE/no-mlx" ]; then
  EXTRA=(--with "mlx-whisper")   # much faster transcription on Apple Silicon
fi

RUNNER=()
if ensure_uv; then
  UVRUN=(uv run --quiet --no-project --python 3.12)
  if [ ! -f "$STATE/ready" ]; then
    log "setup: first run — preparing Python 3.12, whisper and ffmpeg (one time, 1-3 min) …"
  fi
  if ! "${UVRUN[@]}" "${BASE[@]}" "${EXTRA[@]}" python -c "import PIL, faster_whisper, imageio_ffmpeg" >&2; then
    if [ ${#EXTRA[@]} -gt 0 ]; then
      log "setup: mlx-whisper couldn't be installed, continuing with faster-whisper"
      touch "$STATE/no-mlx"; EXTRA=()
      "${UVRUN[@]}" "${BASE[@]}" python -c "import PIL, faster_whisper, imageio_ffmpeg" >&2 || UVRUN=()
    else
      UVRUN=()
    fi
  fi
  if [ ${#UVRUN[@]} -gt 0 ]; then
    RUNNER=("${UVRUN[@]}" "${BASE[@]}" "${EXTRA[@]}" python)
  fi
fi

if [ ${#RUNNER[@]} -eq 0 ]; then
  log "setup: uv route unavailable, trying a plain Python venv …"
  PY="$(venv_python)" || {
    log "SETUP FAILED: could not install dependencies automatically."
    log "Fix manually with ONE of:"
    log "  curl -LsSf https://astral.sh/uv/install.sh | sh      (then re-run)"
    log "  macOS: brew install uv    |   WSL: sudo apt install -y python3-venv"
    echo "SETUP=failed"
    exit 3
  }
  RUNNER=("$PY")
fi

if [ ! -f "$STATE/ready" ]; then
  touch "$STATE/ready"
  log "setup: dependencies ready"
fi

if [ "${1:-}" = "--setup" ]; then
  shift
  "${RUNNER[@]}" "$SCRIPT" --prefetch-model "$@" || {
    log "setup: model download failed (needs internet access to huggingface.co); it will be retried on the next run"
    echo "SETUP=deps-only"; exit 0; }
  echo "SETUP=ok"
  exit 0
fi

exec "${RUNNER[@]}" "$SCRIPT" "$@"
