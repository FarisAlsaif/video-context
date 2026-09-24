# video-context

An agent skill that lets a coding agent watch a screen recording. It was built for [Claude Code](https://claude.com/claude-code) and works with any agent that supports skills, can run shell commands and can view images.

Record your screen while you talk through a bug, a UI problem or a piece of code, then drop the file into a session. The skill turns the video into something the agent can read:

- **Keyframes** of the screen, taken once each change has settled and while you are speaking, with a red box around what changed
- **Contact sheets** that show the keyframes nine at a time, so a long recording can be skimmed before it is read closely
- **A timestamped transcript** of your narration, transcribed locally with Whisper
- **`context.md`**, which places every sentence under the frame that was visible while you said it, so "this button" and "look here" resolve to what was on screen

It also handles bug recordings made by someone else, with or without narration. The agent reconstructs the steps to reproduce, copies error text and request IDs exactly as shown, and gives each frame a clock time so it can be matched to backend logs.

Everything runs on your machine. Nothing is uploaded.

## Install

```bash
git clone https://github.com/FarisAlsaif/video-context ~/.claude/skills/video-context
```

Start a new Claude Code session and the skill is available in every project. To update it later, run `git pull` in that folder.

For other agents (OpenAI Codex, Gemini CLI, GitHub Copilot, OpenCode), use the cross-agent installer, or clone into the skills folder listed in [`references/setup.md`](references/setup.md):

```bash
npx skills add FarisAlsaif/video-context
```

For the Claude apps (claude.ai or desktop), download `video-context.skill` from the [latest release](https://github.com/FarisAlsaif/video-context/releases/latest) and upload it under Settings → Capabilities → Skills.

## Use

Drag a recording into the session, paste its path, or just ask:

```
look at my latest recording
```

```
/video-context ~/Desktop/bug.mov
```

The first run sets itself up without sudo, Homebrew or apt. It installs [`uv`](https://docs.astral.sh/uv/) if it is missing, and `uv` fetches Python 3.12, Whisper and a bundled ffmpeg. This takes 1–3 minutes, plus a speech-model download of about 1 GB. To do it ahead of time:

```bash
bash ~/.claude/skills/video-context/scripts/run.sh --setup
```

On macOS, turn the microphone on before you record: Cmd+Shift+5 → Options → Microphone. It is off by default, and a silent recording gives Claude only the frames.

## Requirements

- macOS (Apple Silicon uses the faster MLX backend), Linux, or Windows with WSL
- `bash`, and `curl` or `wget`
- Any video format ffmpeg can decode: MOV, MP4, WebM, MKV, AVI, GIF and more
- Audio-only files too (voice memos, call recordings: M4A, MP3, WAV, OGG/Opus, FLAC…), loaded as a timestamped transcript
- Internet access on the first run, for the dependencies and the speech model

## Options

The script can also process a time window (`--start`, `--end`), zoom into a moment at a higher sampling rate (`--sample-fps`, `--change`), grab full-resolution frames at exact times for measuring (`--grab`, `--crop`), take a vocabulary hint for technical terms (`--prompt`), use a larger model (`--model large-v3`), or reuse an existing transcript (`--transcript`). [`SKILL.md`](SKILL.md) documents them all, and [`references/setup.md`](references/setup.md) covers recording tips, model sizes and troubleshooting.

## Privacy

Recordings often show tokens, `.env` files or customer data. Output goes to `./.video-context/` in the project you are working in. The script git-ignores that folder automatically, so it can't be committed by accident.

## License

[MIT](LICENSE)
