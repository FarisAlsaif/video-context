#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "pillow>=10",
#   "faster-whisper>=1.1",
#   "imageio-ffmpeg>=0.5",
# ]
# ///
"""
video_to_context.py — turn a screen recording into context for a Claude Code session.

What it produces (in the output directory):
  context.md        timeline: every transcript line grouped under the frame that was
                    on screen when it was spoken
  frames/*.jpg      deduplicated keyframes (only moments where the screen changed)
  transcript.json   raw segments [{start, end, text}]
  meta.json         video info, settings, backend used

Works on macOS and WSL (and plain Linux). Uses ffmpeg from PATH if present, otherwise the
static ffmpeg bundled with the imageio-ffmpeg package (no sudo / Homebrew needed).
Normally launched through run.sh, which installs everything on first use.
Transcription backends, tried in order: mlx-whisper (Apple Silicon), faster-whisper,
openai-whisper. If none is available it still extracts frames and says so.

Usage:
  uv run video_to_context.py <video>            # recommended (auto-installs deps)
  python3 video_to_context.py <video>
  python3 video_to_context.py --latest          # newest screen recording on this machine
  python3 video_to_context.py "C:\\Users\\me\\Videos\\Captures\\clip.mp4"   # Windows path in WSL
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".webm", ".m4v", ".avi"}


def log(msg: str) -> None:
    print(f"[video-context] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


# --------------------------------------------------------------------------- env

def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def is_macos() -> bool:
    return sys.platform == "darwin"


def windows_home_in_wsl() -> list[Path]:
    homes: list[Path] = []
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", "echo %USERPROFILE%"],
            capture_output=True, text=True, timeout=10, cwd="/mnt/c",
        ).stdout.strip()
        if out and "%" not in out:
            conv = subprocess.run(["wslpath", "-u", out], capture_output=True, text=True).stdout.strip()
            if conv:
                homes.append(Path(conv))
    except (OSError, subprocess.SubprocessError):
        pass
    if not homes:
        for p in glob.glob("/mnt/c/Users/*"):
            name = os.path.basename(p)
            if name not in {"Public", "Default", "Default User", "All Users", "desktop.ini"}:
                homes.append(Path(p))
    return homes


_ODD_SPACES = "\u202f\u00a0\u2007\u2009\u200a\u2002\u2003"


def _norm_name(name: str) -> str:
    """Treat all space-like characters as one ordinary space (for matching only)."""
    name = unicodedata.normalize("NFC", name)
    for ch in _ODD_SPACES:
        name = name.replace(ch, " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def resolve_video_path(raw: str) -> Path:
    raw = raw.strip().strip('"').strip("'")
    # Windows path given inside WSL: C:\... or C:/...
    if is_wsl() and re.match(r"^[A-Za-z]:[\\/]", raw):
        conv = subprocess.run(["wslpath", "-u", raw], capture_output=True, text=True).stdout.strip()
        if conv:
            raw = conv
    # Paths dragged into a macOS terminal escape spaces with backslashes
    if not os.path.exists(raw) and "\\ " in raw:
        raw = raw.replace("\\ ", " ")
    p = Path(os.path.expanduser(raw))
    if p.exists():
        return p.resolve()
    # macOS names recordings "Screen Recording 2026-09-19 at 3.14.07\u202fPM.mov" — the
    # character before AM/PM is a narrow no-break space, so a retyped or copied path
    # with a normal space doesn't match. Compare names with all spaces normalised.
    parent = p.parent if str(p.parent) not in ("", ".") else Path.cwd()
    if parent.is_dir():
        want = _norm_name(p.name)
        hits = [f for f in parent.iterdir() if _norm_name(f.name) == want]
        if len(hits) == 1:
            log(f"matched {hits[0].name!r} (filename contains non-standard spaces)")
            return hits[0].resolve()
    die(f"video not found: {raw}  (tip: use --latest, or quote the path)")


def candidate_recording_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = Path.home()
    if is_macos():
        try:
            loc = subprocess.run(
                ["defaults", "read", "com.apple.screencapture", "location"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if loc:
                dirs.append(Path(os.path.expanduser(loc)))
        except (OSError, subprocess.SubprocessError):
            pass
        dirs += [home / "Desktop", home / "Movies", home / "Downloads", home / "Documents"]
    if is_wsl():
        for wh in windows_home_in_wsl():
            for base in (wh, wh / "OneDrive"):
                dirs += [
                    base / "Videos" / "Captures",          # Xbox Game Bar (Win+Alt+R)
                    base / "Videos" / "Screen Recordings",  # Snipping Tool (Win+Shift+R)
                    base / "Videos",
                    base / "Desktop",
                    base / "Downloads",
                ]
    dirs += [home / "Videos", home / "Desktop", home / "Downloads", Path.cwd()]
    seen, out = set(), []
    for d in dirs:
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def find_latest_recording() -> Path:
    newest, newest_m = None, -1.0
    for d in candidate_recording_dirs():
        try:
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    m = f.stat().st_mtime
                    if m > newest_m:
                        newest, newest_m = f, m
        except OSError:
            continue
    if newest is None:
        die("no video files found in the usual screen-recording folders; pass a path instead")
    age_min = (time.time() - newest_m) / 60
    log(f"latest recording: {newest} (modified {age_min:.0f} min ago)")
    return newest.resolve()


# ------------------------------------------------------------------------ ffmpeg

FFMPEG = "ffmpeg"


def require_ffmpeg() -> None:
    global FFMPEG
    found = shutil.which("ffmpeg")
    if found:
        FFMPEG = found
        return
    try:
        import imageio_ffmpeg  # type: ignore
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return
    except Exception:
        pass
    die("ffmpeg not available — launch through run.sh (it installs a bundled ffmpeg), "
        "or install it: " + ("brew install ffmpeg" if is_macos() else "sudo apt install -y ffmpeg"))


def probe(video: Path) -> dict:
    # Parse `ffmpeg -i` output so ffprobe isn't needed (the bundled ffmpeg has no ffprobe).
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    err = r.stderr
    vline = next((l for l in err.splitlines() if re.search(r"Stream #.*: Video:", l)), None)
    if vline is None:
        die(f"no video stream found (ffmpeg says: {err.strip()[-300:]})")
    res = re.search(r"\b(\d{2,5})x(\d{2,5})\b", vline)
    dm = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    dur = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)) if dm else 0.0
    ct = re.search(r"creation_time\s*:\s*(\S+)", err)
    return {
        "duration": dur,
        "width": int(res.group(1)) if res else None,
        "height": int(res.group(2)) if res else None,
        "has_audio": bool(re.search(r"Stream #.*: Audio:", err)),
        "creation_time": ct.group(1) if ct else None,
    }


def auto_sample_fps(duration: float) -> float:
    if duration <= 180:
        return 2.0
    if duration <= 900:
        return 1.0
    return 0.5


def window_args(start: float, end: float | None) -> list[str]:
    a = ["-ss", f"{start:.3f}"] if start else []
    if end:
        a += ["-to", f"{end:.3f}"]
    return a


def extract_samples(video: Path, fps: float, width: int, dest: Path,
                    start: float = 0.0, end: float | None = None) -> list[tuple[float, Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    vf = f"fps={fps},scale='min({width},iw)':-2"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", *window_args(start, end), "-i", str(video),
        "-an", "-vf", vf, "-q:v", "3", str(dest / "s_%06d.jpg"),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"frame extraction failed: {r.stderr.strip()[:400]}")
    files = sorted(dest.glob("s_*.jpg"))
    # With the fps filter, output frame k (0-based) represents time start + k / fps.
    return [(start + i / fps, f) for i, f in enumerate(files)]


def grab_frames(video: Path, times: list[float], crop: str | None, dest: Path) -> list[Path]:
    """Full-resolution PNGs at exact times, optionally cropped (x,y,w,h in source pixels)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for t in times:
        tag = ts(t).replace(":", "m") + "s" + (f"_crop{crop.replace(',', '-')}" if crop else "")
        f = dest / f"grab_{tag}.png"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
               "-frames:v", "1"]
        if crop:
            x, y, w, h = [int(v) for v in crop.split(",")]
            cmd += ["-vf", f"crop={w}:{h}:{x}:{y}"]
        r = subprocess.run(cmd + [str(f)], capture_output=True, text=True)
        if r.returncode != 0 or not f.exists():
            die(f"could not grab frame at {ts(t)}: {r.stderr.strip()[:300]}")
        out.append(f)
    return out


