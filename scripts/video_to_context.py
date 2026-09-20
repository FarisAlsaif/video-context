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
video_to_context.py — turn a screen recording into context for an AI coding agent
(or a human): keyframes, contact sheets, a timestamped transcript and context.md.

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

# Only used by --latest to find recordings; an explicit path can be any format ffmpeg reads.
VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".webm", ".m4v", ".avi", ".gif", ".flv", ".wmv", ".ts", ".3gp"}


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


# -------------------------------------------------------------------- wall clock

def recording_start(video: Path, info: dict):
    """
    When did the recording start? Lets a moment in the video be matched against
    server/app logs. Returns (datetime, source) or (None, None).
    Filenames written by the recorders are the start time in local time and are the
    most trustworthy; container metadata is UTC but some tools stamp it at the end.
    """
    from datetime import datetime, timezone
    name = _norm_name(video.name)
    pats = [
        # macOS: "screen recording 2026-09-19 at 3.14.07 pm.mov"
        (r"(\d{4}-\d{2}-\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2}) ?([ap]m)?", "mac"),
        # Windows Snipping Tool: "screen recording 2026-09-19 151407.mp4"
        (r"(\d{4}-\d{2}-\d{2}) (\d{2})(\d{2})(\d{2})\b", "win"),
        # Xbox Game Bar: "app 2026-09-19 15-14-07.mp4"
        (r"(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})", "win"),
    ]
    for pat, _ in pats:
        m = re.search(pat, name)
        if m:
            h = int(m.group(2))
            ampm = m.group(5) if m.lastindex and m.lastindex >= 5 else None
            if ampm == "pm" and h < 12:
                h += 12
            if ampm == "am" and h == 12:
                h = 0
            try:
                dt = datetime.strptime(f"{m.group(1)} {h:02d}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S")
                return dt.astimezone(), "filename (local time, recording start)"
            except ValueError:
                pass
    ct = info.get("creation_time")
    if ct:
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(), "file metadata (may mark the start or the end — verify against an on-screen clock)"
        except ValueError:
            pass
    return None, None


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
    w = int(res.group(1)) if res else None
    h = int(res.group(2)) if res else None
    # Phone recordings often store landscape pixels plus a rotation flag; ffmpeg
    # applies the rotation when decoding, so report the dimensions as displayed.
    rot = re.search(r"rotation of (-?[\d.]+) degrees", err) or re.search(r"rotate\s*:\s*(-?\d+)", err)
    rotation = int(round(float(rot.group(1)))) % 360 if rot else 0
    if rotation in (90, 270) and w and h:
        w, h = h, w
    if not dur:
        # Browser/MediaRecorder WebM files often have no duration in the header:
        # measure it by reading the stream without decoding.
        r2 = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video), "-map", "0:v:0", "-c", "copy",
                             "-f", "null", "-"], capture_output=True, text=True)
        times = re.findall(r"time=(\d+):(\d+):([\d.]+)", r2.stderr)
        if times:
            hh, mm, ss = times[-1]
            dur = int(hh) * 3600 + int(mm) * 60 + float(ss)
    return {
        "duration": dur,
        "rotation": rotation,
        "width": w,
        "height": h,
        "has_audio": bool(re.search(r"Stream #.*: Audio:", err)),
        "creation_time": ct.group(1) if ct else None,
    }


def auto_sample_fps(duration: float) -> float:
    # Analysis runs on small raw frames, so a high rate is cheap; it's what catches
    # a toast or an error flash that is on screen for less than a second.
    if duration <= 300:
        return 12.0
    if duration <= 1200:
        return 6.0
    return 3.0


def window_args(start: float, end: float | None) -> list[str]:
    a = ["-ss", f"{start:.3f}"] if start else []
    if end:
        a += ["-to", f"{end:.3f}"]  # as an input option, -to is a position in the source
    return a


# ----------------------------------------------------------------- frame choice

PIXEL_TOL = 22   # per-channel difference that counts as a changed pixel
CELL = 4         # analysis pixels per cell for locating the changed region


