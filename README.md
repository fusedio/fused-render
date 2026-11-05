# fused-render

**[Download for macOS and Linux →](https://render.fused.io)**

A desktop app for running AI agents, local models and your own apps on your
own machine. Open a folder and it is browsable; open an `.html` file and it
runs with a tiny injected runtime that can call a Python `main()`, local AI
models and the agents installed on your computer. Describe an app in a
sentence and Claude Code builds it. Give a bot a browser and a task and watch
it work, or text it from your phone.

Everything is served from `127.0.0.1`, state lives in plain folders under
`~/Fused`, and the AI you use is either a model running on your hardware or an
agent CLI signed in with your own account.

- [Privacy first](#privacy-first)
- [What you can do](#what-you-can-do)
- [Install](#install)
- [Run](#run)
- [Build your own pages](#build-your-own-pages)
- [Claude Code plugin](#claude-code-plugin)

## Privacy first

fused-render is a local-first, single-user tool. The posture, stated plainly:

- **No accounts, no telemetry.** Nothing is required to sign up for. The app
  has no analytics, crash reporting is written to a local log, and the
  diagnostics bundle is a zip on your Desktop that you decide whether to send.
- **Loopback only.** The server binds to `127.0.0.1`. Mutating routes require
  a custom header so a foreign web page cannot fire blind requests at it.
  Phone access over Wi‑Fi is a separate, off-by-default listener that only
  paired devices (QR code) can reach, and it serves app pages only.
- **Your AI, your credentials.** Claude Code, Codex, Copilot and Gemini run as
  the CLIs on your machine under your own login or subscription. No API key is
  baked in and no Fused server sits in the middle. Local models run as
  subprocesses on your hardware; the Apple tier uses the on-device models
  macOS already ships.
- **Connected services talk to the vendor, not to us.** Signing in to Notion,
  GitHub or Slack is OAuth straight from your computer to theirs. Tokens live
  in the macOS Keychain (a `0600` file on Linux), are never handed to app code,
  and are never put in logs or a child process environment.
- **Plain files you can read, move and commit.** Apps are folders under
  `~/Fused`, bots are folders under `~/Fused/bots`, project environments are
  built by `uv` from a `pyproject.toml`, and app state is a gitignored
  `.fused/` directory.
- **What does leave the machine, so you know.** The packaged app checks a
  signed update manifest in the background (set `FUSED_RENDER_NO_AUTO_UPDATE`
  to stop it). The Community tab fetches its catalog and a small block list.
  Model downloads come from Hugging Face or a Fused mirror of the curated
  picks (`FUSED_MODEL_MIRROR=""` opts out). A few file previews fetch a pinned
  binary on first use. [SECURITY.md](https://github.com/fusedio/fused-render/blob/main/SECURITY.md) lists each one.
- **No sandbox, on purpose.** Pages you open and Python you run have the same
  reach as you do, like a code editor or a local Jupyter kernel. Treat it that
  way.

The Fused account is optional and only used by two features: editing Fused
Workbench canvases locally, and publishing an app to a public link.

## What you can do

### Agents, without the terminal

Every Claude Code session on your machine, in one place. Chat with a folder,
an app or a canvas, and keep the chat beside the thing it is about.

- **Four coding agents.** Claude Code is on by default; GitHub Copilot CLI,
  OpenAI Codex CLI and Google Gemini CLI can be installed, signed in and
  switched on from Preferences. Pick one per chat, with model and reasoning
  effort pills.
- **Tasks.** A task is one agent session with a status: upcoming, queued,
  running, needs attention, blocked, done. See them as a list, board, cards,
  calendar or workspace, grouped by project or by pull request.
- **Scheduling.** Send a prompt later, or on a recurrence (every N days,
  weekdays, nth weekday of the month, or a cron line). Runs happen inside the
  app with the same skills and permissions as a typed turn; on macOS a launch
  agent wakes the app when something is due, and missed runs catch up.
- **Approvals you can read.** Every permission card carries a model-written
  risk pill and a one-line summary. Modes: auto, accept edits, plan only,
  ask every time, with a default per app.
- **Mid-turn messages, Breakout and peeks.** Type while the agent works and
  the line reaches it immediately. Fork a research-only side chat that can
  read but not edit, then send its notes back. Open a read-only peek on any
  subagent.
- **Git-aware.** Worktrees open as their own chats and queues, pull requests a
  task mentions show their status in the header, and repos that fall behind
  their remote surface in the status bar.
- **Claude Code settings, in a GUI.** Plugins and marketplaces, MCP servers,
  per-project memory, and the common `settings.json` keys. A Skills page
  searches your own skills across every agent's directory and installs more
  from skills.sh.

### Bots

A bot is a named agent with its own Chrome browser, logins, memory, playbooks
and routines. Watch it work in a live view, take over the tab yourself, and
hand it back.

- **Super Bot** is your assistant on the Mac: the full set of Claude Code
  tools plus the browser. It can hand work off to other bots and create new
  ones, always asking first.
- **Text it.** On macOS, Super Bot answers iMessage from the one handle you
  allow, and you can approve or deny its actions by replying with a number.
  On the same Wi‑Fi, the iOS companion app and the phone page chat with every
  bot and answer approval cards.
- **26 site presets** (Gmail, Google Docs and Sheets, Notion, GitHub, Slack,
  LinkedIn, X, YouTube, Amazon and more) give a new bot an avatar, standing
  instructions and playbooks for that site. Starters are complete apps that
  give a bot tools instead of browsing (Apple Notes, Google Docs and Sheets
  tabs, Mac automation).
- **Routines** run a bot on a schedule with history and a circuit breaker.
- **Bots use your apps.** Any app folder with a `SKILL.md` or `mcp.toml`
  becomes a tool. A bot can offer to build an app for a job, and that build
  becomes a Claude task.
- **Share a bot** as a single `.fusedbot` file, apps included. Opening one on
  another Mac installs it, or updates the bot in place.

### Apps

- **Describe one.** From Home or the Apps page, say what you want; Claude Code
  names it, scaffolds it and opens a chat on it.
- **Every app is a place.** Its page has the running app, its tasks, its files
  rendered in a tree, each `.py` as a form you can call, a version picker over
  its git history, and **App Doctor**, a share-readiness checklist (secrets,
  hard-coded paths, stray files, stale API version) where every row has a
  "fix with Claude" button.
- **Community.** A catalog of apps that live in their authors' GitHub repos.
  Installing clones the reviewed commit; updates move only to the next
  reviewed release. Edit someone's app and you can propose the change to the
  author, remix it as your own, or let Claude merge overlapping edits.
  Publishing your own is three steps: put it on GitHub, list it, publish
  updates.
- **Phone access.** Switch it on in Preferences, pair by QR, and your apps
  show up on your phone over Wi‑Fi. The iOS app adds Home Screen widgets, a
  Bots screen and microphone capture.

### Local AI

An AI Models page shows what the Hugging Face cache holds on this machine,
what fits, what is loaded, and lets you download, benchmark and free space.
Pages and Python get one API:

| Verb | What runs it |
|---|---|
| `fused.ai.text` | a local chat model (MLX or llama.cpp GGUF), Apple's on-device model, or Claude, Copilot, Codex or Gemini |
| `fused.ai.image` | FLUX on Apple silicon, Diffusers on CUDA, ROCm or CPU elsewhere, with edit and inpaint |
| `fused.ai.video` | LTX on Apple silicon |
| `fused.ai.speech` | Qwen3-TTS on Apple silicon, Kokoro anywhere |
| `fused.ai.transcribe` | Whisper, with diarization and word timings, or Apple's SpeechAnalyzer |
| `fused.ai.embed` | text, image, audio and video embeddings |
| `fused.ai.decide` | calibrated yes/no, choice and score answers with no generated tokens |
| `fused.ai.vision` | caption, query, detect and point on an image |

Everything local runs in its own `uv`-built environment chosen per capability
and platform. Intel Macs get the agent tiers only.

### Connections

Preferences → Connections signs you in to Notion, GitHub and Slack once.
Their tools then work in your apps (`fused.connect.call`), in Python, through
the local HTTP API, and in bots, where each service is off per bot until you
tick it.

### Files

The explorer opens any folder and previews Parquet, GeoParquet, PDF,
notebooks, spreadsheets, point clouds, GeoTIFF, NetCDF, Zarr, 3D models and
more than a hundred other types, each one an HTML template you can copy.
A machine-wide file index answers search and SQL over your filesystem without
walking it. A terminal drawer, a process monitor and, on macOS, a menu-bar
dock with a ⌥Space launcher round out the desktop.

## Install

Download from **[render.fused.io](https://render.fused.io)**:

- **macOS** — `.dmg`, Apple silicon. Intel Macs: a DMG is attached to each
  [release](https://github.com/fusedio/fused-render/releases), without the
  local model runners.
- **Linux** — AppImage, x86_64.

macOS is the native build; Linux has limited support. Windows is not
supported. Trouble on first run? See
[troubleshooting](https://render.fused.io/#troubleshooting).

**Homebrew** — the same macOS app, from the tap:

```
brew install --cask fusedio/tap/fused-render
```

**Python package** — from PyPI, for a machine where you would rather run the
server in a Python environment than as a desktop app:

```
uv tool install --force --python 3.12 "fused-render[cli]"
uv tool update-shell
fused-render
```

The base install (`pip install fused-render`) runs one app with
`fused-render open <path-or-github-url>` and nothing more. `[cli]` adds the
full explorer, index, previews, Community and the Hugging Face login, about
230 MB more. `[all]` adds the native desktop integrations (screen capture,
clipboard). The pieces on their own are `[index]`, `[data]`, `[hf]`,
`[desktop]` and `[fused]`. Requires Python 3.11+. From a source checkout,
`pip install -e '.[all]'`; building and the dev loop are in
[CONTRIBUTING.md](https://github.com/fusedio/fused-render/blob/main/CONTRIBUTING.md).

## Run

The downloaded app opens Home in its own window. From a pip install it is a
command:

```
fused-render
fused-render --start-dir ~/data --port 9000 --no-browser
```

Either way the server is at `http://127.0.0.1:1777/`. `--start-dir` is where
the explorer starts (default `~/Fused`); the whole filesystem stays browsable
from there. A first-run wizard checks for Claude Code, git and Chrome and
offers a first model download.

## Build your own pages

Any `.py` file is a runnable target as long as it defines a `main()`:

```python
# sine.py
import math

def main(n: int = 80, freq: float = 1.0):
    return {"points": [[i / n, math.sin(2 * math.pi * freq * i / n)] for i in range(n)]}
```

Any `.html` file beside it can call it and bind the result to the URL:

```html
<input id="freq" type="range" min="0.1" max="5" step="0.1" />
<script>
  const slider = document.getElementById("freq");
  slider.addEventListener("input", () => fused.params.set("freq", slider.value));
  fused.params.onChange(draw);

  async function draw() {
    const freq = fused.params.get("freq") || "1.0";
    const { points } = await fused.runPython("./sine.py", { freq });
    // ...render points...
  }
  draw();
</script>
```

No framework, no build step. A folder's dependencies go in one
`pyproject.toml` and `uv` builds the environment on first render. The
injected `fused` object gives a page:

- `fused.runPython`, `fused.params`, `fused.readFile` / `writeFile` /
  `uploadFile` / `mkdir` / `stat` — Python and files, URL-synced state.
  → [fused-render-authoring](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-authoring/SKILL.md)
- `fused.ai.*` — the eight verbs above, with `provider` and `model` to pin a
  tier, streaming and progress callbacks, and `fused.ai.models` to list,
  download, load and unload.
  → [fused-render-ai](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-ai/SKILL.md)
- `fused.tasks` — list, create, follow and cancel agent tasks from a page.
  → [fused-render-tasks](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-tasks/SKILL.md)
- `fused.bots` — see the bots on this machine and give one a task, with
  images or files, and follow it to its result.
  → [fused-render-bots](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-bots/SKILL.md)
- `fused.connect` — call a connected service's tools; the token never
  reaches the page.
- `fused.capture` — record the screen or microphone, or grab a screenshot.
  → [fused-render-capture](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-capture/SKILL.md)
- `fused.fileIndex` — search and SQL over the machine-wide file index.
  → [fused-render-index](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-index/SKILL.md)
- `fused.daemon`, `fused.trackJob` / `watchJob` — a folder's resident
  background process, and long-running work in the download manager.
  → [background apps](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-background-apps/SKILL.md),
  [jobs](https://github.com/fusedio/fused-render/blob/main/skills/fused-render-jobs/SKILL.md)
- A `SKILL.md` or `mcp.toml` at the app root makes the app's Python callable
  by bots and agents without the page.

`fused.env` says whether a page runs here (`"local"`) or exported and hosted
(`"hosted"`). The complete list, with every option, is the header of
[`fused_render/static/runtime.js`](https://github.com/fusedio/fused-render/blob/main/fused_render/static/runtime.js), and every
built-in preview template in `fused_render/templates/` is an example on the
same primitives. Custom templates, theming and app icons have their own
skills under [`skills/`](https://github.com/fusedio/fused-render/blob/main/skills/).

[docs/usage.md](https://github.com/fusedio/fused-render/blob/main/docs/usage.md) covers the execution engine and dependencies,
the AI Models page, preferences and export for hosted serving.

## Claude Code plugin

This repo doubles as a [Claude Code](https://code.claude.com/docs) plugin
marketplace. Its skills teach Claude to run the app, author views, use local
AI, bots, tasks and jobs, and build preview templates.

```
/plugin marketplace add fusedio/fused-render
/plugin install fused-render@fused-render
```

Or from the command line:

```
claude plugin marketplace add fusedio/fused-render
claude plugin install fused-render@fused-render
```

Installing is optional and only affects *your own* Claude Code sessions. Chats
started inside fused-render already get these skills: the app assembles the
same plugin under `~/.fused-render/skill-plugin/` and loads it per session
with `--plugin-dir`.

## Security

[SECURITY.md](https://github.com/fusedio/fused-render/blob/main/SECURITY.md) has the trust model, the list of things that fetch
from the network, and how to report a vulnerability.

## License

Open source under the [MIT license](https://github.com/fusedio/fused-render/blob/main/LICENSE), made by
[Fused](https://www.fused.io).