def parse_time(v: str) -> float:
    parts = [float(x) for x in v.strip().split(":")]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


# ----------------------------------------------------------------- frame choice

def _thumb(path: Path):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("L").resize((384, 216))


def _diff(a, b, pixel_tol: int = 18) -> float:
    """Fraction of pixels that changed noticeably between two thumbnails."""
    from PIL import ImageChops
    d = ImageChops.difference(a, b).point(lambda v: 255 if v > pixel_tol else 0)
    hist = d.histogram()
    return hist[255] / float(a.size[0] * a.size[1])


def select_keyframes(samples, change_thresh: float, settle_thresh: float, max_gap: float,
                     max_frames: int, anchors=(), anchor_thresh: float = 0.0005
                     ) -> list[tuple[float, Path, float]]:
    """
    Pass 1 — keep a sample when the screen has *settled* (little change from the
    previous sample) and differs meaningfully from the last kept frame. This skips the
    blur of scrolling/typing and keeps the resulting state. `max_gap` forces a frame
    during long continuously-changing stretches (e.g. a video playing on screen).

    Pass 2 — speech anchors: for each moment the narrator is talking, if the screen
    then differs even slightly (anchor_thresh) from the frame that would represent it,
    add that moment too. Small but important changes (a typed command, a highlighted
    line) are usually what the speaker is pointing at.
    """
    if not samples:
        return []
    thumbs = [_thumb(p) for _, p in samples]
    keep = {0: 1.0}
    last_i = 0
    for i in range(1, len(samples)):
        vs_prev = _diff(thumbs[i], thumbs[i - 1])
        vs_kept = _diff(thumbs[i], thumbs[last_i])
        settled = vs_prev <= settle_thresh
        long_gap = samples[i][0] - samples[last_i][0] >= max_gap
        if vs_kept >= change_thresh and (settled or long_gap):
            keep[i] = vs_kept
            last_i = i
    if _diff(thumbs[-1], thumbs[last_i]) >= change_thresh:
        keep[len(samples) - 1] = 1.0
    base = set(keep)

    step = samples[1][0] - samples[0][0] if len(samples) > 1 else 1.0
    for a in anchors:
        i = min(len(samples) - 1, max(0, int(round((a - samples[0][0]) / step))))
        active = max(k for k in keep if k <= i) if any(k <= i for k in keep) else 0
        if i != active:
            d = _diff(thumbs[i], thumbs[active])
            if d >= anchor_thresh:
                keep[i] = d

    order = sorted(keep)
    if len(order) > max_frames:
        # Drop speech anchors first (smallest change first), then thin evenly in time.
        extras = sorted((k for k in order if k not in base), key=lambda k: keep[k])
        while len(order) > max_frames and extras:
            order.remove(extras.pop(0))
        if len(order) > max_frames:
            idx = sorted({round(k * (len(order) - 1) / (max_frames - 1)) for k in range(max_frames)})
            order = [order[j] for j in idx]
    return [(samples[i][0], samples[i][1], keep[i]) for i in order]