def _change(a, b):
    """
    Return (fraction of pixels changed, binary mask). Uses the largest of the
    R/G/B differences, not brightness: a field turning red or a green toast on a grey
    panel barely changes luma and is exactly what a bug recording is about.
    """
    from PIL import ImageChops
    r, g, bl = ImageChops.difference(a, b).split()
    m = ImageChops.lighter(ImageChops.lighter(r, g), bl).point(lambda v: 255 if v > PIXEL_TOL else 0)
    return m.histogram()[255] / float(a.size[0] * a.size[1]), m


def _bbox(mask, max_regions: int = 3):
    """
    Changed regions as a list of boxes (analysis pixels), largest first. Nearby cells
    are merged so a dialog comes out as one box; tiny isolated blobs (the mouse
    pointer, compression noise) are dropped whenever anything bigger changed.
    """
    from PIL import ImageFilter
    w, h = mask.size
    cw, ch = max(1, w // CELL), max(1, h // CELL)
    small = mask.resize((cw, ch), resample=2).point(lambda v: 255 if v >= 51 else 0)
    bridged = small.filter(ImageFilter.MaxFilter(5))  # join cells up to 2 apart
    act, br = small.load(), bridged.load()
    seen = set()
    comps = []
    for y in range(ch):
        for x in range(cw):
            if br[x, y] and (x, y) not in seen:
                stack, cells, box = [(x, y)], 0, [x, y, x, y]
                seen.add((x, y))
                while stack:
                    cx, cy = stack.pop()
                    if act[cx, cy]:
                        cells += 1
                        box = [min(box[0], cx), min(box[1], cy), max(box[2], cx), max(box[3], cy)]
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < cw and 0 <= ny < ch and br[nx, ny] and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                if cells:
                    comps.append((cells, box))
    if not comps:
        return []
    # merge boxes that overlap (one panel can split into border + content)
    merged = True
    while merged:
        merged = False
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                a, b = comps[i][1], comps[j][1]
                if a[0] <= b[2] + 1 and b[0] <= a[2] + 1 and a[1] <= b[3] + 1 and b[1] <= a[3] + 1:
                    comps[i] = (comps[i][0] + comps[j][0],
                                [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])])
                    del comps[j]
                    merged = True
                    break
            if merged:
                break
    comps.sort(key=lambda c: -c[0])
    if comps[0][0] >= 6:
        comps = [c for c in comps if c[0] >= 6]
    return [(b[0] * CELL, b[1] * CELL, min(w, (b[2] + 1) * CELL), min(h, (b[3] + 1) * CELL))
            for _, b in comps[:max_regions]]


def analysis_width(info: dict, area: int = 400 * 226) -> int:
    """Analysis frame sized by area, not width, so portrait (phone) and landscape
    recordings are judged by the same thresholds."""
    W, H = info["width"] or 1920, info["height"] or 1080
    return max(64, int(round((area * W / H) ** 0.5 / 2)) * 2)


