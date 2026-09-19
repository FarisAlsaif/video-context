# Setup and troubleshooting

## Install

Copy the skill folder into `~/.claude/skills/video-context/`. That's it: `scripts/run.sh` installs everything else on first use, without sudo:

1. It uses `uv` if present, otherwise installs it into `~/.local/bin` (official installer from astral.sh).
2. `uv` provides Python 3.12, `pillow`, `faster-whisper` and `imageio-ffmpeg` (a static ffmpeg, so Homebrew/apt ffmpeg isn't needed; a system ffmpeg is still used if present). On Apple Silicon it also adds `mlx-whisper`. If that fails to install, it's skipped permanently and faster-whisper is used.
3. If uv can't be installed (no internet to astral.sh, no curl/wget), it falls back to a venv at `~/.cache/video-context/venv` with pip. On Ubuntu/WSL this needs `python3-venv` (`sudo apt install -y python3-venv`), which is the only case needing sudo.
4. The speech model downloads from huggingface.co on the first transcription, or ahead of time with `run.sh --setup`.

State lives in `~/.cache/video-context/`. Delete it to force setup to run again.

Environment switches: `VIDEO_CONTEXT_NO_MLX=1` (never use MLX), `VIDEO_CONTEXT_MODEL`, `VIDEO_CONTEXT_LANG`, `VIDEO_CONTEXT_MAX_FRAMES`, `VIDEO_CONTEXT_BACKEND`.

## Recording tips

- **macOS:** Cmd+Shift+5 → "Record Entire Screen" or "Record Selected Portion". **Options → Microphone must be set** (it's "None" by default). QuickTime → File → New Screen Recording also works.
- **Windows (for WSL users):** The Snipping Tool (Win+Shift+R) saves to `Videos\Screen Recordings`. Turn the mic on in its toolbar. The Xbox Game Bar (Win+Alt+R) saves to `Videos\Captures`, but it records one app window only, not the desktop.
- Recording a single window or region gives larger, more readable frames than a full ultra-wide screen.
- Pause for a second on what you're pointing at, and hover or select the thing you mean. Frames are taken when the screen settles and at moments you're speaking, so a highlighted selection is captured.

## Models

| model | download | notes |
|---|---|---|
| small | ~0.5 GB | fast, but garbles technical words and merges speech bursts; not recommended |
| **large-v3-turbo** (default) | ~0.8–1.6 GB | the best balance; handles Arabic and English |
| large-v3 | ~1.5–3 GB | slowest and most accurate; use it on a `--start/--end` window when a line matters |

Sizes depend on the backend (MLX or faster-whisper). Models are cached after the first download. On Apple Silicon, turbo through MLX runs much faster than real time. On a WSL CPU, expect roughly real time or somewhat faster.

Override with `export VIDEO_CONTEXT_MODEL=…` (and optionally `VIDEO_CONTEXT_LANG=ar`).

## Troubleshooting

- **Model download fails:** The first run fetches the model from huggingface.co. Behind a proxy, set `HTTPS_PROXY`. The model is cached afterwards (`~/.cache/huggingface`).
- **Too many or too few frames:** `--change` (default 0.02, the share of the screen that must change) controls the main frames, and `--anchor` (default 0.0005) controls the extra frames taken while you speak. Lower values give more frames.
- **Slow on WSL:** Reading large files from `/mnt/c` is slower. Copying the video into the Linux filesystem first helps for long recordings.
- **Wrong recording picked by `--latest`:** Pass the path explicitly.