def load_transcript_file(path: Path) -> list[dict]:
    """Read segments from .json ([{start,end,text}]), .srt or .vtt."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip(),
                 "conf": s.get("conf")} for s in json.loads(text)]

    def secs(t: str) -> float:
        t = t.replace(",", ".")
        parts = [float(x) for x in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    segs = []
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)[^\n]*\n(.+)", block, re.S)
        if m:
            body = re.sub(r"<[^>]+>", "", m.group(3)).strip().replace("\n", " ")
            if body:
                segs.append({"start": secs(m.group(1)), "end": secs(m.group(2)), "text": body})
    return segs


# ---------------------------------------------------------------- transcription

def extract_audio(video: Path, dest: Path, start: float = 0.0, end: float | None = None) -> Path:
    wav = dest / "audio.wav"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *window_args(start, end), "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"audio extraction failed: {r.stderr.strip()[:400]}")
    return wav


def audio_is_silent(wav: Path) -> bool:
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return bool(m and float(m.group(1)) < -50.0)


DEFAULT_MODEL = "large-v3-turbo"
_MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


def mlx_repo(model: str) -> str:
    return model if "/" in model else _MLX_REPOS.get(model, f"mlx-community/whisper-{model}-mlx")


def fw_model_name(model: str) -> str:
    return "large-v3-turbo" if model == "turbo" else model


def prefetch_model(backend: str, model: str) -> None:
    """Download the speech model now so the first real run doesn't stall."""
    use_mlx = backend in ("auto", "mlx") and is_macos() and platform.machine() == "arm64"
    if use_mlx:
        try:
            from huggingface_hub import snapshot_download  # type: ignore
            import mlx_whisper  # noqa: F401  # type: ignore
            log(f"downloading {mlx_repo(model)} …")
            snapshot_download(mlx_repo(model))
            print(f"MODEL_READY=mlx:{mlx_repo(model)}")
            return
        except ImportError:
            pass
    from faster_whisper import WhisperModel  # type: ignore
    log(f"downloading faster-whisper model '{fw_model_name(model)}' …")
    WhisperModel(fw_model_name(model), device="cpu", compute_type="int8")
    print(f"MODEL_READY=faster-whisper:{fw_model_name(model)}")