def analyze(video: Path, info: dict, fps: float, aw: int, start: float, end: float | None,
            anchors: list[float], p) -> list[dict]:
    """
    Stream the video once at low resolution and decide which moments to keep.

    - Change is measured against the last *kept* frame, so slow drift accumulates.
      The baseline only advances by keeping a frame; nothing is folded in silently.
    - A frame is kept once the screen has changed AND come to rest for ~0.35 s:
      the state after the click, not the animation blur.
    - During continuous change (a video playing) a frame is forced every max_gap s.
    - Speech anchors: at every moment the narrator is talking, and ~1.5 s after each
      sentence (when the result of "click here" shows up), keep the screen if it
      differs even slightly from the last kept frame. That catches typed commands,
      a highlighted line, a changed value.
    """
    from PIL import Image
    W, H = info["width"] or 1920, info["height"] or 1080
    ah = max(2, int(round(aw * H / W / 2)) * 2)
    p._analysis_w = aw
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", *window_args(start, end), "-i", str(video),
           "-an", "-vf", f"fps={fps},scale={aw}:{ah}:flags=area", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = aw * ah * 3
    settle_need = max(1, int(round(0.35 * fps)))
    anchors = sorted(anchors)
    ai = 0
    kept: list[dict] = []
    base = prev = None
    settled_run = 0
    last_t = -1e9
    last = None
    onset = None  # first sample (since the last keep) where a real change began
    cand = None   # strongest sample of the change in progress: (t, img, frac, mask, n_samples)
    i = 0

    def keep(t, img, frac, mask, reason):
        nonlocal base, last_t, onset
        nonlocal cand
        kept.append({"t": t, "img": img, "change": frac, "onset": onset if onset is not None else t,
                     "bbox": _bbox(mask) if mask is not None else None, "reason": reason})
        base, last_t, onset, cand = img, t, None, None

    while True:
        buf = proc.stdout.read(size)
        if not buf or len(buf) < size:
            break
        img = Image.frombytes("RGB", (aw, ah), buf)
        t = start + i / fps
        if base is None:
            keep(t, img, 1.0, None, "start")
        else:
            vs_prev, _ = _change(img, prev)
            settled_run = settled_run + 1 if vs_prev <= p.settle else 0
            vs_base, mask = _change(img, base)
            if onset is None and vs_base >= p.change:
                onset = t
            if vs_base >= p.change:
                if cand is None or vs_base > cand[2]:
                    cand = (t, img, vs_base, mask, (cand[4] if cand else 0) + 1)
                else:
                    cand = cand[:4] + (cand[4] + 1,)
            hit_anchor = False
            while ai < len(anchors) and anchors[ai] <= t:
                hit_anchor, ai = True, ai + 1
            reason = None
            if vs_base >= p.change and settled_run >= settle_need and t - last_t >= p.min_gap:
                reason = "settled"
            elif vs_base >= p.change and onset is not None and t - onset >= p.max_gap \
                    and t - last_t >= p.max_gap:
                reason = "ongoing"  # still changing (spinner, animation, playing video)
            elif hit_anchor and vs_base >= p.anchor and t - last_t >= p.min_gap:
                reason = "speech"
            if reason:
                keep(t, img, vs_base, mask, reason)
            elif cand is not None and vs_base < p.change:
                # The screen changed and went back before it ever settled: a flash
                # (toast, error, closing dialog). Keep its strongest moment rather
                # than losing it — then the return to normal is a change of its own.
                if cand[4] >= 2:
                    ct, cimg, cfrac, cmask, _ = cand
                    keep(ct, cimg, cfrac, cmask, "transient")
                else:
                    cand = None
                    onset = None
        prev = img
        last = (t, img)
        i += 1
    err = proc.stderr.read().decode(errors="replace")
    proc.wait()
    if proc.returncode not in (0, None) and not kept:
        die(f"frame analysis failed: {err.strip()[:400]}")
    if last and base is not None and last[1] is not base:
        frac, mask = _change(last[1], base)
        if frac >= p.anchor:
            keep(last[0], last[1], frac, mask, "end")
    log(f"analysed {i} samples at {fps} fps, {len(kept)} moments selected")
    return prune(kept, p.max_frames)


def prune(kept: list[dict], max_frames: int) -> list[dict]:
    """Over budget: drop the least informative frames first, keep both ends, and
    recompute each survivor's change/box against the frame now before it."""
    weight = {"ongoing": 0.3, "transient": 0.8, "settled": 1.0, "speech": 1.0}
    changed = False
    while len(kept) > max_frames:
        inner = range(1, len(kept) - 1)
        j = min(inner, key=lambda k: max(kept[k]["change"], 0.01 if kept[k]["reason"] == "speech" else 0)
                * weight.get(kept[k]["reason"], 1.0))
        del kept[j]
        if j < len(kept):
            frac, mask = _change(kept[j]["img"], kept[j - 1]["img"])
            kept[j]["change"], kept[j]["bbox"] = frac, _bbox(mask)
        changed = True
    if changed:
        log(f"frame budget: kept {len(kept)}")
    return kept


