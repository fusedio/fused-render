# Usage & configuration

Reference for fused-render's runtime features and settings. For installing and
running, see the [README](../README.md); for building and development, see
[CONTRIBUTING](../CONTRIBUTING.md).

## Execution engine

Python runs in a fresh subprocess per call through the built-in runner by
default. Opt into fused's local compute backend — which gives a folder its own
cached virtual environment (see [Dependencies](#dependencies)) — with
`FUSED_RENDER_ENGINE=auto` (use it when `fused` is importable, else the builtin)
or `=fused` (require it); `pip install "fused-render[fused]"` first. Under the
fused engine a file may also expose a `@fused.udf`-decorated function or assign
`result = ...` directly instead of defining `main()`. You can also switch the
engine in [Preferences](#preferences).

## Dependencies

Under the fused engine, **a folder declares its dependencies once, in a
`pyproject.toml` at the project root**, and every `.py` beneath it shares one
environment however deep it sits:

```toml
# my-app/pyproject.toml
[project]
name = "my-app"
version = "0.1.0"
dependencies = ["altair", "cowsay"]

[tool.uv]
package = false
```

A folder with no `pyproject.toml` — or one that declares no dependencies — runs
on the app's own interpreter, which already ships numpy, pandas, pyarrow,
duckdb, pillow, openpyxl, requests and the rest of the always-there set. That is
the common case and it needs no download and no waiting. Heavier libraries the
app deliberately does not carry — polars, matplotlib, scipy, geopandas,
rasterio, pymupdf — are exactly what the declaration above is for.

When a folder does declare dependencies, the first render shows a one-time
install (one progress row for the whole project, however many scripts the page
calls) and every later render is instant. The environment is built by
[uv](https://docs.astral.sh/uv/) and stored under
`~/.fused-render/venvs/`, **never inside your folder** — your folder gains only
`pyproject.toml` and `uv.lock`, both of which belong in git. Committing the lock
is what makes the same folder resolve to the same versions on another machine.

Adding a dependency is just an edit: save `pyproject.toml` and re-render, and
the environment is reconciled for you. Moving or renaming a folder gives it a
fresh environment by design; the old one is reclaimed automatically.

The project root is the app folder or the template folder a script belongs to,
otherwise the outermost folder above it holding a `pyproject.toml`. A
`pyproject.toml` in a *subfolder* of a project is ignored — the inspector shows
when that happens.

> **Breaking change (unreleased).** Per-file PEP 723 `# /// script` headers are
> no longer read — a leftover block is an ordinary comment, ignored like any
> other. Move its `dependencies` into the project root's `pyproject.toml`.
> Nothing warns you: a file whose packages only ever came from a header will
> fail on the import, so this is worth grepping for rather than waiting to hit.
> Folders whose scripts had no header are unaffected.

## Remote storage (mounts)

The cloud icon at the sidebar's bottom-left opens **Mounts**: remote storage —
S3-compatible object stores, Google Drive, and anything else
[rclone](https://rclone.org) speaks — mounted as local folders under
`~/.fused-render/mounts/`. Everything downstream (previews, readers, tile
servers) sees ordinary local paths.

- **No setup on macOS:** the packaged app bundles rclone itself — no
  install, nothing on PATH. Running from source, or on Linux, still needs
  rclone (`brew install rclone` / your distro's package). macOS mounts via
  the built-in NFS client — no macFUSE; Linux uses FUSE. Windows is not
  supported yet.
- **Credentials never touch fused-render** — they live in rclone's own
  config. S3-compatible remotes can be created from the page; for Google
  Drive and other sign-in backends, run `rclone config` in a terminal once.
- **Mount narrow prefixes** (`bucket/prefix`), not whole buckets — every
  folder listed inside a mount is a remote API call, and search inside a
  mount is capped for the same reason.
- **First open is slow, repeats are fast**: the first read of a large remote
  file downloads what it needs; a local cache (24h retention) makes repeat
  opens near-instant. How slow the first open is depends on the file's
  layout — cloud-optimized formats (COGs, small parquet row groups) behave
  far better than monolithic files.
- Mounts stay up until you unmount them — including across app restarts, and
  every mount is automatically remounted when the server starts.

## AI Models

**AI Models** in the sidebar lists what the Hugging Face cache holds on this
machine — every model, dataset and Space anything on your computer has pulled
from the Hub, biggest first, with what each one costs on disk. If you have never
downloaded anything the page says so and offers the **Discover** tab, which is
where a first model comes from.

- **Each card says what the model is for** — "text generation", "image
  generation", "speech recognition" — and how big the model is in parameters
  ("7.2B params"). Both are read from the files already cached: the purpose from
  the model card's `pipeline_tag` when it came down with the weights, otherwise
  from the architecture in `config.json` (hover the label to see which), and the
  parameter count from the safetensors headers. A repo whose download brought
  neither says nothing rather than guessing.
- **Hover a task to see what it means.** "Image + text to text" says it takes a
  picture and a prompt and answers in text; "fill mask" says it fills in blanked
  words. The same hover says where the label came from — the model card, or the
  architecture in `config.json`.
- **Quantized models say so, and their size is marked "≈".** A 4-bit checkpoint
  packs eight weights into each stored word, so the card shows the width
  ("4-bit") and reports the parameter count unpacked from it — approximate by
  nature, hence the ≈, and read from what the checkpoint declares rather than
  from a name like `…-4bit`.
- **"Added" is when this machine got the model, not when it was released.** The
  release date lives on the Hub, and this page only ever reads your disk.
- **The sizes are real disk usage.** Inside the cache each revision's files are
  symlinks to one shared copy of the bytes, so the page counts that copy once:
  the per-card sizes add up to the total in the header.
- **You can clear space from here**, two ways, each behind a confirmation that
  names what goes and what it frees:
  - **Delete a repo** — the ✕ on its card.
  - **Delete one revision** — expand a card with more than one revision. The
    size shown is what deleting *that* revision frees: revisions share their
    weights, so two revisions of a 7GB model differing in a config file are
    7GB shared and a few KB each, and only the few KB come back.

  There is deliberately no bulk "delete everything older than N days": the age
  it would select on is filesystem last-read time, which some volumes never
  record (see below), and one confirm on a list built from that is a multi-GB
  re-download waiting to happen. The ages on the cards are there for you to
  weigh one model at a time.
- **Nothing is re-downloaded for you.** Deleting a model means the next thing
  that wants it pulls it from the Hub again.
- **Last-read time comes from the filesystem.** Volumes mounted `noatime` never
  update it, so on those a model you use daily can still look untouched. Weigh
  "used 4 months ago" with that in mind before deleting anything.
- **The name goes to the Hub; "Explore" opens it here.** Clicking a model's
  name opens its page on huggingface.co in a new tab — the licence, the full
  model card, the discussions and every revision live there, not on your disk.
  **Explore**, in the card's footer, opens the copy you already have.
- **Explore gives you a model card** view — which carries its own **Hugging
  Face ↗** link in the header, so the way back out to the licence, the
  discussions and the other revisions is always one click away. It shows what the
  model is, its parameters and disk, the summary and tags from its own model
  card, the configuration worth reading (layers, heads, context length,
  vocabulary), a weights table and its largest tensors. It opens instantly even
  on a 40GB checkpoint — nothing is loaded, it is all read from the metadata and
  the safetensors headers. If the model has a `tokenizer.json`, the same page
  ends with a **tokenizer** box: type text and see how that model splits it,
  with counts and chars-per-token. (Live encoding needs the `tokenizers`
  library, which the template installs itself under the fused engine; the
  tokenizer's vocabulary and special tokens show either way.) The plain folder
  listing is still one click away in the mode switcher.
- **The cache path under the heading is a link** — click it to open that folder
  in the explorer, rather than copying it out by hand.
- **It looks where `huggingface_hub` looks** — `HF_HUB_CACHE`,
  `HUGGINGFACE_HUB_CACHE`, `$HF_HOME/hub`, `$XDG_CACHE_HOME/huggingface/hub`,
  then `~/.cache/huggingface/hub` — so a shared model disk pinned with `HF_HOME`
  is the one you see. The path in use is printed under the heading.
- **Scanning happens when you open the page**, and then only when the cache
  really changed. It walks every file in the cache, so it is deliberately not
  re-run each time you switch back to the window — and there is no Refresh
  button, because knowing when that walk is worth paying for is not something
  you should have to judge. Deleting something re-reads the cache on its own,
  and so does a download finishing.
- **You can load a model into memory from here.** Each cached model has a
  **Load** button; while it is resident the card turns green and carries a
  **LOADED** badge next to its name — findable at a glance in a grid of a dozen
  — with the memory it is holding underneath, and **Unload** gives that back. Only one model per kind stays loaded
  — loading a second chat model unloads the first, because two 8GB models on a
  16GB machine is a swap storm — and a dot appears on the AI Models entry in the
  sidebar whenever anything is in memory, so gigabytes are never held by
  something you have forgotten about. Hover the dot to see what.
  - Loading a model that is not downloaded yet fetches it first; the progress is
    the same download-manager row.
  - **Load is offered only where something can actually run it.** Chat models
    and image models have it; a dataset, an embedding model or a transcription
    model does not, because nothing here would load them. A multimodal chat
    model — one labelled "image + text to text" — counts as a chat model: it is
    loaded for its text, and its picture-reading half simply goes unused.
  - **Every kind of model runs on every supported desktop platform**, on the
    backend that suits it: chat models prefer MLX on Apple Silicon with PyTorch
    as a fallback, while Windows and Linux use PyTorch directly; images use
    PyTorch everywhere; transcription uses CTranslate2 everywhere. The Discover
    tab names which backend a suggestion will load on, because the shortlists
    differ — an MLX checkpoint is packed for Metal and will not load on a PC, so
    you are never offered one there.
  - **A model may be running on your processor rather than a graphics card**, and
    the page says so: a loaded card carries **on CPU** beside its memory figure
    when it is, and Discover warns before the download. It works — expect a few
    words a second rather than an instant answer. On Windows the standard PyTorch
    build is CPU-only, so that is the usual case there whatever card is fitted.
    The smaller suggestions exist for exactly this: Qwen3 1.7B answers at a
    readable speed with no GPU at all.
  - The first use of a backend builds a several-GB environment, which shows as
    its own row in the download manager before any weights are fetched.
- **Pages can use these models.** `fused.ai(prompt, {model: "org/name"})` runs a
  local chat model instead of Claude, `fused.ai.image({prompt})` renders a
  picture, and `fused.ai.transcribe({path})` turns a recording into text — all
  through the same download manager, so a page that asks for a model you don't
  have yet shows the download rather than hanging. Images land in
  `~/.fused-render/ai/images/`, named by the time they were made, and the seed
  comes back with every one so you can make the same picture again. Transcripts
  land in `~/.fused-render/ai/transcripts/` as a `.json` carrying every
  segment's timestamps and a plain `.txt` beside it — written to disk, so a
  transcription that took twenty minutes survives closing the tab that asked
  for it.
- **Two tabs: Local and Discover.** Local is what this machine has; Discover is
  what the Hub has. Which one is showing is part of the address (`?tab=discover`),
  so you can bookmark or share a link to either — and the **back button** returns
  you to the tab you came from rather than leaving the page.
- **The Discover tab searches the Hub for models you don't have.** Type a name,
  filter by task, sort by downloads, likes, recently updated or newest. Each
  result says whether it is **already on this machine** (with what it costs on
  disk and when you last read it), **partly downloaded** — an interrupted pull —
  or not here at all, with an estimated size so you know what fetching it would
  cost before you decide. A result you already have opens its model card; one
  you don't opens its page on the Hub in a new tab.
  - **Nothing is sent anywhere until you open that tab**, and the caption names
    the host being asked — and links to it, so you can go and look.  If
    `HF_ENDPOINT` points this machine at a mirror, that is the name you see. Typing is batched into one request, and repeating
    a search inside a minute or so reuses the answer rather than asking again.
  - **Download happens here now.** The suggestions below carry a **Download**
    button; the transfer shows up in the download manager at the bottom-right
    like any other long job, and its ✕ really stops it. Search results stay
    read-only. While a download runs, its card shows that progress rather than
    the ✓ — the ✓ means the model is complete and ready to open, not that you
    once asked for it.
  - **Suggested** is a short curated list per capability — chat models, image
    models — with what each costs and why you would pick it, and a ✓ on the ones
    you already have. It only shows when the search box is empty: it is the
    answer to "what should I even get", which is the question you have before
    you know what to type.
  - **Sizes are estimates and marked "≈"**, computed from the parameter counts
    the Hub publishes for the weights. Other files in the repo aren't counted,
    and a repo the Hub has no such metadata for shows no size rather than a
    guess.
  - If you have an `HF_TOKEN` (or have logged in with the Hugging Face CLI), it
    is used — that is what makes gated and private repos searchable, and it
    raises the rate limit.

## Preferences

The gear at the sidebar's bottom-left opens **Preferences**:

- **Appearance** — System (follows your desktop), Light or Dark. Applies
  immediately — to the app and to every open built-in view, with no reload —
  and is remembered per browser profile (so a browser tab and the desktop
  window each keep their own choice). A few built-in views are exceptions: the
  document-like ones (maps, slides, PDF, docs) are always light, the media and
  geospatial ones are always dark, and Excel and Tableau keep their own in-view
  buttons. **Your own `.html` views are never touched** — their CSS stays
  entirely yours; see the authoring skill for how to follow the desktop
  preference if you want to.
- **Call log** — whether the app records the API calls your pages make, how
  much of each run's parameters it keeps, and how long records are kept. See
  [Call log](#call-log) below.
- **Template registry** — the merged extension → templates bindings (built-in
  plus your own overrides), read-only.
- **Execution engine** — switch `fused.runPython` between the built-in
  executor (fresh subprocess per call) and the fused engine (a folder's
  `pyproject.toml` dependencies in cached venvs). Applied to the next run, no
  restart; setting
  `FUSED_RENDER_ENGINE` pins the engine and locks the switch.

Execution engine sits last, since builtin suits almost everyone. The guided tour
is not on this page — it runs itself the first time you open the app.

The **server's own log** is not a preference, so it is not on that page. It
exists for debugging: when something goes wrong (an "Internal Server Error" in
the browser, or a misbehaving file-open) it has the traceback. Each run writes
its own file in your system temp directory — the CLI prints the path on
startup, and the packaged app reveals it from **menu bar → Open app logs**. It's
disposable: it rotates so it can't grow without bound, and living in temp means
the OS reclaims it. Set `FUSED_RENDER_LOG_DIR` to keep logs somewhere
persistent instead. (Not to be confused with the **call log** below, which is
durable, has settings, and lives in `~/.fused-render/logs/`.)

## AI calls (`fused.ai`)

Pages can ask an AI model with `fused.ai(prompt, opts?)`. It resolves with
exactly `{text, model, usage}` — `model` is the full model id that ran, and
`usage` is either `null` or exactly `{input_tokens, output_tokens}`
(Anthropic-style names, not OpenAI's `prompt_tokens`/`completion_tokens`):

```json
{
  "text": "the completion",
  "model": "claude-haiku-4-5-20251001",
  "usage": { "input_tokens": 544, "output_tokens": 73 }
}
```

The server runs the
call through the **`claude` (Claude Code) CLI** on your machine — your existing
Claude Code login is the credential; there is no proxy or API key to configure.
The binary is found on `PATH`; set `FUSED_RENDER_CLAUDE_BIN` to point at a
specific binary per process. If the CLI isn't installed, calls reject with an
`ai_unavailable` error saying what to install or set.
`fused.ai` is local-only: exported/hosted pages can't use it (see
[EXPORT.md](EXPORT.md)).

## Export for hosted serving

The running server exposes a programmatic export (`POST /api/export`) that
packs a page and its `runPython`/`rawUrl` dependencies into a portable bundle
a hosting layer can serve — see [EXPORT.md](EXPORT.md) for the bundle format
and rules. Publishing a bundle is left to the `fused` CLI directly (`fused
share create`) rather than to this app.

## Call log

The app records every API call your pages make — each `fused.runPython`,
`readFile`, `stat`, `writeFile` and `ai` — with its duration, result size, the
`print()` output, and any traceback. It answers the questions a page can't
otherwise: which of your `.py` files is slow, whether a run errored while you
weren't looking, and whether the page is quietly re-running Python far more
often than you thought.

**Where to see it.** Two surfaces today — a dedicated in-app view is coming in a
later release. Open any `.calls.jsonl` file from the store and it renders in
**Log studio**: records carry a conventional log `level`, so its level facets,
query filter and volume histogram work on them, and expanding a row shows the
record as fields rather than a wall of JSON. For anything scripted, or for an
agent checking its own work, use the CLI below.

**From a terminal:**

```
fused-render calls                          # the last hour, digested
fused-render calls --page ~/views/sine.html # one page
fused-render calls --failed --since 24h     # only what broke
fused-render calls --json                   # machine-readable
fused-render calls --follow                 # wait for the next calls, then print
```

The digest is a per-target rollup plus any failures in full — including **page
errors**, the page's own JavaScript failing, which is what you have when a page
made *no* calls at all. `--since-cursor <id>` (the `cursor` printed at the end)
returns only what is new since a previous run, which is handy in a loop or for
an agent checking its own work.

**Superseded calls.** Dragging a slider fires a call per tick, and each one
cancels the last — so most of a drag is work nobody waited for. Those calls are
recorded and counted as **stale**, but deliberately left out of the duration
percentiles: including them would report a dozen slow calls for what you
experienced as one. A large stale count next to a small ok count is the sign a
page is re-running Python far more than it needs to (a slider with no debounce,
or an `onChange` handler that retriggers itself).

**Getting to it from Preferences.** **Browse call logs** opens the store in the
explorer: one directory per app, and clicking any `.calls.jsonl` opens it in
**Log studio**.

Records carry a normal log `level` (INFO for a healthy call, ERROR for a
failure, DEBUG for a superseded one), which is what makes Log studio a real
viewer for them — level facets, filtering, and its volume-by-level histogram.
Expanding a row there shows the record as **fields** rather than one long line of
JSON: nested objects indented under their key, a traceback or output tail kept as
a block so its newlines survive, and any value clickable to filter the log by it.
**Raw JSON** switches that row back to the exact text on disk. A plain-text log
line is untouched — it still shows as itself. **Show context** fetches the lines
immediately before and after that one straight from the file, which is how you
see a line's neighbours when a query or a level filter has hidden them (a
traceback split across lines, or the request that preceded a failure). The level
facets list only the levels the file actually contains, so a log that only writes
INFO doesn't offer six filters that match nothing. Its Tail button switches
auto-reload off while engaged, so watching a live file polls instead of
reloading, and a poll updates only the lines that changed — a row you have
expanded stays expanded and the scroll stays put. Log studio follows
**Preferences → Appearance** like the rest of the app: System, Light or Dark,
applied as you change it, with no button of its own.

Opening a log file in any view is safe. Nothing watches a log file for changes,
so a view of one never reloads itself — which matters because reading a log *is*
a recorded call, and a watcher would reload, re-read and append forever. The
reads themselves are kept — what a viewer costs on a big log is worth seeing.

**Where it lives.** `~/.fused-render/logs/<app>/` as newline-delimited JSON —
one directory per app (named for the page's folder, e.g. `sine-3f9a1c2b8d7e6f50`,
with `index.json` at the root mapping names back to folders), one file per day
per server process inside it, rolled to a new part every 32 MB. (This is not
the app's own log: that one is disposable diagnostic output and lives in the
system temp dir, reached from **menu bar → Open app logs**. `~/.fused-render/logs/`
holds only the durable call records described here.) Records are capped
(a long traceback or a big parameter is truncated, and marked as such),
rate-limited per page, and pruned after 14 days or once the store passes
200 MB — whichever comes first, with the largest app's oldest files trimmed
first so a chatty app cannot evict a quiet one's history. Today's files are
never pruned, since a running server is appending to them. Renaming or moving
an app's folder starts a fresh directory; the old one ages out on its own. The
files are ordinary JSONL, so `tail`, `jq` (one app's history is one
directory), and the built-in `duckdb` view all work on them — a whole-store
query globs `logs/*/*.calls.jsonl`.

**A note on parameters.** A run's parameters are recorded by default: they are
usually the whole reproduction, and they are already visible in the URL. If a
page passes something sensitive as a parameter, switch **Preferences → Call log
→ Parameters** to *names only* (or off). `FUSED_RENDER_CALLS=0` turns capture
off entirely for a run, and `FUSED_RENDER_CALLS_RETENTION_DAYS` overrides the
retention window.

**What it does not see:** requests a page makes with its own `fetch()` to a
third party, the map templates' tile daemons (they serve the browser directly),
and `fused.rawUrl()` used as an `<img>`/`<embed>` source — a plain URL has
nowhere to carry the attribution header. It is a diagnostic for your own pages,
not an audit trail.

## Fixing a failure on your own machine

Two ways in: a failed row in the download manager, and
**Preferences → Fix this app** for when nothing has actually errored.

### Nothing errored, but something is wrong

Open **Preferences → Fix this app** and describe it — "opening a big folder
takes ten seconds and the window freezes", "the dates in the parquet preview are
off by a day". You do not need an error message; most of what goes wrong never
produces one.

Claude opens a session on this installation with your description and the app's
recent log, and you land in the conversation to watch. It is told to *reproduce
what you describe before changing anything*, and to say so and change nothing if
it cannot — so "I could not make that happen" is a possible and useful outcome.

The same tab shows the state of this installation: whether it has been modified,
and every report a fix session has written. (How to *reinstall* is not here —
that answers a question the amber badge raises, so it lives in the badge's own
panel, below.)

### Something failed

When something the app is doing fails — a model download, a long job, anything
that shows up in the download manager at the bottom right — the failed row
carries a **Fix this** button beside its ✕ (*Diagnose this* on an installation
you cannot write to, *Set up Claude Code* if Claude Code isn't installed — see
below).

It opens a Claude Code session **on the fused-render installation itself**, with
what went wrong already written down for it, and drops you into that
conversation in the explorer's chat sidebar. You watch it work and answer its
permission requests; nothing happens to your copy of the app unattended.

A few things worth knowing:

- **It needs Claude Code on this machine.** If it isn't installed, nothing
  offers you a session in the first place: the button reads *Set up Claude Code*
  and shows you how to get it. Being **signed out** or **over your usage limit**
  are different — the app can't know either without running Claude, so those you
  find out when the session starts, and the card that appears tells you which one
  it was and what to do.
- **Python changes need a restart.** Quit and reopen fused-render before
  deciding whether the fix worked.
- **It can only fix some things.** The session is working on installed files:
  Python, templates, and shipped assets. Anything that needs a rebuilt frontend
  or a new release, it will tell you about rather than attempt.
- **If the app is installed somewhere you can't write to** — an admin installed
  it for everyone, say — the session still runs, but it can only **diagnose**.
  Claude reads the code, works out what is wrong, and writes a report saying
  what the fix would be; nothing on your machine changes. Send that report on,
  or apply the fix in a copy you own. **You are told before you start**, not
  after: the failed row's button reads *Diagnose this* rather than *Fix this*,
  and the Preferences one *Start a diagnostic session*.

### The modified badge

If the session changes anything, the version number in the sidebar turns amber
with a **✳** beside it. That is the app being honest: you are running the
release it names *plus* a local change, so the version alone no longer describes
what is on your disk.

Click it and you get:

- **the report** the session wrote — what went wrong, what it found, what it
  changed, and how to check it. It opens as an ordinary markdown file in the
  app;
- **a way to send it to us.** Please do. A fix that only exists on your machine
  helps nobody else; the report is what lets it ship for everyone. The button
  opens a pre-filled GitHub issue, and there is a **Copy report** button beside
  it for pasting the text in (or attach the file);
- **how to reinstall**, worded for how *this* copy was installed —
  `brew reinstall --cask fused-render`, the DMG, the installer, or
  `pip install --force-reinstall`.

**Reinstalling always clears the badge.** The mark lives inside the installation
folder, so replacing that folder removes it — there is nothing to remember to
reset. Upgrading to a newer version clears it too.

If you would rather keep the change and stop being reminded about it, the panel
has a quiet **Dismiss this badge**. It clears the mark only — the report files
stay where they are.