_SENT_END = (".", "?", "!", "\u061f", "\u060c", ",", "\u3002")  # incl. Arabic ؟ and ،


def split_on_pauses(raw_segs: list[dict], gap: float = 0.6, max_len: float = 12.0) -> list[dict]:
    """
    Whisper often merges several bursts of speech into one long segment, which blurs
    which frame a sentence belongs to. Using word timings, cut wherever the speaker
    paused for `gap` seconds (or at punctuation once a piece exceeds max_len), and
    score each piece by its mean word probability.
    """
    out = []

    def emit(words, seg):
        text = "".join(w["word"] for w in words).strip()
        if not text:
            return
        probs = [w["prob"] for w in words if w.get("prob") is not None]
        conf = sum(probs) / len(probs) if probs else None
        out.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text,
                    "conf": round(conf, 2) if conf is not None else None})

    for seg in raw_segs:
        words = seg.get("words") or []
        if not words:
            conf = seg.get("conf")
            out.append({"start": seg["start"], "end": seg["end"], "text": seg["text"].strip(),
                        "conf": conf})
            continue
        cur = [words[0]]
        for w in words[1:]:
            paused = w["start"] - cur[-1]["end"] >= gap
            too_long = (w["end"] - cur[0]["start"] > max_len
                        and cur[-1]["word"].rstrip().endswith(_SENT_END))
            if paused or too_long:
                emit(cur, seg)
                cur = [w]
            else:
                cur.append(w)
        emit(cur, seg)
    return [o for o in out if o["text"]]


def _logprob_conf(lp):
    # rough mapping of avg log-prob to 0..1 when word probabilities are missing
    import math
    return round(math.exp(lp), 2) if lp is not None else None


