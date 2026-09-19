---
name: video-context
description: Watch a screen recording and load what it shows and says into the session as context — keyframes of the screen plus a timestamped transcript of the narration, aligned so "this" and "here" resolve to what was on screen. Use this whenever the user shares, drags in, or mentions a video file (.mov, .mp4, .mkv, .webm), says they recorded their screen, asks you to "watch", "look at", or "check" a recording or video, or refers to "the recording I just made" / "my latest recording" — even if they don't say "transcribe" or name this skill. Works on macOS and WSL.
---

# Video context

The user records their screen while talking — pointing at code, UI, errors, terminals — and wants you to understand it as if you had watched over their shoulder. A video can't be read directly, so a bundled script turns it into two things you can read: the **frames** (what was on screen, only when it changed) and the **transcript** (what they said, with timestamps). `context.md` stitches them together so every sentence sits under the frame that was visible while it was spoken.

## 1. Find the video

- **A path in the message.** Pass it to the script as given, in quotes, and don't `ls` it first. Drag-and-drop pastes a path with escaped spaces; WSL may give a Windows path (`C:\Users\…\Videos\Captures\clip.mp4`). macOS also puts an invisible narrow no-break space before "AM"/"PM" in recording names, so a path you or the user typed won't match exactly, and `ls` reports "No such file". The script matches such names itself.
- **"My latest recording", "the video I just made", or no path.** Use `--latest`. It searches the usual save locations (the macOS screenshot folder and Desktop; on WSL, the Windows `Videos\Captures` and `Videos\Screen Recordings` folders, including OneDrive). It logs which file it picked and how old it is. If that file is more than about an hour old, confirm with the user that it's the right one before going deep.

## 2. Run it (setup is automatic)

Always launch through `run.sh` in this skill's `scripts/` folder (normally `~/.claude/skills/video-context/scripts/`). It installs whatever is missing, then processes the video:

```bash
bash ~/.claude/skills/video-context/scripts/run.sh "<path>" [options]
bash ~/.claude/skills/video-context/scripts/run.sh --latest [options]
```

No sudo, Homebrew or apt is needed. On the first run it installs `uv` into `~/.local/bin`, and uv then fetches Python 3.12, the Whisper packages and a bundled ffmpeg into its cache. It adds the faster MLX backend on Apple Silicon. If uv can't be installed, it falls back to a plain Python venv. Later runs skip all of this.

- **When stderr shows `setup:` lines**, tell the user it's a one-time setup (about 1–3 minutes, plus the model download on the first transcription) so the wait isn't a mystery. Use a timeout of 10 minutes or more for that run.
- **When the user asks to set it up, or you want the first real run to be fast**, run `run.sh --setup`. It installs everything and pre-downloads the speech model (it accepts `--model medium` etc.).
- **If the output ends with `SETUP=failed`**, automatic setup couldn't finish. Show the user the manual commands the script printed. Don't try sudo yourself: Claude Code can't answer a password prompt. Ask the user to run it.

Options worth using:
- `--prompt "…"`: vocabulary hint for the speech model. Pass the project's name and the identifiers likely to be spoken (package or module names, component names, product terms, field labels visible in the UI). This noticeably improves technical words. Build it from the repo in a few seconds before running.
- `--lang ar` / `--lang en`: only when auto-detection gets it wrong (for example, heavy Arabic/English code-switching).
- `--max-frames N` (default 30): raise this if the output says the cap was reached.
- `--start mm:ss --end mm:ss`: process only a window. Results go to a separate folder, so the full context isn't overwritten. Use this to re-transcribe a doubtful stretch with a bigger model, or to take a finer look at one part.
- `--model` (default `large-v3-turbo`, which is accurate and reasonably fast). For a doubtful window, re-run it with `--model large-v3`.
- `--transcript file.srt|.vtt|.json`: reuse an existing transcript and skip transcription.
- `--grab 0:36,0:52 [--crop x,y,w,h]`: save full-resolution PNGs at exact times, optionally cropped to the same region, into `grabs/`. Use this to measure or compare, for example checking whether a pane or column really changed size between two moments. Keyframes are downscaled to 1280px, so measure on grabs, not keyframes.