def export_frames(video: Path, kept: list[dict], info: dict, aw: int, width: int,
                  dest: Path, annotate: bool) -> list[dict]:
    """Extract the chosen moments at full detail and mark what changed."""
    from PIL import Image, ImageDraw
    dest.mkdir(parents=True, exist_ok=True)
    W = info["width"] or 1920
    frames = []
    for n, k in enumerate(kept, 1):
        name = f"frame_{n:03d}_{ts(k['t']).replace(':', 'm')}s.jpg"
        f = dest / name
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{k['t']:.3f}",
               "-i", str(video), "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(f)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not f.exists():  # seeking past the last frame etc.
            k["img"].resize((min(width, W), int(min(width, W) * k["img"].size[1] / k["img"].size[0]))).save(f, quality=85)
        regions = []
        if k["bbox"] and k["reason"] != "start":
            sx = W / aw
            regions = [[int(v * sx) for v in b] for b in k["bbox"]]
            if annotate:
                with Image.open(f) as im:
                    im = im.convert("RGB")
                    fx = im.size[0] / W
                    d = ImageDraw.Draw(im)
                    for rb in regions:
                        x0, y0, x1, y1 = [int(v * fx) for v in rb]
                        pad = 4
                        d.rectangle([max(0, x0 - pad), max(0, y0 - pad),
                                     min(im.size[0] - 1, x1 + pad), min(im.size[1] - 1, y1 + pad)],
                                    outline=(255, 40, 40), width=3)
                    im.save(f, quality=88)
        frames.append({"t": round(k["t"], 2), "onset": round(k["onset"], 2),
                       "before": round(max(0.0, k["onset"] - 0.15), 2),
                       "file": f"frames/{name}", "reason": k["reason"],
                       "change": round(k["change"], 4),
                       "regions": [{"x": b[0], "y": b[1], "w": b[2] - b[0], "h": b[3] - b[1]} for b in regions]})
    return frames