def transcribe(wav: Path, backend: str, model: str, lang: str | None, prompt: str | None):
    """Returns (segments, backend_used, detected_language)."""
    order = [backend] if backend != "auto" else (
        ["mlx", "faster-whisper", "openai-whisper"]
        if is_macos() and platform.machine() == "arm64"
        else ["faster-whisper", "openai-whisper"]
    )
    errors = []

    def from_dicts(segs):
        return [{"start": s["start"], "end": s["end"], "text": s["text"],
                 "conf": _logprob_conf(s.get("avg_logprob")),
                 "words": [{"word": w["word"], "start": w["start"], "end": w["end"],
                            "prob": w.get("probability")} for w in (s.get("words") or [])]}
                for s in segs]

    for b in order:
        try:
            if b == "mlx":
                import mlx_whisper  # type: ignore
                log(f"transcribing with mlx-whisper ({mlx_repo(model)}) — first run downloads the model …")
                res = mlx_whisper.transcribe(str(wav), path_or_hf_repo=mlx_repo(model), language=lang,
                                             initial_prompt=prompt, word_timestamps=True)
                return split_on_pauses(from_dicts(res.get("segments", []))), "mlx-whisper", res.get("language")
            if b == "faster-whisper":
                from faster_whisper import WhisperModel  # type: ignore
                name = fw_model_name(model)
                log(f"transcribing with faster-whisper ({name}) — first run downloads the model …")
                wm = WhisperModel(name, device="auto", compute_type="int8")
                it, info = wm.transcribe(str(wav), language=lang, initial_prompt=prompt,
                                         vad_filter=True, beam_size=5, word_timestamps=True)
                raw = [{"start": s.start, "end": s.end, "text": s.text,
                        "conf": _logprob_conf(s.avg_logprob),
                        "words": [{"word": w.word, "start": w.start, "end": w.end, "prob": w.probability}
                                  for w in (s.words or [])]} for s in it]
                return split_on_pauses(raw), "faster-whisper", info.language
            if b == "openai-whisper":
                import whisper  # type: ignore
                name = "turbo" if model in ("turbo", "large-v3-turbo") else model
                log(f"transcribing with openai-whisper ({name}) …")
                res = whisper.load_model(name).transcribe(str(wav), language=lang, initial_prompt=prompt,
                                                          word_timestamps=True)
                return split_on_pauses(from_dicts(res.get("segments", []))), "openai-whisper", res.get("language")
        except ImportError as e:
            errors.append(f"{b}: not installed ({e.name})")
        except Exception as e:  # model download / runtime failure
            errors.append(f"{b}: {type(e).__name__}: {e}")
    log("no transcription backend worked:\n  " + "\n  ".join(errors))
    return None, "; ".join(errors), None


# ----------------------------------------------------------------------- output