Transcription is well under real time on Apple Silicon (MLX) and roughly real time on a WSL CPU, and the first run also downloads the model (about 1 GB). For videos over a few minutes, tell the user it's processing and use a generous timeout (10 minutes or more).

Stdout ends with `CONTEXT=`, `FRAMES_DIR=`, `FRAME_COUNT=`, `TRANSCRIPT_SEGMENTS=` and any `NOTE=` lines. The output goes to `./.video-context/<video-name>/`, which is git-ignored automatically.

## 3. Take it in

The narration is the user's request; the frames are the evidence. Past sessions went wrong by analysing frames, reaching a conclusion, and only then (or never) listening. The script prints `AUDIO=speech` when there is narration, and when it does:

1. Read `context.md` in full, **transcript included, before forming any view** of what the problem is.
2. Read every keyframe image, in parallel. Users say "this button" or "look at this" and mean something only the frame shows. Read the frames closely for file names, line numbers, error text, values in forms, and highlighted or selected regions.
3. List every distinct complaint or question in the narration, each one separately. A recording often shows two problems, such as "the image doesn't grow" and "it collapses the fields on the right". Folding the second into the first, or reading it as confirmation of your fix for the first, is the typical miss. Each item needs its own evidence in the frames and its own resolution: fixed, answered, or explicitly deferred.
4. Treat ⚠ low-confidence lines as unverified. If a flagged line, or any line that seems odd against what the frames show, carries a request, don't guess:
   - Re-run that window: `--start` a few seconds before, `--end` a few seconds after, with `--model large-v3`.
   - Cross-check against the frames. Words that are visible on screen (labels, field names) are usually what was said.
   - If it's still ambiguous, quote the line and ask.
5. Resolve references. For each sentence, the frame it sits under is what "this" points at. Near a frame boundary, it may mean the next frame.
6. Measure, don't eyeball. When the point is a size, alignment, clipping or something disappearing, use `--grab` at the before and after moments, with the same `--crop` region, and compare the grabs.
7. Connect it to the repo. Find the code behind what the frames show (the component, the stylesheet, the handler) before replying, so the answer rests on the actual code.

## 4. Respond

Open with a short recap, so the user can confirm you understood before you act:

```
From your recording (2:14):
- What you showed: <the flow, in 1–3 lines — screens, files, the error>
- What you're asking: <each distinct question or request, one per line, in your words>
- Unclear: <only if something genuinely couldn't be resolved — otherwise omit>
```

Then do the work: answer the questions, or proceed with the requested change if it's clear. When you finish, account for every item on that list: done, answered, or deliberately left, with the reason. Don't paste the transcript back. Quote a phrase only when the exact words matter.

The recording is now part of the session context. When the user refers back to it later ("like in the video, at the part where…"), re-read `context.md` or the relevant frame instead of re-running the script.

## Problems the NOTE lines point to

- **No `AUDIO=speech` line.** Check the NOTE lines for why. Never assume a recording has no narration without the script's word for it.
- **"no audio track" / "audio track is silent".** The microphone was off while recording. On macOS, Cmd+Shift+5 records without a microphone by default (Options → Microphone). Tell the user plainly and work from the frames. If the frames alone don't make the request clear, ask them to type it or re-record with the mic on.
- **"no transcription backend installed".** Something bypassed `run.sh`. Re-run through `run.sh`.
- **"transcription failed … download".** The first run needs internet access to fetch the model. Details are in `references/setup.md`.
- **`SETUP=failed`.** See step 2 and `references/setup.md`.

## Privacy

Screen recordings often capture things that shouldn't leave the machine, such as tokens in a terminal, `.env` contents, or customer data. Everything is processed locally. Don't copy secrets you see in frames into files, commits, or messages, and warn the user if the recording exposed any. When the user is done with a recording, you can offer to delete its folder (`rm -rf .video-context/<name>`).