def contact_sheets(out: Path, frames: list[dict], cols: int = 3, rows: int = 3, tile_w: int = 480) -> list[str]:
    """Labelled 3x3 overview grids of the keyframes — a cheap first look."""
    from PIL import Image, ImageDraw, ImageFont
    sheets_dir = out / "sheets"
    sheets_dir.mkdir(exist_ok=True)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()
    per = cols * rows
    paths = []
    for si in range(0, len(frames), per):
        group = frames[si:si + per]
        tiles = []
        for fr in group:
            with Image.open(out / fr["file"]) as im:
                im = im.convert("RGB")
                th = int(tile_w * im.size[1] / im.size[0])
                tiles.append((fr, im.resize((tile_w, th))))
        th = max(t.size[1] for _, t in tiles)
        bar = 26
        r = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tile_w + (cols - 1) * 6, r * (th + bar) + (r - 1) * 6), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        for k, (fr, t) in enumerate(tiles):
            x = (k % cols) * (tile_w + 6)
            y = (k // cols) * (th + bar + 6)
            d.rectangle([x, y, x + tile_w, y + bar], fill=(30, 30, 30))
            num = fr["file"].split("_")[1]
            d.text((x + 6, y + 3), f"#{num}  {ts(fr['t'])}  {fr['reason']}", fill=(255, 255, 255), font=font)
            sheet.paste(t, (x, y + bar))
        name = f"sheets/sheet_{si // per + 1:02d}.jpg"
        sheet.save(out / name, quality=82)
        paths.append(name)
    return paths


def grab_frames(video: Path, times: list[float], crop: str | None, dest: Path) -> list[Path]:
    """Full-resolution PNGs at exact times, optionally cropped (x,y,w,h in source pixels)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for t in times:
        tag = tsp(t).replace(":", "m") + "s" + (f"_crop{crop.replace(',', '-')}" if crop else "")
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

def tsp(sec: float) -> str:
    """mm:ss.s — precise enough to scrub to, or to --grab the instant before a change."""
    sec = max(0.0, sec)
    m, s_ = divmod(sec, 60)
    return f"{int(m):02d}:{s_:04.1f}"


def ts(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


LOW_CONF = 0.6


def write_context(out: Path, video: Path, info: dict, frames: list[dict],
                  segments, backend, language, notes: list[str], sheets=(), started=None) -> Path:
    from datetime import timedelta
    rec_start, rec_src = started or (None, None)

    def clock(t):
        return f" ≈{(rec_start + timedelta(seconds=t)).strftime('%H:%M:%S')}" if rec_start else ""

    lines = [
        f"# Video context: {video.name}",
        "",
        f"- Source: `{video}`",
        f"- Duration: {ts(info['duration'])} · Resolution: {info['width']}x{info['height']}",
        f"- Keyframes: {len(frames)} (in `frames/`, only moments where the screen changed; "
        f"red box = region that changed since the previous keyframe)",
    ]
    if sheets:
        lines.append(f"- Overview sheets (9 keyframes each): {', '.join(f'`{x}`' for x in sheets)}")
    if rec_start:
        lines.append(f"- Recording started ≈ {rec_start.strftime('%Y-%m-%d %H:%M:%S %Z (UTC%z)')} — from "
                     f"{rec_src}. The ≈HH:MM:SS after each frame is that start + video time, for matching logs.")
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
        regs = fr.get("regions") or []
        where = (" — changed: " + "; ".join(f"x={r['x']} y={r['y']} w={r['w']} h={r['h']}" for r in regs)
                 + f" ({fr['change'] * 100:.1f}% of screen)") if regs else ""
        began = ""
        if fr.get("reason") in ("settled", "ongoing") and fr.get("onset") is not None \
                and fr["t"] - fr["onset"] >= 0.2:
            began = f" · change began {tsp(fr['onset'])} (grab {tsp(fr['before'])} for the moment before it)"
        elif fr.get("reason") not in ("start", None) and fr.get("before") is not None:
            began = f" · moment before: {tsp(fr['before'])}"
        lines.append(f"### [{tsp(start)}{clock(start)}] {fr['file']} · {fr.get('reason', '')}{where}{began}")
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
    ap.add_argument("--max-frames", type=int,
                    default=int(os.environ["VIDEO_CONTEXT_MAX_FRAMES"]) if os.environ.get("VIDEO_CONTEXT_MAX_FRAMES") else None,
                    help="frame budget (default 40, or 60 for recordings over 10 min)")
    ap.add_argument("--width", type=int, default=1568,
                    help="max keyframe width in px (default 1568: readable small text, and within what "
                         "current vision models accept without downscaling much)")
    ap.add_argument("--sample-fps", type=float, default=0, help="analysis rate (0 = auto: 12/6/3 by length)")
    ap.add_argument("--change", type=float, default=0.0025,
                    help="fraction of the screen that must change to count as an event (lower = more frames)")
    ap.add_argument("--settle", type=float, default=0.0015,
                    help="max change between samples for the screen to count as at rest")
    ap.add_argument("--min-gap", type=float, default=0.45, help="min seconds between kept frames")
    ap.add_argument("--no-annotate", action="store_true", help="don't draw changed-region boxes")
    ap.add_argument("--no-sheets", action="store_true", help="don't build contact sheets")
    ap.add_argument("--anchor", type=float, default=0.0005,
                    help="min change to add an extra frame at a moment of speech (catches typing)")
    ap.add_argument("--max-gap", type=float, default=3.0,
                    help="keep a frame after this many seconds of continuous change (spinners, animations)")
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
            notes.append("video has no audio track (no narration) — work from the frames and any description "
                         "the user gave in chat; if it's their own recording, the mic may have been off")
        else:
            wav = extract_audio(video, tmpd, start, end)
            if audio_is_silent(wav):
                notes.append("audio track is silent (no narration) — work from the frames and any description "
                             "the user gave in chat; if it's their own recording, the mic may have been off")
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
        if args.max_frames is None:
            args.max_frames = 40 if span <= 600 else 60
        anchors = []
        for x in segments or []:
            anchors += [(x["start"] + x["end"]) / 2, x["end"] + 1.5]  # while speaking + result state
        log(f"analysing frames at {fps} fps …")
        aw = analysis_width(info)
        kept = analyze(video, info, fps, aw, start, end, anchors, args)
        if len(kept) >= args.max_frames:
            notes.append(f"frame budget ({args.max_frames}) reached; the least informative moments were "
                         f"dropped — for more detail re-run a window with --start/--end")
        frames = export_frames(video, kept, info, aw, args.width, out / "frames", not args.no_annotate)
        sheets = [] if args.no_sheets else contact_sheets(out, frames)
        log(f"kept {len(frames)} keyframes")

    started = recording_start(video, info)
    if segments is not None:
        (out / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    meta = {"video": str(video), **info, "window": [start, end],
            "recording_start": started[0].isoformat() if started and started[0] else None, "sample_fps": fps, "frames": frames,
            "sheets": sheets,
            "backend": backend, "language": language, "model": args.model, "notes": notes}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx = write_context(out, video, info, frames, segments, backend, language, notes, sheets, started)

    # stdout is for the calling agent: a short, parseable summary
    print(f"CONTEXT={ctx}")
    print(f"FRAMES_DIR={out / 'frames'}")
    print(f"FRAME_COUNT={len(frames)}")
    if sheets:
        print(f"SHEETS={' '.join(str(out / x) for x in sheets)}")
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