def ts(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


LOW_CONF = 0.6


def write_context(out: Path, video: Path, info: dict, frames: list[dict],
                  segments, backend, language, notes: list[str]) -> Path:
    lines = [
        f"# Video context: {video.name}",
        "",
        f"- Source: `{video}`",
        f"- Duration: {ts(info['duration'])} · Resolution: {info['width']}x{info['height']}",
        f"- Keyframes: {len(frames)} (in `frames/`, only moments where the screen changed)",
    ]
    if segments is not None:
        low = sum(1 for x in segments if x.get("conf") is not None and x["conf"] < LOW_CONF)
        lines.append(f"- Transcript: {len(segments)} segments · backend: {backend} · language: {language}"
                     + (f" · {low} low-confidence line(s) marked ⚠" if low else ""))
    for n in notes:
        lines.append(f"- NOTE: {n}")
    lines += [
        "",
        "Each section below is one screen state. The narration listed under a frame was",
        "spoken (at its midpoint) while that frame was on screen, so words like \"this\" or",
        "\"here\" refer to what that frame shows.",
        "Open the frame images to see them. Lines marked ⚠ may be mis-heard: check them",
        "against the frames, and re-transcribe that window with a bigger model if one matters.",
        "",
        "## Timeline",
        "",
    ]
    segs = segments or []
    for i, fr in enumerate(frames):
        start = fr["t"]
        end = frames[i + 1]["t"] if i + 1 < len(frames) else float("inf")
        lines.append(f"### [{ts(start)}] {fr['file']}")
        mid = lambda s: (s["start"] + s["end"]) / 2
        said = [s for s in segs if (i == 0 or start <= mid(s)) and mid(s) < end]
        if said:
            for s in said:
                flag = " ⚠ low-confidence" if s.get("conf") is not None and s["conf"] < LOW_CONF else ""
                lines.append(f"- [{ts(s['start'])}–{ts(s['end'])}]{flag} {s['text']}")
        elif segments is not None:
            lines.append("- (no narration while this was on screen)")
        lines.append("")
    if segments is not None:
        lines += ["## Full transcript", ""]
        lines.append(" ".join(s["text"] for s in segs) if segs else "(empty)")
        lines.append("")
    path = out / "context.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Turn a screen recording into session context.")
    ap.add_argument("video", nargs="?", help="path to the video (Windows paths OK under WSL)")
    ap.add_argument("--latest", action="store_true", help="use the newest screen recording found")
    ap.add_argument("--out", help="output dir (default: ./.video-context/<name>)")
    ap.add_argument("--max-frames", type=int, default=int(os.environ.get("VIDEO_CONTEXT_MAX_FRAMES", 30)))
    ap.add_argument("--width", type=int, default=1280, help="max keyframe width in px")
    ap.add_argument("--sample-fps", type=float, default=0, help="sampling rate (0 = auto by length)")
    ap.add_argument("--change", type=float, default=0.02,
                    help="min fraction of screen that must change to keep a new frame")
    ap.add_argument("--settle", type=float, default=0.01,
                    help="max change vs previous sample for the screen to count as settled")
    ap.add_argument("--anchor", type=float, default=0.0005,
                    help="min change to add an extra frame at a moment of speech (catches typing)")
    ap.add_argument("--max-gap", type=float, default=20.0,
                    help="force a frame after this many seconds of continuous change")
    ap.add_argument("--backend", default=os.environ.get("VIDEO_CONTEXT_BACKEND", "auto"),
                    choices=["auto", "mlx", "faster-whisper", "openai-whisper"])
    ap.add_argument("--model", default=os.environ.get("VIDEO_CONTEXT_MODEL", DEFAULT_MODEL),
                    help=f"whisper model: tiny|base|small|medium|large-v3|large-v3-turbo "
                         f"(default {DEFAULT_MODEL})")
    ap.add_argument("--lang", default=os.environ.get("VIDEO_CONTEXT_LANG"),
                    help="spoken language code, e.g. en or ar (default: auto-detect)")
    ap.add_argument("--prompt", help="vocabulary hint for transcription (project names, jargon)")
    ap.add_argument("--transcript", help="use an existing .srt/.vtt/.json transcript instead of transcribing")
    ap.add_argument("--no-transcribe", action="store_true", help="frames only")
    ap.add_argument("--prefetch-model", action="store_true",
                    help="only download the speech model (used by run.sh --setup)")
    ap.add_argument("--start", help="only process from this time (seconds or mm:ss)")
    ap.add_argument("--end", help="only process up to this time (seconds or mm:ss)")
    ap.add_argument("--grab", help="comma-separated times: save full-resolution PNGs there and exit")
    ap.add_argument("--crop", help="with --grab: x,y,w,h crop in source pixels")
    ap.add_argument("--keep-audio", action="store_true", help="keep audio.wav in the output dir")
    args = ap.parse_args()

    if args.prefetch_model:
        prefetch_model(args.backend, args.model)
        return
    require_ffmpeg()
    if args.latest:
        video = find_latest_recording()
    elif args.video:
        video = resolve_video_path(args.video)
    else:
        ap.error("give a video path or --latest")

    info = probe(video)
    log(f"{video.name}: {ts(info['duration'])}, {info['width']}x{info['height']}, "
        f"audio={'yes' if info['has_audio'] else 'no'}")

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", video.stem).strip("-")[:60] or "video"
    base_out = Path(args.out) if args.out else Path.cwd() / ".video-context" / slug

    if args.grab:
        times = [parse_time(t) for t in args.grab.split(",") if t.strip()]
        for f in grab_frames(video, times, args.crop, base_out / "grabs"):
            print(f"GRAB={f}")
        return

    start = parse_time(args.start) if args.start else 0.0
    end = parse_time(args.end) if args.end else None
    out = base_out
    if start or end:
        out = base_out.parent / f"{base_out.name}__{ts(start).replace(':', 'm')}-" \
                                f"{ts(end if end else info['duration']).replace(':', 'm')}"
    if out.exists():
        shutil.rmtree(out)
    (out / "frames").mkdir(parents=True)
    root = out.parent if not args.out else None
    if root is not None and root.name == ".video-context":
        gi = root / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")  # recordings can show secrets; never commit this folder

    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vctx_") as tmp:
        tmpd = Path(tmp)

        # --- audio first, so speech timing can guide frame selection
        segments = backend = language = None
        if args.transcript:
            segments = [x for x in load_transcript_file(resolve_video_path(args.transcript))
                        if x["end"] > start and (end is None or x["start"] < end)]
            backend, language = f"file:{Path(args.transcript).name}", args.lang or "?"
        elif args.no_transcribe:
            notes.append("transcription skipped (--no-transcribe)")
        elif not info["has_audio"]:
            notes.append("video has no audio track — was the microphone enabled when recording?")
        else:
            wav = extract_audio(video, tmpd, start, end)
            if audio_is_silent(wav):
                notes.append("audio track is silent — the microphone was probably off while recording")
            else:
                segments, backend, language = transcribe(wav, args.backend, args.model,
                                                         args.lang, args.prompt)
                if segments is not None and start:
                    for x in segments:  # times relative to the whole video
                        x["start"] += start
                        x["end"] += start
                if segments is None:
                    why, backend = backend, None
                    if all("not installed" in e for e in why.split("; ")):
                        notes.append("no transcription backend installed — run the script with "
                                     "`uv run`, or `pip install faster-whisper`")
                    else:
                        notes.append(f"transcription failed ({why[:300]}) — first run needs internet "
                                     "to download the whisper model")
            if args.keep_audio:
                shutil.copy2(wav, out / "audio.wav")

        # --- frames
        span = (end or info["duration"]) - start
        fps = args.sample_fps or auto_sample_fps(span)
        log(f"sampling frames at {fps} fps …")
        samples = extract_samples(video, fps, args.width, tmpd / "samples", start, end)
        anchors = [(s["start"] + s["end"]) / 2 for s in (segments or [])]
        kept = select_keyframes(samples, args.change, args.settle, args.max_gap,
                                max(2, args.max_frames), anchors, args.anchor)
        if len(kept) >= args.max_frames:
            notes.append(f"frame cap ({args.max_frames}) reached; some screen states were dropped — "
                         f"re-run with a higher --max-frames for more detail")
        frames = []
        for n, (t, p, score) in enumerate(kept, 1):
            name = f"frame_{n:03d}_{ts(t).replace(':', 'm')}s.jpg"
            shutil.copy2(p, out / "frames" / name)
            frames.append({"t": round(t, 2), "file": f"frames/{name}", "change": round(score, 3)})
        log(f"kept {len(frames)} keyframes out of {len(samples)} samples")

    if segments is not None:
        (out / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    meta = {"video": str(video), **info, "window": [start, end], "sample_fps": fps, "frames": frames,
            "backend": backend, "language": language, "model": args.model, "notes": notes}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx = write_context(out, video, info, frames, segments, backend, language, notes)

    # stdout is for Claude: a short, parseable summary
    print(f"CONTEXT={ctx}")
    print(f"FRAMES_DIR={out / 'frames'}")
    print(f"FRAME_COUNT={len(frames)}")
    print(f"TRANSCRIPT_SEGMENTS={len(segments) if segments is not None else 'none'}")
    if segments:
        print("AUDIO=speech — the narration is the user's actual request; read it before concluding anything")
        low = sum(1 for x in segments if x.get("conf") is not None and x["conf"] < LOW_CONF)
        if low:
            print(f"LOW_CONFIDENCE_LINES={low}")
    for n in notes:
        print(f"NOTE={n}")


if __name__ == "__main__":
    main()
