# video-context

A [Claude Code](https://claude.com/claude-code) skill that lets Claude watch a screen recording.

Record your screen while you talk through a bug, a UI problem or a piece of code, then drop the file into a Claude session. The skill turns the video into something Claude can read:

- **Keyframes** of the screen, taken only when it changes or while you are speaking
- **A timestamped transcript** of your narration, transcribed locally with Whisper
- **`context.md`**, which places every sentence under the frame that was visible while you said it, so "this button" and "look here" resolve to what was on screen

Everything runs on your machine. Nothing is uploaded.

## Install

```bash
git clone https://github.com/FarisAlsaif/video-context ~/.claude/skills/video-context
```

Start a new Claude Code session and the skill is available in every project. To update it later, run `git pull` in that folder.

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

- macOS (Apple Silicon uses the faster MLX backend) or Windows with WSL
- Internet access on the first run, for the dependencies and the speech model

## Options

The script can also process a time window (`--start`, `--end`), grab full-resolution frames at exact times for measuring (`--grab`, `--crop`), take a vocabulary hint for technical terms (`--prompt`), use a larger model (`--model large-v3`), or reuse an existing transcript (`--transcript`). [`SKILL.md`](SKILL.md) documents them all, and [`references/setup.md`](references/setup.md) covers recording tips, model sizes and troubleshooting.

## Privacy

Recordings often show tokens, `.env` files or customer data. Output goes to `./.video-context/` in the project you are working in. The script git-ignores that folder automatically, so it can't be committed by accident.

## License

[MIT](LICENSE)
