"""class Bot: one browsing bot (docs/bots.md §1, §5) — the engine-neutral
half of OpenBot's `agents.py`.

Everything a bot is apart from the model loop lives here: lifecycle, the
transcript, memory, playbooks, the Inbox (artifacts) and attachments, the
file inbox (botsend / iMessage), routines, offers, builds, `send` / pause /
resume / stop / take-over / window, idle sleep, and `summary()` for the page.
The loop itself is an ENGINE: `steps_engine.run(bot, task, label)` (OpenBot's
JSON-action loop over `fused_ai.text`) or `agent_engine.run(bot, task, label)`
(Claude Code with native tools over MCP). `start_task` picks one.

State on disk (paths.py): `<home>/bots/data/bots/<id>/` (bot.json, events.jsonl,
memory.md, skills/, profile/, downloads/, files/, inbox/), `<home>/bots/cache/bots/<id>/`
(shot.png, steps/<seq>.jpg, badjson/), the Inbox `~/Fused/bots/<bot name>/`
and the apps `~/Fused/app/` (`FUSED_RENDER_DIR` overrides `~/Fused`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from fused_render.bots import apptools, imessage, store
from fused_render.bots import browser as browser_mod
from fused_render.bots import paths as bpaths
from fused_render.bots.browser import Browser
from fused_render.bots.channels import base as chan

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "low"
MAX_STEPS = 60
ATTACH_MAX = 8 * 1024 * 1024  # composer attachment size cap
IDLE_SLEEP_S = 10 * 60  # close Chrome after this long idle (every bot); encrypted bots also seal the profile
ROUTINE_MAX_FAILS = 3   # consecutive failed runs before a routine pauses itself
MODEL_LOADING_SLEEP_S = 8
MODEL_LOADING_MAX_WAITS = 12
IDLE_SHOT_TIMEOUT_S = 3
# Builds: a bot hands a job to Claude Code through the server's tasks API and
# gets a whole fused-render app made, one folder per build under the apps root
# (bpaths.apps_root(), resolved lazily). The per-bot "Builds" setting
# (build_access) picks the Claude permission mode: "scoped" (default) = Claude
# asks before risky tools; "full" = unattended.
BUILD_MODEL = "opus"
BUILD_EFFORT = "high"
BUILD_MODES = {"scoped": "default", "full": "auto"}
BUILD_POLL_S = 15          # how often a build watcher asks the server for the task's status
BUILD_MAX_S = 3 * 3600     # stop watching after this long
# Hand-offs (docs §11): Super Bot gives an ordinary bot a task and gets ONE result back.
HANDOFF_POLL_S = 2         # how often a hand-off watcher looks at the target bot
HANDOFF_MAX_S = BUILD_MAX_S  # a target may wait on the user at the Mac (login, approval) for a long time
HANDOFF_KEEP = 40          # meta["handoffs"] rows kept on Super Bot
INBOX_LIST = 12            # artifacts the page shows per bot

MODELS = ("haiku", "sonnet", "opus", "fable", "local-4b", "local-9b")  # fused.ai aliases the model picker offers
LOCAL_MODELS = {"local-4b": "mlx-community/gemma-4-e4b-it-4bit",
                "local-9b": "mlx-community/gemma-4-12B-it-qat-4bit"}
LOCAL_MODEL_SIZES_GB = {"mlx-community/gemma-4-e4b-it-4bit": 5.2,
                        "mlx-community/gemma-4-12B-it-qat-4bit": 11.0}
EFFORTS = ("low", "medium", "high", "xhigh")  # fused.ai effort levels the picker offers
ENGINES = ("auto", "steps", "agent")
# Bot kinds. "bot" (the default, absent from older bot.json files) browses
# through the tool table. "super" is the one bot per install that also gets
# Claude Code's own tools (Read, Edit, Write, Bash, …) over the agent engine:
# the user's assistant on this Mac. Its permission prompts become approval
# cards (agent_engine._permission); `super_access` picks how many are raised:
# "ask" (default) = the CLI's default mode, a card for every write / edit /
# shell command; "full" = the CLI's `auto` mode (its own classifier approves
# what it judges safe and escalates the rest), the same mode Builds' "Full
# access" uses. Only the user starts a Super Bot task: from its chat, or by text
# from the iMessage handle set on it (then always in "ask" posture, see
# agent_engine.super_mode); routines and the file inbox are refused (docs §5
# "Super Bot"). Super Bot hands browsing work to the other bots (`handoff`,
# docs §11); a hand-off to Super Bot is refused (depth one).
KINDS = ("bot", "super")
SUPER_ACCESS = {"ask": "default", "full": "auto"}
SUPER_NAME = "Super Bot"
# Super Bot's avatar is the Claude mark (frontend lib/face.ts BRANDS["claude"], a
# locked brand): the face picker does not offer it, `_flag` refuses it for any
# other bot and refuses any face change for Super Bot.
RESERVED_ICON = "claude"
SUPER_FACE = {"shape": "", "color": "#262624", "icon": RESERVED_ICON}  # the dark disc; the rays are orange in the glyph
SUPER_INSTRUCTIONS = ("You are my assistant on this Mac. Use Claude Code's tools for files, PDFs, images, shell and code, "
                   "and the browser tools for the web. Keep what you make for me in your Inbox folder unless I name "
                   "another place. Before anything destructive (deleting, overwriting, sending), tell me what you are about to do.")


def _builds_root() -> str:
    """Where builds land and every bot's APPS live (OpenBot BUILDS_ROOT), resolved per call."""
    return bpaths.apps_root()


def _artifacts_root() -> str:
    return bpaths.artifacts_root()


# Mounted into the step prompt only when the task or a recent user message touches an
# app topic (see APP_GUIDE_TRIGGER), like a skill: the bot can explain every setting
# and feature without paying for the text on ordinary browsing steps. `@APPS_ROOT@`
# is substituted when the prompt is built (app_guide()), so FUSED_RENDER_DIR applies.
APP_GUIDE = """APP GUIDE (Browser Bots, a local desktop app; every bot has its own Chrome and its own settings):
- Settings (menu on the preview pane, or the bot's avatar): name and avatar; Model (Haiku fastest, Sonnet balanced, Opus strongest, Fable most capable, plus Gemma 4B and 12B, local models that run on this Mac) and Effort (low/medium/high/xhigh, how long you think per step), both apply from the next task; Instructions (your STANDING INSTRUCTIONS); Approvals: "Ask before irreversible actions" (default; the gate pauses on risky actions and on upload) or "Never ask"; Browser profile: import one of the user's own Chrome profiles (its logins, cookies, extensions) into your browser; Encrypt browser profile at rest (AES-256 file while Chrome is closed, key in the macOS Keychain); Memory: the user can read and edit your MEMORY there (it caps at 200 notes, then `remember` fails until they trim it).
- Routines (same menu): scheduled tasks, "Every N minutes" (min 5), "Daily at HH:MM" on chosen weekdays, or "Once at" a date-time. Each can be enabled, disabled, run now or deleted. A run only starts when you are idle; a busy bot skips that slot. A routine pauses itself after 3 failed runs in a row. You cannot create routines yourself: tell the user how to add one.
- Skills (same menu): the PLAYBOOKS. The user can write one by hand, click "Learn from last task" (the model condenses your last finished task), or you save one with `learn`. Up to 40 per bot; each mounts into your prompt only when one of its trigger words appears in the task.
- Chat: the user can pause, resume or stop you at any time; a message sent while you work arrives as USER INSTRUCTION and overrides the task; they can reply to or react with an emoji on one of your messages (you see reactions in CONVERSATION SO FAR); they can search the thread; "Export" saves the whole transcript as Markdown. Attaching, pasting or dropping a file on the composer puts it in FILES so you can `upload` it.
- Live view: clicking your screenshot opens your browser full size with a tab strip, back/forward/reload and a URL bar. "Take over" pauses you and lets the user drive (solve a captcha, pass a popup); "Hand back" returns control and you continue. "Open in browser" pops your Chrome out as a real desktop window; "Dock" brings it back. Your `login` action pops a real window the same way and waits until the user replies "done" or clicks Hand back.
- Inbox: everything you produce lands in the user's Inbox, a Finder folder at ~/Fused/bots/<your name>/ with one subfolder per task: `save` results, downloads that arrived during the task, and a README with the task and your final answer. The Inbox list under your screenshot shows the most recent items with "Open folder" to reveal them in Finder. Files the user attaches in the composer land in FILES instead, for `upload`. There is no other export path.
- iMessage: only Super Bot takes tasks by text (from the phone number or Apple ID set in its Settings > Advanced) and texts its replies back; it can hand a browsing task to a bot like you, whose final answer goes back to it as the result. Other bots are not reachable by text. "Contacts the bot may text" (Settings > Advanced) is the allowlist your `text` action can message and `texts` can read replies from (shown to you as CONTACTS). You cannot add contacts yourself: tell the user where.
- Builds (button under the bots list): Claude Code sessions that create fused-render apps. The user can start one there, and you can start one with `build` (say so, then `done` with the link it returns). Apps land under @APPS_ROOT@/<name>; the Builds panel tracks progress and holds Claude's chat for each build; a chat message (and a text, if iMessage is on) arrives when one is ready. Settings > Advanced > Builds picks "Scoped" (Claude asks the user before risky steps) or "Full access" (unattended).
- Apps: every fused app under @APPS_ROOT@ is visible to every bot, whoever built it: the APPS section of your prompt lists them all (folder, name, description, link). `show` any of them as a card, `goto` its link to use it in the browser, or `build` with its exact name to update it. You also OFFER apps on your own (`offer`): an existing one that fits the task, or a new one worth building, as a card with "Use it" / "Build it" / "Not now". A yes starts the build with no further step (the yes is the approval), "Not now" keeps that app out of offers for a week, and an unanswered offer stays clickable in the chat after the task ends (a plain yes or no later settles it). When the user asks for an app in so many words you `build` it straight away and the approval card confirms it with one click.
- App tools: local apps that expose MCP tools (an `mcp.toml` curated in fused-render's MCP panel) are available to you through the `tool` action; the APP TOOLS section of your prompt lists them by app. Reading tools run at once; tools that change something ask the user first. Cards in the Apps panel show a tools badge when an app exposes any. Nothing has to be attached: every app with a manifest is available to every bot.
- App skills: an app that ships a SKILL.md (marked [py] in APPS) tells you what each of its .py files does and how to call it; the `py` action runs one (its main(**args), exactly as the app's page would run it). Skills load only when needed: apps you built this task and apps the task names are mounted under APP SKILLS; `py` with an app and no file loads any other. Apps you build get a SKILL.md as part of the build; an older app without one can get it from an update build. Your own builds run at once; other apps' files ask the user first, showing the file's line from its SKILL.md.
- Bots list: New bot, search, pin or hide a bot (pinned first, then bots waiting on the user, then most recent); "Clone" makes a new bot sharing your logins, memory, instructions and skills; "Delete" removes a bot with its browser profile. The Usage button shows model calls per hour, day and bot. Other local scripts can hand you tasks through botsend.py; they show up as normal tasks.
- Limits: a task ends after 60 steps; your browser closes after 10 minutes idle (and reopens on the next task, logins kept); model calls time out after 3 minutes."""
APP_GUIDE_TRIGGER = re.compile(
    r"\b(setting|settings|routine|schedule|scheduled|daily|every (day|morning|hour|\d+ ?min)|cron|remind|skill|playbook|memory|remember|forget|model|haiku|sonnet|opus|fable|effort|faster|slower|smarter|approval|approve|permission|encrypt|keychain|profile|login|cookie|export|transcript|clone|copy of you|delete you|rename|avatar|take ?over|live view|pop out|window|dock|download|upload|attach|file|save|usage|calls|steps|limit|timeout|sleep|pause|resume|stop|botsend|build|builds|app|apps|tool|tools|mcp|dashboard|tracker|how do (i|you)|can you|what can you|help|who are you|your (name|settings|config))\b", re.I)


def app_guide() -> str:
    """APP_GUIDE with the apps root filled in (resolved now, not at import)."""
    return APP_GUIDE.replace("@APPS_ROOT@", _builds_root())


_YES = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|sure|approve|approved|go(?!\s+(to|back|on|and)\b)|go ahead|do it|proceed|confirm|allow)\b", re.I)  # "go to X instead" is not a yes
_NO = re.compile(r"^\s*(n|no|nope|deny|denied|stop|don'?t|cancel|skip)\b", re.I)
# `offer` replies: the option labels, a plain yes/no, or the natural forms of each. The
# strict pair is for a bare message typed after the task ended (see _answer_pending_offer):
# it must not swallow "ok now go to linkedin…" as a yes.
_OFFER_YES = re.compile(r"^\s*(build|use|make|do|create|go for|try) (it|that|one|this)\b", re.I)
_OFFER_NO = re.compile(r"^\s*(not now|no thanks|no thank you|maybe later|later|skip|nah|pass|not (today|yet|really))\b", re.I)
_BARE_YES = re.compile(r"^\W*(yes|yep|yeah|yup|sure|ok|okay|please|please do|do it|go ahead|go for it|build it|use it|make it|"
                       r"yes please|sounds good|let'?s do it|absolutely|definitely)\W*(please|thanks|thank you)?\W*$", re.I)
_BARE_NO = re.compile(r"^\W*(no|nope|nah|not now|no thanks|no thank you|later|maybe later|skip|pass|don'?t|do not)\W*(thanks|thank you)?\W*$", re.I)
DECLINED_OFFER_S = 7 * 86400  # a declined app offer is not made again for this long
OFFER_WAIT_S = 10 * 60        # an unanswered `offer` stops blocking the task after this; the card stays answerable
# Tasks that read like something the user will want again: the step-1 hint suggests an `offer`.
_APP_WORTHY = re.compile(
    r"\b(track|tracking|monitor|keep an eye|compare|comparison|every (day|week|morning|hour)|daily|weekly|regularly|each (day|week)|"
    r"dashboard|top \d+|collect|compile|inventory|budget|expenses?|prices?|rates?|scores?|standings|progress|status of|leaderboard|"
    r"portfolio|watchlist|wishlist|checklist|habit|calculate|calculator|convert)\b", re.I)
# The user asked for an app in so many words: the model then goes straight to `build`.
_ASKS_FOR_APP = re.compile(
    # a creation or update verb, then a noun that is only ever an app ("build me a price tracker", "update the expense tracker app")
    r"\b(?:build|make(?! sure)|create|code|write|generate|set ?up|spin up|update|change|fix|improve|extend|edit|modify|tweak|rebuild|redo)\b"
    r"(?: me| us)?(?: a| an| some| my| our| the| this| that)?(?: new| small| simple| quick| little| tiny| basic| local)*(?: \w+){0,3}?"
    r" (?:app|application|tool|dashboard|tracker|calculator|widget|web ?page|website|site|viewer|visuali[sz]er|planner|checker|"
    r"converter|simulator|generator|kanban|scheduler|leaderboard|counter|timer|todo|to-do)s?\b"
    # or "make / build / create a <thing>" for things that are apps only when made from scratch ("make a form", "build a calendar")
    r"|\b(?:build|make|create|code|write|generate)\b(?: me| us)? (?:a|an)(?: new| small| simple| quick| little| tiny| basic| local)*(?: \w+){0,3}?"
    r" (?:page|form|table|chart|calendar|editor|game|board|gallery|explorer|monitor|inventory|catalog|catalogue|directory|wiki|notebook|"
    r"journal|diary)s?\b", re.I)
# Words too common to say anything about which app a task is about.
_STOP = set("the a an and or of for to in on at by with from my me our your this that these those is are be it its what which who "
            "how when where all any some each every please can could would should want need like find get show tell give make "
            "check look see use using into about over under just also than then there here new one two open list app apps".split())


def _norm_url(u):
    """Strip fragment and trailing slash so /pricing and /pricing/#top count as one page."""
    if not u:
        return u
    u = u.split("#", 1)[0]
    return u[:-1] if u.endswith("/") and u.count("/") > 3 else u


def _resolve_model(alias):
    return LOCAL_MODELS.get(alias, alias)


def _cancel_job(ai, job_id):
    if not job_id:
        return
    try:
        ai._post_json(f"/api/jobs/{job_id}/cancel", {}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


_FUSED_AI = None
_FUSED_AI_LOCK = threading.Lock()


def _fused_ai():
    """The `fused_ai` client (fused_render/templates/shared/fused_ai.py): it talks to
    THIS server over HTTP (FUSED_RENDER_ORIGIN, exported before the server serves).

    Loaded by path once, as fused_render/server/routers/claude_agent.py loads
    `app_entry.py`: `templates/shared/` is a stdlib-only directory of template
    helpers, not a package, and stays out of the package's import graph."""
    global _FUSED_AI
    if _FUSED_AI is None:
        with _FUSED_AI_LOCK:
            if _FUSED_AI is None:
                import importlib.util

                path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "templates", "shared", "fused_ai.py")
                spec = importlib.util.spec_from_file_location("fused_render_shared_fused_ai", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _FUSED_AI = mod
    return _FUSED_AI


def _server_origin():
    return bpaths.server_origin()


def _server_origin_quiet():
    return bpaths.server_origin_quiet()


def _slug(name):
    return bpaths.slug(name)


def _tasks_api(method, path, body=None, timeout=20):
    """One call against the server's /api/tasks routes with the X-Fused guard header.
    Raises RuntimeError carrying the server's own sentence on a non-2xx answer."""
    import urllib.error
    import urllib.request
    url = _server_origin() + path
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Fused": "1"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read() or b"{}")
            said = said.get("error") or said.get("detail") or f"HTTP {e.code}"
        except Exception:  # noqa: BLE001
            said = f"HTTP {e.code}"
        raise RuntimeError(str(said))
    except urllib.error.URLError as e:
        raise RuntimeError(f"fused-render is not reachable: {e.reason}")


def _build_prompt(name, d, ask, update=False):
    """Mirror of buildPrompt in the Builds panel. The first line is the marker the
    Builds panel adopts a task by, so it must keep this form."""
    if update:
        return f"""Build "{name}" · update to the fused-render app in {d}

You are running inside {d}, which already holds the app "{name}". Read what is there first
(README.md, SKILL.md, index.html, any .py) and change it to match the request below; keep the parts
the request does not touch. Do not start over and do not create a second copy.
Rules:
- Invoke the fused-render-authoring skill before writing any code and follow its contract.
- Keep the single entry page {d}/index.html with its <meta name="fused-app" /> and <meta name="fused-api-version" /> tags.
- ALL UI state MUST live in the URL through fused.params (selected tab, filters, search text, sort, open item, map view, toggles, any value the user picks): read it on load, write it on every change. Move any existing view state that lives only in JS variables or localStorage into fused.params. The shell's Copy state button copies the page URL, so a copied link must reopen the app exactly as the user sees it.
- Plain HTML/CSS/JS, no build step, no network at runtime. Python beside the page via fused.runPython only when it adds value.
- Every .py beside the page exposes ONE top-level annotated main(**params), returns JSON-native values, takes no argv/stdin and finishes under 60 s, and gets a section in the app's SKILL.md (beside index.html): what it does, what it changes, args, return shape, one example call. Bots read that SKILL.md to call these files directly (their `py` action). Keep SKILL.md in step with every .py you add, change or remove. The authoring skill's "App SKILL.md" section has the exact format.
- Update README.md if the behaviour changed. Do not touch anything outside {d}.
- When done, reply with a two-line summary of what changed and the folder path.

What to change:
{(ask or "").strip()}"""
    return f"""Build "{name}" · new fused-render app in {d}

You are running inside {d}, an empty folder made for this app. Build the app there.
Rules:
- Invoke the fused-render-authoring skill before writing any code and follow its contract.
- Exactly one entry page, {d}/index.html, with <meta name="fused-app" /> and <meta name="fused-api-version" content="1" /> near the top of <head>.
- Plain HTML/CSS/JS, no build step, no network at runtime. Python beside the page via fused.runPython only when it adds value, with a pyproject.toml in that folder.
- Every .py beside the page exposes ONE top-level annotated main(**params), returns JSON-native values, takes no argv/stdin and finishes under 60 s, and gets a section in the app's SKILL.md (beside index.html): what it does, what it changes, args, return shape, one example call. Bots read that SKILL.md to call these files directly (their `py` action). Keep SKILL.md in step with every .py you add, change or remove. The authoring skill's "App SKILL.md" section has the exact format.
- Follow the shell theme (data-fused-theme="shell") and gate the _preview=1 mode.
- ALL UI state MUST live in the URL through fused.params (selected tab, filters, search text, sort, open item, map view, toggles, any value the user picks): read it on load, write it on every change, never keep view state only in JS variables or localStorage. The shell's Copy state button copies the page URL, so a copied link must reopen the app exactly as the user sees it.
- Add a short README.md describing the app. Do not touch anything outside {d}.
- When done, reply with a two-line summary and the folder path.

What the app should do:
{(ask or "").strip()}"""


def _app_link(d):
    from urllib.parse import quote
    try:
        return f"{_server_origin()}/render?path={quote(d)}"
    except Exception:  # noqa: BLE001
        return d


def _app_title(d):
    return os.path.basename(d.rstrip("/")).replace("-", " ").replace("_", " ").strip().capitalize() or "App"


def _app_at(url_or_path):
    """A built app referenced by a /render?path=… link or a plain path under the
    apps root → {"name", "dir", "params"}; None otherwise. `params` is the link's
    query beyond `path` (minus the shell's `_` keys): the app's own state, as
    copied by the page's "Copy state" button — the card reopens the app with it."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    s = (url_or_path or "").strip()
    params = ""
    if not s:
        return None
    if "render?path=" in s:
        pairs = parse_qsl(urlsplit(s).query, keep_blank_values=True)
        s = next((v for k, v in pairs if k == "path"), "")
        params = urlencode([(k, v) for k, v in pairs if k != "path" and not k.startswith("_")])
    s = s.rstrip("/")
    if s.endswith("/index.html"):
        s = s[: -len("/index.html")]
    builds_root = _builds_root()
    root = builds_root.rstrip("/") + "/"
    if not s.startswith(root):
        return None
    d = os.path.join(builds_root, s[len(root):].split("/")[0])
    return {"name": _app_title(d), "dir": d, "params": params} if os.path.isfile(os.path.join(d, "index.html")) else None


def _app_in_text(text):
    """First built app linked from a message, or None."""
    for m in re.finditer(r"https?://\S*?/render\?path=[^\s)\]>\"']+", text or ""):
        a = _app_at(m.group(0))
        if a:
            return a
    return None


def _relevant_apps(task, items, limit=2):
    """APPS whose folder, name or description share words with the task, best first:
    [(score, app)]. A name that appears whole in the task counts most."""
    tl = (task or "").lower()
    words = {w for w in re.findall(r"[a-z0-9]{3,}", tl) if w not in _STOP}
    if not words:
        return []
    out = []
    for a in items:
        text = f"{a['folder'].replace('-', ' ')} {a['name']} {a.get('desc') or ''}".lower()
        aw = {w for w in re.findall(r"[a-z0-9]{3,}", text) if w not in _STOP}
        whole = a["name"].lower() in tl or a["folder"].replace("-", " ").lower() in tl
        score = len(words & aw) + (3 if whole else 0)
        if score >= 2:
            out.append((score, a))
    out.sort(key=lambda t: -t[0])
    return out[:limit]


def _read_meta(bid):
    return store.read_meta(bid)


def _list_ids():
    return store.list_ids()


def _iter_events(path):
    return store.iter_events(path)


def _engine_for(meta) -> str:
    """`steps` | `agent` for this bot's next task (docs §5): the `engine` setting,
    `auto` = steps for a local model or when no `claude` CLI is resolved."""
    if is_super(meta):
        return "agent"  # Super Bot IS Claude Code; without a CLI its task fails and says so
    engine = meta.get("engine") or "auto"
    if engine in ("steps", "agent"):
        return engine
    if (meta.get("model") or DEFAULT_MODEL) in LOCAL_MODELS:
        return "steps"
    try:
        from fused_render.bots import claude_cli
        if claude_cli.runnable() is None:
            return "steps"
    except Exception:  # noqa: BLE001
        return "steps"
    return "agent"


def is_super(meta) -> bool:
    """The one bot with Claude Code's own tools (KINDS)."""
    return (meta or {}).get("kind") == "super"


def super_id():
    """Super Bot's id when one exists, else None (one per install)."""
    for o in _list_ids():
        try:
            if is_super(_read_meta(o)):
                return o
        except Exception:  # noqa: BLE001
            continue
    return None


class Bot:
    def __init__(self, bid):
        self.id = bid
        self.dir = bpaths.bot_dir(bid)
        self.cache_dir = bpaths.bot_cache_dir(bid)
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.browser = Browser(self.dir, self.cache_dir)
        self.lock = threading.RLock()
        self.meta = _read_meta(bid)
        self.browser.encrypt = bool(self.meta.get("encrypt"))
        self.seq = self._count_events()
        self.thread = None
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.inbox = []           # user messages arriving mid-task
        self.task_dir = None      # this task's folder in the user's Inbox, made on first artifact
        self.last_task_dir = None  # (task_via, folder) of the task that ended last (collect_task_artifacts)
        self.task_started = 0.0   # downloads newer than this belong to the running task
        self.task_origin = "manual"
        self.task_via = dict(chan.WEB)  # where the running task came from (channels/base.py Via); web by default
        self.engine = None        # "steps" | "agent" while a task runs
        self.wake = threading.Event()
        self.asking = False       # the task thread is blocked on a question/approval for the user
        self.window_closed = False
        self.recovering = False
        self.shooting = False
        self.model_ready = set()
        self._offers = 0          # `offer` actions made in this task (one allowed)
        self._offer_seq = None    # seq of the offer the task thread is waiting on right now
        self._tool_apps = set()
        self._skills_loaded = []
        self._handoff_queue = []  # hand-offs waiting for this bot to go idle: [(super bot id, hand-off id)], FIFO
        self._handoff_threads = {}  # hand-off id -> the target's task thread it started (Super Bot side, in memory)
        dirty = False
        if self.meta.pop("channel_forwards", None) is not None:
            dirty = True  # per-bot forwards are gone (docs §10): only a task's origin hears back
        # A server restart leaves "running" on disk with no thread behind it.
        if self.meta.get("status") in ("running", "waiting", "paused"):
            self.meta["status"] = "idle"
            self.meta["note"] = "interrupted by worker restart"
            dirty = True
        # Hand-offs still open when the server last stopped: their target's task died with it.
        interrupted = []
        for hd in self.meta.get("handoffs") or []:
            if not hd.get("done_at"):
                text = f"Interrupted by a restart; {hd.get('target_name') or 'the bot'}'s chat has what it got to."
                hd.update(state="error", done_at=time.time(), result=text)
                interrupted.append((hd, text))
                dirty = True
        if dirty:
            self.save()
        for hd, text in interrupted:
            # The user was told they would hear: the result card says how it ended (texted back by the origin rule).
            try:
                self.emit("done", text, summary=text, source="handoff",
                          handoff=self._handoff_ref(hd, task=hd.get("task") or "", task_dir=""), via=hd.get("origin_via"))
            except Exception:  # noqa: BLE001
                logger.debug("interrupted hand-off card not written", exc_info=True)
        # Builds still in flight when the server last stopped: pick their watchers back up.
        for bd in list(self.meta.get("builds") or []):
            if not bd.get("done_at") and time.time() - float(bd.get("created_at") or 0) < BUILD_MAX_S:
                self._watch_build(bd)

    # -- persistence -------------------------------------------------------
    def save(self):
        store.write_meta(self.id, self.meta)

    def _count_events(self):
        try:
            with open(self.events_path, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0

    def emit(self, role, text, **extra):
        """Append one event to events.jsonl and return it (docs §2 shape).

        The counter is re-synced to the file first: another writer on the same
        log (a second server process on this app home, a botsend.py) may have
        appended since, and a repeated seq leaves the page's thread with a
        duplicate row key (an orphaned bubble on bot switch).

        Events written by the task thread carry the task's `via` (the channel
        the task came from) unless the caller set one; other threads (build
        watcher, offer settlement, greeting) pass theirs explicitly or carry
        none. The channels router gets every event (registry.on_event) and
        delivers the outbound-worthy ones under its policy."""
        with self.lock:
            self.seq = max(self.seq, self._count_events()) + 1
            ev = {"seq": self.seq, "ts": time.time(), "role": role, "text": text, **extra}
            if "via" not in ev:
                t = self.thread
                if t is not None and threading.get_ident() == t.ident and not chan.is_web(self.task_via):
                    ev["via"] = dict(self.task_via)
            elif ev["via"] is None:
                del ev["via"]
            os.makedirs(self.dir, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        _registry().on_event(self, ev)
        return ev

    # -- step thumbnails -------------------------------------------------------
    # One small JPEG per action, under cache/<id>/steps/, referenced from the
    # action event so the transcript can show what the page looked like after
    # each step. Cache only: a missing file just leaves the chip without an image.
    STEP_THUMBS = 200

    def _keep_bad_reply(self, raw, step):
        """Write a reply `_parse` rejected to cache/<id>/badjson/<step>.txt and
        return a one-line summary for the error event."""
        raw = raw or ""
        d = os.path.join(self.cache_dir, "badjson")
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{step}.txt"), "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError:
            pass
        head = re.sub(r"\s+", " ", raw)[:160]
        return f"{len(raw)} chars" + (f": {head}" if head else " (empty reply)")

    @property
    def steps_dir(self):
        return os.path.join(self.cache_dir, "steps")

    def _step_thumb(self):
        """Save the browser's latest thumbnail as steps/<next seq>.jpg and return
        the FILE NAME (`"<seq>.jpg"`) for the event's `thumb`; None when there is
        none. Call it as an argument of the `emit` it belongs to (seq + 1)."""
        data = getattr(self.browser, "thumb_bytes", None)
        if not data:
            return None
        d = self.steps_dir
        try:
            os.makedirs(d, exist_ok=True)
            name = f"{max(self.seq, self._count_events()) + 1}.jpg"  # the seq emit() is about to hand out
            p = os.path.join(d, name)
            with open(p + ".tmp", "wb") as f:
                f.write(data)
            os.replace(p + ".tmp", p)
            names = sorted((n for n in os.listdir(d) if n.endswith(".jpg")), key=lambda n: int(n[:-4]) if n[:-4].isdigit() else 0)
            for n in names[:-self.STEP_THUMBS]:
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
            return name
        except OSError:
            return None

    def past_conversation(self, limit=24, width=400):
        """Earlier user messages and the bot's questions/answers, for the prompt.
        Built from the on-disk transcript, so it survives restarts and lets a new
        task like "do it again" refer to what happened before."""
        keep = {"user": "USER", "question": "YOU ASKED", "approval": "YOU ASKED APPROVAL", "done": "YOU FINISHED", "system": None}
        out = []
        for _, ev in _iter_events(self.events_path):
            role, text = ev.get("role"), (ev.get("text") or "").strip()
            if role not in keep or not text:
                continue
            if role == "system":
                if not text.startswith("Task started: "):
                    continue
                label, text = "TASK STARTED", text[len("Task started: "):]
            elif role == "question" and ev.get("offer"):
                o = ev["offer"]
                label = f"YOU OFFERED TO {'USE' if o.get('kind') == 'use' else 'BUILD'} THE APP {o.get('name') or ''!r}"
            elif ev.get("source") == "handoff":
                # Super Bot's hand-off cards: what another bot reported is data, never its own words or orders.
                who = str((ev.get("handoff") or {}).get("target_name") or "A BOT").upper()
                label = f"{who} REPORTED (data from a bot you handed off to)" if role == "done" else f"HAND-OFF NOTE ({who})"
            else:
                label = keep[role]
            text = " ".join(text.split())
            if len(text) > width:
                text = text[:width] + "…"
            rx = (self.meta.get("reactions") or {}).get(str(ev.get("seq")))
            if rx:
                text += f"  [user reacted {rx}]"
            out.append(f"{label}: {text}")
        return out[-limit:]

    # -- memory --------------------------------------------------------------
    # memory.md: durable notes the bot (or you) keep between tasks: site quirks,
    # preferences, where things live. Read into every step's prompt, capped.
    MEMORY_CAP = 24 * 1024
    MEMORY_LINES = 200

    @property
    def memory_path(self):
        return os.path.join(self.dir, "memory.md")

    def memory(self):
        try:
            with open(self.memory_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def set_memory(self, text):
        text = (text or "").strip()
        if text:
            with open(self.memory_path + ".tmp", "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(self.memory_path + ".tmp", self.memory_path)
        else:
            try:
                os.remove(self.memory_path)
            except FileNotFoundError:
                pass

    def remember(self, note):
        note = " ".join((note or "").split())
        if not note:
            return "nothing to save"
        cur = self.memory()
        if note.lower() in cur.lower():
            return "already in memory"
        lines = [l for l in cur.splitlines() if l.strip()]
        if len(lines) >= self.MEMORY_LINES or len(cur) + len(note) > self.MEMORY_CAP:
            return "memory is full: ask the user to trim it in Settings"
        stamp = time.strftime("%Y-%m-%d")
        self.set_memory(cur.rstrip("\n") + f"\n- [{stamp}] {note}")
        return "saved to memory"

    def memory_for_prompt(self):
        m = self.memory().strip()
        if not m:
            return ""
        if len(m) > self.MEMORY_CAP:
            m = m[-self.MEMORY_CAP:]
        return m

    # -- skills: reusable playbooks learned from finished tasks -----------------
    # skills/<slug>.md: "# title", a "trigger: a, b" line, then numbered steps.
    # A skill is mounted into the prompt only when one of its trigger phrases
    # appears in the task text, so unrelated tasks pay nothing for it.
    SKILL_MAX = 40
    SKILL_BODY_CAP = 6000

    @property
    def skills_dir(self):
        return os.path.join(self.dir, "skills")

    @staticmethod
    def _parse_skill(text):
        title, trigger, body = "", "", []
        for line in text.splitlines():
            if not title and line.startswith("# "):
                title = line[2:].strip()
            elif not trigger and re.match(r"(?i)^trigger\s*:", line):
                trigger = line.split(":", 1)[1].strip()
            else:
                body.append(line)
        return title, trigger, "\n".join(body).strip()

    def skills(self):
        out = []
        try:
            names = sorted(n for n in os.listdir(self.skills_dir) if n.endswith(".md"))
        except OSError:
            return out
        for n in names:
            try:
                with open(os.path.join(self.skills_dir, n), encoding="utf-8") as f:
                    title, trigger, body = self._parse_skill(f.read())
            except OSError:
                continue
            out.append({"name": n[:-3], "title": title or n[:-3], "trigger": trigger, "body": body})
        return out

    def skill_save(self, title, trigger, body, name=None):
        title = " ".join((title or "").split())[:80] or "Untitled playbook"
        trigger = ", ".join(t.strip() for t in re.split(r"[,\n]", trigger or "") if t.strip())[:300]
        body = (body or "").strip()[:self.SKILL_BODY_CAP]
        if not trigger:
            raise ValueError("a playbook needs at least one trigger word or phrase")
        if not body:
            raise ValueError("a playbook needs steps")
        name = re.sub(r"[^a-z0-9]+", "-", (name or title).lower()).strip("-")[:60] or "playbook"
        os.makedirs(self.skills_dir, exist_ok=True)
        if not os.path.exists(os.path.join(self.skills_dir, name + ".md")) and len(self.skills()) >= self.SKILL_MAX:
            raise ValueError(f"this bot already has {self.SKILL_MAX} playbooks; delete one first")
        p = os.path.join(self.skills_dir, name + ".md")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(f"# {title}\ntrigger: {trigger}\n\n{body}\n")
        os.replace(p + ".tmp", p)
        return name

    def skill_delete(self, name):
        try:
            os.remove(os.path.join(self.skills_dir, os.path.basename(name or "") + ".md"))
        except FileNotFoundError:
            pass

    def skills_for(self, task):
        """Skills whose trigger phrases occur in the task text (case-insensitive,
        whole words). A trigger is a comma-separated list; any one phrase matches."""
        t = " " + " ".join((task or "").lower().split()) + " "
        hits = []
        for sk in self.skills():
            for phrase in (x.strip().lower() for x in sk["trigger"].split(",")):
                if phrase and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", t):
                    hits.append(sk)
                    break
        return hits[:4]

    def skills_for_prompt(self, task):
        hits = self.skills_for(task)
        if not hits:
            return ""
        parts = [f"### {sk['title']} (trigger: {sk['trigger']})\n{sk['body']}" for sk in hits]
        return ("PLAYBOOKS (step-by-step recipes you saved from earlier runs that match this task; follow them "
                "unless the page has changed, and prefer them over exploring):\n" + "\n\n".join(parts) + "\n\n")

    def last_task_transcript(self, width=200):
        """The most recent finished task as text: the task, each action with its
        result, questions/answers and the final message. Fuel for `learn`."""
        evs = [ev for _, ev in _iter_events(self.events_path)]
        start = None
        for i in range(len(evs) - 1, -1, -1):
            if evs[i].get("role") == "system" and (evs[i].get("text") or "").startswith("Task started: "):
                start = i
                break
        if start is None:
            return "", []
        task = evs[start]["text"][len("Task started: "):]
        lines = []
        for ev in evs[start + 1:]:
            role, text = ev.get("role"), " ".join((ev.get("text") or "").split())
            if role == "action":
                res = " ".join((ev.get("result") or "").split())[:width]
                lines.append(f"ACTION {text[:width]} -> {res}")
            elif role in ("question", "approval"):
                lines.append(f"ASKED {text[:width]}")
            elif role == "user":
                lines.append(f"USER {text[:width]}")
            elif role == "done":
                lines.append(f"DONE {text[:width]}")
        return task, lines[-80:]

    def greet(self):
        """Say hi in a background thread right after creation. With standing
        instructions the bot introduces what it is set up to do; without them a
        short hello. A failed model call falls back to a canned line."""
        instr = (self.meta.get("instructions") or "").strip()
        name = self.meta.get("name") or "Bot"

        def go():
            ai = _fused_ai()
            alias = self.meta.get("model") or DEFAULT_MODEL
            real = LOCAL_MODELS.get(alias)
            if real and alias not in self.model_ready:
                info = self._local_model_info(ai, real)
                if info is None or not info.get("downloaded"):
                    # A local model not on disk yet: no greeting. The "download
                    # first?" question belongs to the first task — asked here it
                    # would block this thread, and `send` would hand the user's
                    # first task to it as the answer (and swallow it).
                    self.set_status("idle", note="")
                    return
                self.model_ready.add(alias)
            self.set_status("idle", note="")
            text = None
            try:
                if instr:
                    prompt = (f"You are a browser-automation assistant named {name}. You drive your own Chrome window to do "
                              "tasks the user types in chat. The user just created you with these standing instructions:\n\n"
                              f"{instr}\n\nWrite your first message in the chat: greet the user by saying hi, say what you are set up "
                              "to help with and list 2-4 concrete things they can ask you for, based only on the instructions above. "
                              "Then invite them to give you a first task. Plain text, friendly, first person, under 90 words, "
                              "no headings, no markdown, no quotes around the message.")
                else:
                    prompt = (f"You are a browser-automation assistant named {name}. You drive your own Chrome window to do tasks "
                              "the user types in chat (browse sites, fill forms, gather information, check feeds). The user just "
                              "created you with no special instructions. Write your first chat message: say hi, say in one sentence "
                              "what you can do, and ask what they would like you to do first. Plain text, friendly, first person, "
                              "under 50 words, no markdown, no quotes around the message.")
                text = (self._ai_call(ai, prompt, model=self.meta.get("model") or DEFAULT_MODEL, effort="low", timeout=60) or "").strip()
            except Exception:  # noqa: BLE001
                text = None
            if not text:
                text = (f"Hi, I'm {name}. My standing instructions: {instr[:300]} Tell me what to do first."
                        if instr else f"Hi, I'm {name}. Give me a browser task and I'll get started.")
            self.emit("done", text)
            # A task typed while the greeting was being written went to the inbox
            # (send() saw this thread alive). It is the user's first task: run it
            # now instead of dropping it with the greeting.
            queued = self._drain_inbox()
            if queued:
                self.start_task("\n".join(queued), label=queued[0])
            else:
                self.set_status("idle", note="")
        self.thread = threading.Thread(target=go, daemon=True, name=f"greet-{self.id}")
        self.thread.start()

    def learn_from_last(self):
        """Condense the last finished task into a playbook, in a background
        thread (the model call takes a while). Progress lands in the thread."""
        task, lines = self.last_task_transcript()
        if not task or not any(l.startswith("ACTION") for l in lines):
            raise ValueError("no finished task with actions to learn from yet")
        if self.thread and self.thread.is_alive():
            raise ValueError("wait until the bot is idle")
        self.emit("system", f"Learning a playbook from: {task[:80]}…")

        def go():
            try:
                from fused_render.bots.steps_engine import _parse
                ai = _fused_ai()
                model = self.meta.get("model") or DEFAULT_MODEL
                real = LOCAL_MODELS.get(model)
                if real and model not in self.model_ready:
                    info = self._local_model_info(ai, real)
                    if info is None or not info.get("downloaded"):
                        raise RuntimeError("local model not downloaded yet; ask the bot a question first to trigger the download")
                prompt = ("Below is the transcript of a browser task an agent completed. Write a reusable playbook so the "
                          "agent can repeat this kind of task faster next time.\n\nReply with strict JSON only:\n"
                          '{"title": "<short name, e.g. LinkedIn feed summary>", '
                          '"trigger": "<2-5 comma-separated words or phrases that would appear in a task asking for this, e.g. linkedin feed, linkedin posts>", '
                          '"steps": "<5-15 numbered steps: exact URLs, what to click/type, what to skip (popups, dead ends seen here), how to finish. Generalise names and dates; never include passwords or codes.>"}\n\n'
                          f"TASK: {task}\n\nTRANSCRIPT:\n" + "\n".join(lines))
                raw = self._ai_call(ai, prompt, model=self.meta.get("model") or DEFAULT_MODEL, effort="low", timeout=120)
                d = _parse(raw) or {}
                name = self.skill_save(d.get("title"), d.get("trigger"), d.get("steps") or d.get("body"))
                sk = next((x for x in self.skills() if x["name"] == name), None)
                self.emit("system", f"Saved playbook \"{sk['title'] if sk else name}\" (trigger: {sk['trigger'] if sk else '?'}). Edit it under Skills.")
            except Exception as e:  # noqa: BLE001
                self.emit("error", f"Could not learn a playbook: {e}")
        threading.Thread(target=go, daemon=True, name=f"learn-{self.id}").start()

    # -- files: results the bot saves, and things you drop in for it to use ----
    @property
    def files_dir(self):
        return os.path.join(self.dir, "files")

    def save_file(self, name, text):
        """`save`: a text result into this task's Inbox folder (see artifacts_dir).
        Returns the artifact's absolute path."""
        name = re.sub(r"[^\w.\- ]+", "_", os.path.basename(name or "")).strip() or "result.md"
        if "." not in name:
            name += ".md"
        folder = self.task_folder()
        p = os.path.join(folder, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(text or "")
        os.replace(p + ".tmp", p)
        self._record_artifact(p, "save")
        return p

    # -- inbox (artifacts): ~/Fused/bots/<name>/<task>/ --------------------------
    @property
    def artifacts_dir(self):
        """The bot's Inbox folder. Pinned in bot.json the first time it is used,
        so a rename afterwards does not split the bot's output across folders."""
        d = self.meta.get("artifacts_dir")
        if d:
            return d
        root = _artifacts_root()
        base = _slug(self.meta.get("name")) or self.id
        d = os.path.join(root, base)
        if os.path.isdir(d):
            others = []
            for o in _list_ids():
                if o == self.id:
                    continue
                try:
                    others.append(_read_meta(o).get("artifacts_dir"))
                except Exception:  # noqa: BLE001
                    continue
            if d in others:
                d = os.path.join(root, f"{base}-{self.id[:4]}")  # another bot with the same name owns it
        return d

    def _pin_artifacts_dir(self):
        d = self.artifacts_dir
        if self.meta.get("artifacts_dir") != d:
            with self.lock:
                self.meta["artifacts_dir"] = d
                self.save()
        return d

    def task_folder(self):
        """This task's folder in the Inbox, created on the first artifact."""
        if not self.task_dir:
            stamp = time.strftime("%Y%m%d-%H%M", time.localtime(self.task_started or time.time()))
            slug = _slug(self.meta.get("task") or "task")[:40].rstrip("-") or "task"
            root = self._pin_artifacts_dir()
            cand, n = os.path.join(root, f"{stamp}-{slug}"), 1
            while os.path.isdir(cand) and self.task_dir is None and n < 50:
                n += 1
                cand = os.path.join(root, f"{stamp}-{slug}-{n}")
            os.makedirs(cand, exist_ok=True)
            self.task_dir = cand
        return self.task_dir

    def _record_artifact(self, path, kind, **extra):
        """One manifest line per artifact; the page's Inbox reads the tail."""
        try:
            size = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            size = 0
        row = {"ts": time.time(), "name": os.path.basename(path), "path": path, "kind": kind, "size": size,
               "task": (self.meta.get("task") or "")[:160], "folder": os.path.basename(os.path.dirname(path)), **extra}
        root = self._pin_artifacts_dir()
        os.makedirs(root, exist_ok=True)
        with self.lock:
            with open(os.path.join(root, "index.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        return row

    def artifacts(self, limit=INBOX_LIST):
        """Most recent artifacts first, dropping ones removed from disk (builds
        keep their row while the app folder exists)."""
        p = os.path.join(self.artifacts_dir, "index.jsonl")
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()[-(limit * 3):]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if os.path.exists(row.get("path") or ""):
                out.append(row)
            if len(out) >= limit:
                break
        return out

    def task_artifacts(self):
        """Artifacts written by the running task, for the prompt."""
        if not self.task_dir or not os.path.isdir(self.task_dir):
            return []
        return [r for r in self.artifacts(40) if os.path.dirname(r.get("path", "")) == self.task_dir]

    def collect_task_artifacts(self, result_message=""):
        """Task end: move downloads that arrived during the task into its Inbox
        folder and write a README with the task and the final answer. Returns the
        task's artifact rows (newest first). Silent on any failure; the
        transcript already has the result."""
        try:
            try:
                names = os.listdir(self.browser.downloads)
            except OSError:
                names = []
            for n in names:
                p = os.path.join(self.browser.downloads, n)
                if n.startswith(".") or n.endswith(".crdownload") or not os.path.isfile(p):
                    continue
                if os.path.getmtime(p) < self.task_started - 5:
                    continue
                dest = os.path.join(self.task_folder(), n)
                stem, ext = os.path.splitext(n)
                k = 1
                while os.path.exists(dest):
                    k += 1
                    dest = os.path.join(self.task_folder(), f"{stem}-{k}{ext}")
                shutil.move(p, dest)
                self._record_artifact(dest, "download")
            if self.task_dir and os.path.isdir(self.task_dir):
                names = sorted(n for n in os.listdir(self.task_dir) if n != "README.md" and not n.startswith("."))
                with open(os.path.join(self.task_dir, "README.md"), "w", encoding="utf-8") as f:
                    f.write(f"# {self.meta.get('task') or 'Task'}\n\n"
                            f"Bot: {self.meta.get('name')} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.task_started or time.time()))}\n\n"
                            + ("## Result\n\n" + result_message.strip() + "\n\n" if result_message.strip() else "")
                            + ("## Files\n\n" + "\n".join(f"- {n}" for n in names) + "\n" if names else ""))
            return self.task_artifacts()
        except Exception:  # noqa: BLE001
            return []
        finally:
            # last_task_dir: a hand-off watcher (docs §11) reports the folder after the task has ended,
            # keyed by the task's via so a follow-up task never hands it another task's folder.
            self.last_task_dir, self.task_dir = (dict(getattr(self, "task_via", None) or {}), self.task_dir), None

    def _fresh_downloads(self):
        try:
            return any(not n.startswith(".") and not n.endswith(".crdownload")
                       and os.path.getmtime(os.path.join(self.browser.downloads, n)) >= self.task_started - 5
                       for n in os.listdir(self.browser.downloads))
        except OSError:
            return False

    def reveal(self, path=None):
        """Open the Inbox (or one artifact's folder) in Finder."""
        d = path if path and os.path.exists(path) else self.artifacts_dir
        if os.path.isfile(d):
            subprocess.Popen(["open", "-R", d], close_fds=False)
            return d
        os.makedirs(d, exist_ok=True)
        subprocess.Popen(["open", d], close_fds=False)
        return d

    def save_bytes(self, name, data):
        """A file the user attached in the composer; lands in files/ so the bot
        can `upload` it by name. Never overwrites: a clash gets a numeric suffix."""
        name = re.sub(r"[^\w.\- ]+", "_", os.path.basename(name or "")).strip() or "attachment"
        os.makedirs(self.files_dir, exist_ok=True)
        stem, ext = os.path.splitext(name)
        cand, n = name, 1
        while os.path.exists(os.path.join(self.files_dir, cand)):
            n += 1
            cand = f"{stem}-{n}{ext}"
        p = os.path.join(self.files_dir, cand)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
        return cand

    def resolve_file(self, name):
        """A file for `upload`: a bare name is looked up in files/ then downloads/;
        anything else is treated as a path (~ expanded)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("no file given")
        if os.sep not in name and not name.startswith("~"):
            for d in (self.files_dir, self.browser.downloads, self.task_dir or ""):
                p = os.path.join(d, name) if d else ""
                if p and os.path.isfile(p):
                    return p
            for r in self.artifacts(40):  # something this bot produced in an earlier task
                if r.get("name") == name and os.path.isfile(r.get("path") or ""):
                    return r["path"]
        p = os.path.expanduser(name)
        if os.path.isfile(p):
            return p
        raise ValueError(f"file not found: {name} (looked in this bot's files/, downloads/, its Inbox, and as a path)")

    def all_files(self):
        """Attached (files/) and downloaded files, `[{name, size, path, kind, …}]`."""
        return ([{**f, "kind": "saved"} for f in self.browser.list_files(self.files_dir, 12)]
                + [{**f, "kind": "download"} for f in self.browser.list_files(self.browser.downloads, 12)])

    # -- file inbox: any local process drops a .txt task here (see botsend.py) --
    @property
    def inbox_dir(self):
        return os.path.join(self.dir, "inbox")

    def drain_file_inbox(self):
        try:
            names = sorted(n for n in os.listdir(self.inbox_dir) if n.endswith(".txt"))
        except OSError:
            return
        for n in names:
            p = os.path.join(self.inbox_dir, n)
            try:
                with open(p, encoding="utf-8") as f:
                    task = f.read().strip()
                os.remove(p)
            except OSError:
                continue
            if task:
                # botsend.py drops. A file is a local script's door whatever its name: never a phone
                # channel (its stem is no address to text back), and Super Bot refuses it.
                if not is_super(self.meta):  # Super Bot refuses it in receive(): its thread shows that line only
                    self.emit("system", f"Task received from {chan.label('botsend')} ({n[:-4]})")
                self.receive(task, via=chan.via("botsend", n[:-4]))

    def events_since(self, cursor):
        """Events after the page's cursor, by position in the file.

        The cursor is a line count (the `seq` the page last saw), not a seq
        filter: anything appended by another writer with a lower seq — a
        botsend.py process — still reaches the page."""
        out = [ev for i, ev in _iter_events(self.events_path) if i >= cursor]
        if out:
            # Keep our counter ahead of anything on disk so new seqs stay unique.
            with self.lock:
                self.seq = max(self.seq, cursor + len(out))
        return out

    def set_status(self, status, **kw):
        with self.lock:
            self.meta["status"] = status
            self.meta.update(kw)
            self.meta["updated"] = time.time()
            self.save()

    def summary(self, light=False, detail=False):
        """docs §2. light: skip the per-bot liveness probe (full-screen polls 2-3x/s);
        detail: the selected bot also reports its tabs, files and Inbox."""
        bs = self.browser.status_cached() if light else self.browser.status()
        if detail and bs.get("running"):
            bs["tabs"] = self.browser.tabs()
        if detail:
            bs["files"] = self.all_files()
            bs["artifacts"] = self.artifacts()
            bs["artifacts_dir"] = self.artifacts_dir
        shot_ts = self.browser.shot_ts()
        with self.lock:  # hand-off watchers change rows in place (under this lock); copy them as they stand
            meta = dict(self.meta)
            if meta.get("handoffs"):
                meta["handoffs"] = [dict(h) for h in meta["handoffs"]]
        return {**meta, "id": self.id, "seq": self.seq, "browser": bs,
                "memory": self.memory() if detail else None,
                "skills": self.skills() if detail else None,
                "shot": f"/api/bots/{self.id}/shot" if shot_ts else None,
                "shot_ts": shot_ts, "viewport": list(getattr(browser_mod, "VIEWPORT", (1280, 800)))}

    # -- routines ------------------------------------------------------------
    # meta["routines"]: [{id, task, kind: interval|daily|once, minutes, time "HH:MM",
    #   weekdays [0-6, Mon=0], at (ts), enabled, next, last, last_result}]
    def routines(self):
        return self.meta.setdefault("routines", [])

    @staticmethod
    def _next_run(r, after):
        kind = r.get("kind")
        if kind == "interval":
            m = max(5, int(r.get("minutes") or 60))
            base = r.get("anchor") or after
            n = base
            while n <= after:
                n += m * 60
            return n
        if kind == "daily":
            hh, mm = [int(x) for x in (r.get("time") or "09:00").split(":")[:2]]
            days = r.get("weekdays") or list(range(7))
            for d in range(0, 8):
                day = time.localtime(after + d * 86400)
                cand = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hh, mm, 0, 0, 0, -1))
                if cand > after and time.localtime(cand).tm_wday in days:
                    return cand
            return None
        if kind == "once":
            at = float(r.get("at") or 0)
            return at if at > after and not r.get("last") else None
        return None

    def routine_add(self, task, kind, minutes=None, time_s="", weekdays=None, at=None):
        if is_super(self.meta):
            raise ValueError("Super Bot takes tasks only from your chat; routines are for the other bots")
        task = (task or "").strip()
        if not task:
            raise ValueError("routine needs a task")
        r = {"id": uuid.uuid4().hex[:6], "task": task, "kind": kind, "enabled": True, "created": time.time(), "last": None, "last_result": ""}
        if kind == "interval":
            r["minutes"] = max(5, int(minutes or 60))
            r["anchor"] = time.time()
        elif kind == "daily":
            r["time"] = time_s or "09:00"
            r["weekdays"] = [int(d) for d in (weekdays or list(range(7)))]
        elif kind == "once":
            r["at"] = float(at or 0)
            if r["at"] <= time.time():
                raise ValueError("that time is in the past")
        else:
            raise ValueError("kind must be interval|daily|once")
        r["next"] = self._next_run(r, time.time())
        with self.lock:
            self.routines().append(r)
            self.save()
        return r

    def routine_update(self, rid, enabled=None, delete=False):
        with self.lock:
            rs = self.routines()
            r = next((x for x in rs if x["id"] == rid), None)
            if not r:
                raise ValueError("no such routine")
            if delete:
                rs.remove(r)
            elif enabled is not None:
                r["enabled"] = bool(enabled)
                if r["enabled"]:
                    r["next"] = self._next_run(r, time.time())
            self.save()

    def routine_fire(self, r, manual=False):
        """Start the routine's task now if the bot is free; else skip this slot."""
        with self.lock:
            if not manual and not self._spacing_ok(r):
                return
            busy = self.thread is not None and self.thread.is_alive()
            r["last"] = time.time()
            if busy:
                r["last_result"] = "skipped: bot was busy"
                self.emit("system", f"Routine \"{r['task'][:60]}\" skipped: bot busy")
            else:
                r["last_result"] = "started"
                self.emit("system", f"Routine {'run now' if manual else 'fired'}: {r['task']}")
                self.meta["control"] = False
            r["next"] = self._next_run(r, time.time()) if r.get("enabled") else None
            if r["kind"] == "once" and not busy:
                r["enabled"] = False
            self.save()
        if not busy:
            self.start_task(r["task"], origin="routine")

    def _spacing_ok(self, r):
        """Refuse to fire inside the routine's own interval, judged from disk.

        bot.json is the one source of truth several writers may share. If the
        copy on disk shows this routine fired more recently than our in-memory
        state knows, adopt the disk state and skip — the stale-copy double fire
        that once ran a 5-minute routine every 20 s."""
        try:
            disk = next((x for x in _read_meta(self.id).get("routines", []) if x.get("id") == r.get("id")), None)
        except Exception:  # noqa: BLE001
            return True
        if not disk:
            return True
        gap = 60 * float(r.get("minutes") or 0) if r.get("kind") == "interval" else 60.0
        gap = max(30.0, min(gap, 3600.0)) * 0.9
        if disk.get("last") and time.time() - disk["last"] < gap and disk["last"] > (r.get("last") or 0):
            r.update(disk)
            self.save()
            return False
        return True

    def tick_routines(self):
        self.drain_file_inbox()
        now = time.time()
        for r in list(self.routines()):
            if not r.get("enabled"):
                continue
            if r.get("next") is None:
                r["next"] = self._next_run(r, now)
                self.save()
                continue
            if r["next"] <= now:
                self.routine_fire(r)

    def _routine_outcome(self, task, result, message):
        """Record how a routine-started task ended, so the page can show it.
        Matches the routine that is still marked "started" for this task.
        `fails` counts consecutive errors and resets on success."""
        with self.lock:
            hit = None
            for r in self.routines():
                if r.get("task") == task and r.get("last_result") == "started":
                    hit = r
                    break
            if hit is None:
                return
            hit["last_result"] = result
            hit["last_message"] = (message or "")[:300]
            hit["last_done"] = time.time()
            hit["fails"] = 0 if result == "done" else int(hit.get("fails") or 0) + (1 if result == "error" else 0)
            tripped = hit["fails"] >= ROUTINE_MAX_FAILS and hit.get("enabled")
            if tripped:
                # Circuit breaker: a routine that keeps failing (quota exhausted,
                # site down, login lost) must not keep spending model calls.
                hit["enabled"] = False
                hit["next"] = None
            self.save()
        if tripped:
            self.emit("system", f"Routine \"{task[:60]}\" paused after {hit['fails']} failed runs in a row. "
                                "Fix the cause, then re-enable it under Routines.")

    # -- control -----------------------------------------------------------
    def event_by_seq(self, seq):
        """One transcript event by seq, or None."""
        return next((ev for _, ev in _iter_events(self.events_path) if ev.get("seq") == seq), None)

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def send(self, text, reply_to=None):
        """A message typed on the web page (the composer, a card button)."""
        return self.receive(text, via=None, reply_to=reply_to)

    def receive(self, text, via=None, reply_to=None):
        """A message from the user, from any channel (docs §10): the one entry
        point. `via` is where it came from (channels/base.py; None = the web).
        `reply_to` is the seq of an earlier message the user is replying to: the
        transcript keeps the plain reply plus a quoted snippet for the UI, and the
        bot reads the reply with that message quoted above it so it knows exactly
        what is being referred to.

        A running task gets it as an instruction or an answer; an idle bot starts
        a task from it, tagged with the channel so replies go back there."""
        via = dict(via) if via else dict(chan.WEB)
        if not chan.is_web(via) and via.get("kind") != "imessage" and is_super(self.meta):
            # Super Bot has shell and file access: only the user's own chat, or a text from the handle set
            # on it (the router lets no other sender through), may drive it (docs §5).
            self.emit("system", f"Ignored a task from {chan.label(via['kind'])}: Super Bot only takes tasks you type here.")
            return
        quoted = self.event_by_seq(int(reply_to)) if reply_to else None
        shown = text  # what the user typed; the transcript and status show this, the model reads the quoted form
        stamp = {} if chan.is_web(via) else {"via": via}
        if quoted:
            snippet = " ".join((quoted.get("text") or "").split())
            self.emit("user", text, reply={"seq": quoted.get("seq"), "role": quoted.get("role"), "text": snippet[:280]}, **stamp)
            who = "my own earlier message" if quoted.get("role") == "user" else "your earlier message"
            text = f"Replying to {who}:\n> {snippet[:1200]}\n\n{text}"
        else:
            self.emit("user", text, **stamp)
        with self.lock:
            running = self.thread is not None and self.thread.is_alive()
            if not running and self.meta.get("control"):
                self.meta["control"] = False  # a fresh task means the bot drives again
            pending = self.meta.get("pending_offer")
        texted = via.get("kind") == "imessage"
        # A yes or no to an app offer that outlived its task (see _offer) is settled here, without a model call.
        # Never from a text: a yes starts a build, and approvals are answered at the Mac (docs §10).
        if pending and not running and not texted and self._answer_pending_offer(pending, shown):
            return
        if pending and running and pending.get("seq") != getattr(self, "_offer_seq", None):
            self._settle_offer()  # the offer timed out earlier and the user has moved on: it is stale, not pending
        if running and texted and is_super(self.meta) and chan.is_web(getattr(self, "task_via", None)):
            # A chat task may run unattended and its replies are never texted: a text must not steer it.
            self.emit("system", "Texted while busy on a chat task; not applied.", via=None)
            self.emit("error", "Busy with a task from the Mac; text again when it's done.", via=dict(via))
            return
        if running and texted and self._texted_approval(shown):
            return
        with self.lock:
            if not running and not self.start_task(text, label=shown, via=via) and self.running():
                running = True  # a hand-off started a task first: this message becomes an instruction to it
            if running:
                # Marked as texted: approval and offer waits never take it as their verdict (channels.base.Texted).
                self.inbox.append(chan.Texted(text, via) if texted else text)
                self.wake.set()
                if self.meta.get("status") == "waiting":
                    # waiting_on stays: the engine clears it when its wait ends,
                    # or re-arms the card when this message was an instruction,
                    # not an answer. Clearing here would settle the card for a poll.
                    self.set_status("running")

    def _texted_approval(self, text):
        """A yes/no texted while the task waits on an approval card: approvals
        are answered at the Mac only (docs §10), so it is not taken as the
        answer. True when the text was held back (a system line says why);
        anything else is an ordinary mid-task instruction."""
        with self.lock:
            waiting = self.meta.get("status") == "waiting" and self.meta.get("waiting_on")
        if not waiting:
            return False
        ev = self.event_by_seq(waiting)
        if not ev or ev.get("role") != "approval":
            return False
        try:
            from fused_render.bots.agent_engine import NO, YES
        except Exception:  # noqa: BLE001
            return False
        if not (YES.match(text or "") or NO.match(text or "")):
            return False
        self.emit("system", "Approvals are answered here at the Mac, not by text: use Approve or Deny on the card.", via=None)
        return True

    def _ai_call(self, ai, prompt, **kw):
        """Every fused_ai.text call goes through here so the usage ledger sees it."""
        ok = False
        try:
            raw = ai.text(prompt, **{**kw, "model": _resolve_model(kw.get("model") or DEFAULT_MODEL)})
            ok = True
            return raw
        finally:
            try:
                store.usage_log(self.id, kw.get("model") or DEFAULT_MODEL,
                                getattr(self, "task_origin", "manual"), self.meta.get("task"), ok,
                                name=self.meta.get("name"))
            except Exception:  # noqa: BLE001
                pass

    def start_task(self, task, label=None, origin="manual", via=None):
        """`task` is what the model reads; `label` (default: the same) is what the
        transcript and status show — a reply's quoted prefix is only for the model.
        `via` is the channel the task came from (channels/base.py; None = web);
        `origin` tags the usage ledger ("manual" | "routine" | a channel kind) and
        follows `via` when one is given. Picks the engine (docs §5): the bot's
        `engine` setting, `auto` = steps for a local model or when no `claude`
        CLI resolves, else the agent engine. Returns True when the task started;
        False when it was refused or the bot is already running one."""
        if via is None:
            via = dict(chan.ROUTINE) if origin == "routine" else dict(chan.WEB)
        elif not chan.is_web(via):
            origin = via.get("kind") or origin
        if is_super(self.meta) and via.get("kind") != "imessage" and not (chan.is_web(via) and origin == "manual"):
            # Super Bot has shell and file access: only the user's chat or their text may start it (docs §5).
            # Judged on the via, so an origin label alone never lifts the gate (or the posture, super_mode).
            self.emit("system", f"Ignored a task from {origin}: Super Bot only takes tasks you type here.")
            return False
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                # Two engine threads on one browser is never right: whoever checked "idle" first won.
                logger.info("bot %s: start_task refused, a task is already running", self.id)
                return False
            self.task_origin = origin
            self.task_via = dict(via)
            self.task_started = time.time()  # the engines stamp it again; the router reads it from here on (forwards)
            self.stop_flag.clear()
            self.pause_flag.clear()
            self.inbox = []
            self.set_status("running", task=label or task, step=0, note="", waiting_on=None,
                            task_via=None if chan.is_web(via) else dict(via))
            engine = _engine_for(self.meta)
            run = None
            if engine == "agent":
                try:
                    from fused_render.bots import agent_engine
                    run = agent_engine.run
                except Exception:  # noqa: BLE001 — the agent engine may not be installed; the steps engine always is
                    logger.warning("bot %s: agent engine unavailable, using the steps engine", self.id, exc_info=True)
                    engine = "steps"
            if run is None:
                from fused_render.bots import steps_engine
                run = steps_engine.run
            self.engine = engine
            self.thread = threading.Thread(target=run, args=(self, task, label or task), daemon=True, name=f"bot-{self.id}")
            self.thread.start()
        return True

    def pause(self, note=True):
        """`note=False` (a yield from the live view) pauses silently: the page shows its
        own driving pill, so the thread's Paused/Resumed toasts would only be noise."""
        if self.thread and self.thread.is_alive() and not self.pause_flag.is_set():
            self.pause_flag.set()
            self.set_status("paused")
            if note:
                self.emit("system", "Paused")

    def resume(self, note=True):
        """Let the bot drive again. Resuming ends any take-over (the bot and the
        user cannot both have the page), and a bot that was paused mid-question
        goes back to "waiting", not "running": it is still blocked on your answer."""
        if self.thread and self.thread.is_alive():
            self.pause_flag.clear()
            self.wake.set()
            # waiting_on survives a pause mid-question; it only means something while waiting.
            self.set_status("waiting" if self.asking else "running", control=False,
                            **({} if self.asking else {"waiting_on": None}))
            if note:
                self.emit("system", "Resumed")

    def stop(self):
        """Stop the running task. stop_flag goes FIRST (it releases every blocked
        wait); the agent engine then interrupts its `claude` process (docs §6)."""
        if self.thread and self.thread.is_alive():
            self.stop_flag.set()
            self.pause_flag.clear()
            self.wake.set()
            self.emit("system", "Stop requested")
        # Unconditional: a no-op without an agent session, and the only thing that
        # interrupts a running `claude` process.
        try:
            from fused_render.bots import agent_engine
        except ImportError:
            return
        try:
            agent_engine.stop(self)
        except Exception:  # noqa: BLE001 — the flag alone still ends the task at its next wait
            logger.warning("bot %s: agent engine stop failed", self.id, exc_info=True)

    def takeover(self):
        """Hand the page to the user inside the live view: the bot pauses and the
        page drives the tab over its own DevTools socket. No relaunch."""
        self.pause()
        self.wake_browser()
        self.set_status("paused" if self.thread and self.thread.is_alive() else "idle", control=True)

    def giveback(self):
        if self.meta.get("visible"):
            self.window(False)
        self.meta["control"] = False
        self.save()
        self.resume()

    def wake_browser(self):
        """Relaunch an asleep browser (headless). A browser that is already up is
        left exactly as it is (start(False) here used to tear down a popped-out
        window the moment the user clicked Take over)."""
        if self.browser.alive():
            return
        self.browser.start(self.browser.visible())  # asleep -> no session -> headless
        # Waking restarts the idle clock (idle_sleep_due measures from meta["updated"]).
        with self.lock:
            self.meta["updated"] = time.time()
            self.save()

    def window(self, visible, closed=False):
        """Pop the same profile out as a real Chrome window on the desktop (or
        back to headless). While visible, the bot is paused and you drive the
        real window; the live view keeps mirroring it."""
        if visible:
            self.pause()
            self.meta["control"] = True
        self.browser.set_visible(visible)
        self.meta["visible"] = visible
        handback = closed and bool(self.meta.get("control"))
        if handback:
            self.window_closed = True
            self.meta["control"] = False
        self.save()
        self.emit("system", "Opened this bot's browser as a real window on your desktop. Hand back (or close the window) to return control to the bot."
                  if visible else ("Desktop window closed; the browser is back here, headless"
                                   + (" and the bot has control again." if handback else ".") if closed
                                   else "Browser is headless again; the live view is the only window."))
        if handback:
            if self.thread and self.thread.is_alive():
                self.resume(note=False)
            else:
                self.set_status("idle", control=False)

    def _closed_window_note(self, history):
        if not self.window_closed:
            return
        self.window_closed = False
        history.append("The user popped your browser out as a real desktop window and then closed that window "
                       "instead of handing back, so control returned to you. The browser was relaunched headless "
                       "on the last page it knew about (possibly about:blank). Nothing was necessarily accomplished "
                       "in that window; do not assume a login or any step succeeded. Act on the observation below.")

    def _recover_popup(self):
        if self.meta.get("control") and self.browser.recover_stuck_google_popup():
            self.emit("system", "Google sign-in completed but its popup couldn't hand back to "
                      "the page; closed it and reloaded to finish.")

    def set_encrypt(self, on):
        """Turn profile encryption at rest on or off. Takes effect at once when
        Chrome is closed; otherwise at the next stop (idle sleep, dock, delete)."""
        on = bool(on)
        self.meta["encrypt"] = on
        self.browser.encrypt = on
        self.save()
        if not self.browser.alive():
            if on:
                if self.browser.seal():
                    self.emit("system", "Profile encrypted at rest. It is decrypted only while this bot's browser runs.")
            else:
                if self.browser.unseal():
                    self.emit("system", "Profile encryption turned off; the profile is stored in plain files again.")
        elif on:
            self.emit("system", "Profile encryption on: the profile is sealed whenever the browser sleeps or closes.")

    def idle_sleep_due(self, selected):
        """Idle for a while, headless, not being looked at or driven -> put the
        browser to sleep. The next task, take-over or live view relaunches it."""
        if selected or self.meta.get("control"):
            return False
        if self.meta.get("status") not in ("idle", "error") or (self.thread and self.thread.is_alive()):
            return False
        if time.time() - (self.meta.get("updated") or 0) < IDLE_SLEEP_S:
            return False
        return self.browser.alive() and not self.browser.visible()

    def idle_sleep(self):
        try:
            self.browser.stop()  # seals too when browser.encrypt is on
            self.emit("system", f"Browser closed after {IDLE_SLEEP_S // 60} minutes idle"
                      + ("; profile encrypted at rest." if self.meta.get("encrypt") else "."))
        except Exception as e:  # noqa: BLE001
            self.emit("error", f"Could not put the browser to sleep: {e}")

    def shutdown(self):
        self.stop()
        if self.thread:
            self.thread.join(5)
        self.browser.stop()

    # -- waits the engines share --------------------------------------------
    def _wait_if_paused(self):
        while self.pause_flag.is_set() and not self.stop_flag.is_set():
            self.wake.wait(1)
            self.wake.clear()
            self._recover_popup()

    def _drain_inbox(self):
        with self.lock:
            msgs, self.inbox = self.inbox, []
        return msgs

    def _await_answer(self, timeout=None):
        """Block until the user sends something (or hits Stop); with `timeout`, give up
        after that many seconds. Returns (answers, timed_out)."""
        end = time.time() + timeout if timeout else None
        self.asking = True
        try:
            while not self.inbox and not self.stop_flag.is_set():
                if end and time.time() >= end:
                    return [], True
                self.wake.wait(1)
                self.wake.clear()
        finally:
            self.asking = False
        return self._drain_inbox(), False

    def _local_model_info(self, ai, real_id):
        try:
            cat = ai.models.catalog()
        except Exception:  # noqa: BLE001
            return None
        for cap in cat.get("capabilities", []):
            if cap.get("capability") != "text-generation":
                continue
            for m in cap.get("models", []):
                if m.get("id") == real_id:
                    return m
        return None

    def _ensure_model_ready(self, ai, task):
        """A local model must be downloaded before the first call: ask, download
        with progress, or end the task. True when the model is ready."""
        alias = self.meta.get("model") or DEFAULT_MODEL
        real = LOCAL_MODELS.get(alias)
        if not real or alias in self.model_ready:
            return True
        info = self._local_model_info(ai, real)
        if info is not None and info.get("downloaded"):
            self.model_ready.add(alias)
            return True
        size = info.get("size_gb") if info is not None else LOCAL_MODEL_SIZES_GB.get(real)
        q = (f"This bot's model needs to download (~{size:.1f} GB) before it can run locally. Download it now?" if size
             else "This bot's model needs to download before it can run locally. Download it now?")
        ev = self.emit("question", q, options=["Download now", "Cancel"])
        self.set_status("waiting", waiting_on=ev["seq"])
        self.asking = True
        try:
            while not self.inbox and not self.stop_flag.is_set():
                self.wake.wait(1)
                self.wake.clear()
        finally:
            self.asking = False

        def stopped():
            self.emit("system", "Stopped")
            self.set_status("idle", note="", dl_pct=None, waiting_on=None)
            self._routine_outcome(task, "stopped", "")
            return False

        if self.stop_flag.is_set():
            return stopped()
        answer = " ".join(self._drain_inbox()).strip().lower()
        if not (answer.startswith("download") or (_YES.match(answer) and not _NO.match(answer))):
            msg = "OK, I won't download the model. Pick a different one in Settings, or ask again when you're ready."
            self.emit("done", msg)
            self.set_status("idle", note="", waiting_on=None)
            self._routine_outcome(task, "done", msg)
            return False
        self.set_status("running", waiting_on=None)
        last_pct = -1
        last_chat_pct = -1

        def on_progress(job):
            nonlocal last_pct, last_chat_pct
            if self.stop_flag.is_set():
                _cancel_job(ai, job.get("id"))
                raise RuntimeError("stopped by user")
            done, total = job.get("done"), job.get("total")
            pct = int(done * 100 / total) if done is not None and total else None
            if pct != last_pct:
                last_pct = pct
                self.set_status("running", note=f"Downloading model… {pct}%" if pct is not None else "Downloading model…",
                                dl_pct=pct)
            if last_chat_pct < 0 or (pct is not None and pct - last_chat_pct >= 10) or pct == 100:
                last_chat_pct = pct if pct is not None else 0
                self.emit("system", f"Downloading model… {pct}%" if pct is not None else "Downloading model…")
        try:
            ai.models.download(real, capability="text-generation", on_progress=on_progress, timeout=3600)
        except Exception as e:  # noqa: BLE001
            if self.stop_flag.is_set():
                return stopped()
            self.emit("error", f"Model download failed: {e}")
            self.set_status("error", note=str(e)[:200], dl_pct=None)
            self._routine_outcome(task, "error", str(e))
            return False
        self.emit("system", "Model downloaded.")
        self.set_status("running", note="", dl_pct=None)
        self.model_ready.add(alias)
        return True

    # -- builds: Claude Code makes an app ---------------------------------------
    def build(self, name, spec, fresh=False):
        """Start one Claude task that creates a fused-render app, then watch it.
        An app of the same name already under the apps root is updated in place
        (the task gets an "update" prompt) unless `fresh` asks for a separate copy.
        Returns the (label, result) pair the engine hands back to the model."""
        name = " ".join((name or "").split())[:60] or "App"
        if not (spec or "").strip():
            return f"build \"{name}\"", "error: build needs `text`, a spec of what the app should do"
        self._settle_offer()  # a build answers any app offer still open
        builds_root = _builds_root()
        d = os.path.join(builds_root, _slug(name))
        update = os.path.isfile(os.path.join(d, "index.html")) and not fresh
        if not update and os.path.isdir(d) and os.listdir(d):
            d = os.path.join(builds_root, f"{_slug(name)}-{uuid.uuid4().hex[:4]}")
        os.makedirs(d, exist_ok=True)
        from fused_render.bots import tools as _tools
        mode = BUILD_MODES.get(_tools.effective_build_access(self), "default")  # scoped for a phone-started Super Bot task
        r = _tasks_api("POST", "/api/tasks/create", {
            "prompt": _build_prompt(name, d, spec, update=update), "target": d, "title": f"{'Update' if update else 'Build'} · {name}",
            "model": BUILD_MODEL, "effort": BUILD_EFFORT, "permission_mode": mode})
        bd = {"entry_id": r.get("entry_id") or "", "key": r.get("key") or "", "name": name, "dir": d,
              "mode": mode, "created_at": time.time(),
              # the channel the asking task came from: the "ready" message goes back there (router D5)
              "via": None if chan.is_web(getattr(self, "task_via", None)) else dict(self.task_via)}
        with self.lock:
            self.meta["builds"] = (self.meta.get("builds") or [])[-39:] + [bd]
            self.save()
        link = _app_link(d)
        verb = "Update" if update else "Build"
        self.emit("system", f"{verb} started: {name}. Follow it under Builds.")
        if not update:
            try:
                self._record_artifact(d, "build", link=link, title=name)  # the app folder shows up in the Inbox list
            except Exception:  # noqa: BLE001
                pass
        self._watch_build(bd)
        how = "it may pause to ask you under Builds" if mode == "default" else "it runs unattended"
        doing = "updating the existing app" if update else "building it"
        return (f"{verb.lower()} \"{name}\"",
                f"started; Claude is {doing} now ({how}; a few minutes). Link for the user: {link} . "
                f"Now `done`: tell the user you are {doing} \"{name}\", include that exact link, and that they will hear when it is ready.")

    def _watch_build(self, bd):
        """Background: poll the task until it settles, then tell the user (a `done`
        event reaches the chat, and iMessage when that is on)."""
        def run():
            from urllib.parse import quote
            seen_running, stable = False, ""
            deadline = float(bd.get("created_at") or time.time()) + BUILD_MAX_S
            while time.time() < deadline:
                time.sleep(BUILD_POLL_S)
                try:
                    rows = _tasks_api("GET", f"/api/tasks?under={quote(bd['dir'])}").get("tasks") or []
                except Exception:  # noqa: BLE001
                    continue
                row = next((t for t in rows if t.get("entry_id") == bd["entry_id"] or (bd.get("key") and t.get("key") == bd["key"])), None)
                st = (row or {}).get("status") or ""
                if st in ("in_progress", "queued", "needs_attention", "blocked"):
                    seen_running = True
                if st in ("needs_attention", "blocked") and not bd.get("nudged"):
                    bd["nudged"] = True
                    self.emit("question", f"The build of \"{bd['name']}\" is waiting for you: answer Claude under Builds.",
                              source="build", via=bd.get("via"))
                if st in ("done", "archived") and seen_running:
                    if stable != st:      # status can flicker for ~15 s after a turn: want it twice in a row
                        stable = st
                        continue
                    reply = " ".join(((row or {}).get("last_reply") or "").split())[:400]
                    # `app` lets the chat render an app card under this message (open inline / beside the chat).
                    self.emit("done", f"Your app \"{bd['name']}\" is ready: {_app_link(bd['dir'])}" + (f"\n\n{reply}" if reply else ""),
                              app={"name": bd["name"], "dir": bd["dir"]}, source="build", via=bd.get("via"))
                    with self.lock:
                        bd["done_at"] = time.time()
                        self.save()
                    return
                stable = ""
        threading.Thread(target=run, name=f"build-{(bd.get('entry_id') or '')[:8]}", daemon=True).start()

    # -- hand-offs: Super Bot gives a bot a task (docs §11) ---------------------
    # meta["handoffs"] on Super Bot: [{id, target, target_name, task, origin_via, created_at,
    #   state: queued|running|waiting|done|error|stopped, done_at?, result?}], last HANDOFF_KEEP.
    # Task text goes down, status and ONE result come up. The target's ask / login / approvals stay
    # in its own chat for the user at the Mac; Super Bot never answers for it. Every change to a
    # row happens under Super Bot's lock (summary() copies the rows under it).
    def _handoff_via(self, hd):
        return chan.handoff_via(self.id, hd["id"])

    @staticmethod
    def _handoff_ref(hd, **extra):
        return {"id": hd["id"], "target": hd["target"], "target_name": hd.get("target_name") or "",
                "state": hd["state"], **extra}

    @staticmethod
    def _exists(bid):
        """bot.json is still on disk: a deleted bot must never be written back by a late save."""
        return bool(bid) and os.path.isfile(os.path.join(bpaths.bot_dir(bid), "bot.json"))

    def _handoff_target(self, name):
        """(Bot, "") for an ordinary bot's name (exact, case-insensitive, then a
        unique prefix; two bots with the same exact name: the idle one), or
        (None, "error: …") with the names the model may use."""
        want = " ".join((name or "").split()).lower()
        cands, supers = [], []
        for bid in _list_ids():
            try:
                m = _read_meta(bid)
            except Exception:  # noqa: BLE001
                continue
            n = " ".join((m.get("name") or "").split())
            if n:
                (supers if is_super(m) or bid == self.id else cands).append((bid, n, m))
        names = ", ".join(sorted({n for _, n, _ in cands})) or "none yet"
        if not want:
            return None, f"error: name a bot from BOTS ({names})"
        if any(n.lower() == want for _, n, _ in supers):
            return None, f"error: a hand-off to Super Bot is refused (only the BOTS take hand-offs: {names}); do it yourself"
        exact = [c for c in cands if c[1].lower() == want]
        if len(exact) > 1:
            idle = [c for c in exact if (c[2].get("status") or "idle") in ("idle", "error")]
            exact = idle[:1] or exact
        hit = exact or [c for c in cands if c[1].lower().startswith(want)]
        if len(hit) != 1:
            if not hit:
                return None, f"error: no bot named {name!r}. BOTS: {names}"
            return None, (f"error: several bots match {name!r} ({', '.join(n for _, n, _ in hit)}) and none is free to pick; "
                          "use a bot's full name, or tell the user two bots share a name")
        bid = hit[0][0]
        try:
            return _registry().get(bid), ""
        except ValueError:
            return None, f"error: no bot named {name!r}. BOTS: {names}"

    def _handoff_start(self, t, hd, from_queue=False):
        """Start hand-off `hd` on bot `t` when `t` is idle and `hd` is next in
        its queue (a fresh hand-off never jumps a queue). True when started;
        a queued hand-off leaves the queue only once its task has started."""
        hv = self._handoff_via(hd)
        key = (self.id, hd["id"])
        with t.lock:
            if t.running() or not self._exists(t.id):
                return False
            q = t._handoff_queue
            if from_queue:
                if not q or q[0] != key:
                    return False
            elif q:
                return False
            if t.meta.get("control"):
                t.meta["control"] = False  # a fresh task means the bot drives again (as receive() does)
            t.emit("user", hd["task"], via=hv)  # the task in the bot's own chat, chip "from Super Bot"
            start_seq = t.seq
            if not t.start_task(hd["task"], origin=chan.HANDOFF_KIND, via=hv):
                return False
            if from_queue and q and q[0] == key:
                q.pop(0)
            self._handoff_threads[hd["id"]] = t.thread
            with self.lock:
                hd.update(state="running", start_seq=start_seq, started_at=time.time())
                if self._exists(self.id):
                    self.save()
        return True

    def handoff(self, target_name, task):
        """`handoff`: give an ordinary bot a task and watch it (Super Bot only).
        A busy bot gets it queued; its watcher starts it when the bot is idle.
        Returns the (label, result) pair the engine hands back to the model."""
        task = (task or "").strip()
        label = f"handoff \"{' '.join((target_name or '').split())[:60] or '?'}\""
        if not is_super(self.meta):
            return label, "error: only Super Bot hands tasks to other bots"
        if not task:
            return label, "error: handoff needs `task`, a self-contained description of what to do"
        t, err = self._handoff_target(target_name)
        if t is None:
            return label, err
        name = " ".join((t.meta.get("name") or "bot").split())
        label = f"handoff \"{name}\""
        hd = {"id": uuid.uuid4().hex[:8], "target": t.id, "target_name": name, "task": task,
              # the channel the asking task came from: the result goes back there (router origin rule)
              "origin_via": None if chan.is_web(getattr(self, "task_via", None)) else dict(self.task_via),
              "created_at": time.time(), "state": "queued"}
        with self.lock:
            self.meta["handoffs"] = (self.meta.get("handoffs") or [])[-(HANDOFF_KEEP - 1):] + [hd]
            self.save()
        try:
            started = self._handoff_start(t, hd)
            if not started:
                with t.lock:
                    t._handoff_queue.append((self.id, hd["id"]))
        except Exception as e:  # noqa: BLE001 — never leave a row "queued" with no watcher behind it
            logger.warning("hand-off %s to %s did not start", hd["id"], t.id, exc_info=True)
            with self.lock:
                hd.update(state="error", done_at=time.time(), result=f"could not start: {e}"[:400])
                self.save()
            return label, f"error: could not hand the task to {name}: {e}"
        self.emit("system", f"Asked {name} to: {task}", source="handoff", handoff=self._handoff_ref(hd))
        self._watch_handoff(hd)
        if started:
            return label, (f"started; {name} is working on it now (it may pause for the user's approval in its own chat). "
                           f"Now finish: tell the user you asked {name}, in one short sentence, and that they will hear when it is done.")
        return label, (f"queued; {name} is busy and starts this as soon as its current task ends. Now finish: tell the user "
                       f"you asked {name}, in one short sentence, and that they will hear when it is done.")

    def _handoff_is_running(self, t, hd):
        """`t` is running THIS hand-off's task right now (its via and the thread it started)."""
        return (t.running() and dict(getattr(t, "task_via", None) or {}) == self._handoff_via(hd)
                and self._handoff_threads.get(hd["id"]) is t.thread)

    def handoff_stop(self, target_name):
        """`handoff_stop`: cancel what THIS Super Bot handed to a bot: a queued
        hand-off is dropped, a running one is stopped. Anything else the bot is
        doing (the user's own task, a routine) is refused."""
        label = f"handoff_stop \"{' '.join((target_name or '').split())[:60] or '?'}\""
        if not is_super(self.meta):
            return label, "error: only Super Bot hands tasks to other bots"
        t, err = self._handoff_target(target_name)
        if t is None:
            return label, err
        name = " ".join((t.meta.get("name") or "bot").split())
        label = f"handoff_stop \"{name}\""
        with self.lock:
            mine = [hd for hd in self.meta.get("handoffs") or [] if hd.get("target") == t.id and not hd.get("done_at")]
        dropped, running = [], False
        with t.lock:
            for hd in mine:
                key = (self.id, hd["id"])
                if hd.get("state") == "queued" and key in t._handoff_queue:
                    t._handoff_queue.remove(key)
                    dropped.append(hd)
            running = any(self._handoff_is_running(t, hd) for hd in mine)
        for hd in dropped:
            self._handoff_finish(None, hd, "stopped", f"Cancelled before {name} started it.")
        if running:
            t.stop()  # the watcher reports the outcome (state "stopped") once the task has ended
        if not dropped and not running:
            return label, f"error: {name} is not working on anything you handed off; only your own hand-offs can be stopped"
        return label, (f"stopped; {name} will not finish what you handed off. Now finish: tell the user it is "
                       "cancelled, in one short sentence.")

    def _watch_handoff(self, hd):
        """Background: start a queued hand-off when its bot is free, tell the
        user once when the bot waits on them at the Mac, then report the ONE
        result to Super Bot's chat (and, by the origin rule, to the phone the
        asking task came from). Both bots are looked up again on every poll: a
        deleted one is never written back. Emits carry `via` explicitly: this
        is not the task thread."""
        def run():
            name = hd.get("target_name") or "the bot"
            t = None
            try:
                deadline = float(hd.get("created_at") or time.time()) + HANDOFF_MAX_S
                while time.time() < deadline:
                    time.sleep(HANDOFF_POLL_S)
                    if hd.get("done_at"):
                        return  # settled elsewhere (handoff_stop on a queued hand-off)
                    if not self._exists(self.id):
                        return  # Super Bot was deleted: nobody to report to, and nothing to save
                    try:
                        t = _registry().get(hd["target"]) if self._exists(hd["target"]) else None
                    except ValueError:
                        t = None
                    if t is None:
                        self._handoff_finish(None, hd, "error", f"{name} was deleted before it finished.")
                        return
                    if hd.get("state") == "queued":
                        self._handoff_start(t, hd, from_queue=True)
                        continue
                    th = self._handoff_threads.get(hd["id"])
                    if th is not None and th.is_alive():
                        self._handoff_progress(t, hd)
                        continue
                    self._handoff_settle(t, hd)
                    return
                started = hd.get("start_seq") is not None
                if t is not None and started and self._handoff_is_running(t, hd):
                    t.stop()  # its late result would be lost: end it rather than leave it running unwatched
                self._handoff_finish(t if started else None, hd, "error",
                                     f"No result from {name} after {HANDOFF_MAX_S // 3600} hours, so it was stopped; "
                                     "its chat has what it got to.")
            except Exception as e:  # noqa: BLE001 — a dead watcher must not leave the row "running" forever
                logger.warning("hand-off %s: watcher failed", hd.get("id"), exc_info=True)
                self._handoff_finish(t if hd.get("start_seq") is not None else None, hd, "error",
                                     f"Lost track of {name}'s task ({e}); its chat has the details.")
            finally:
                # Never leave this hand-off at the head of the bot's queue: later ones would stall behind it.
                try:
                    tq = t if t is not None else _registry().get(hd["target"])
                    with tq.lock:
                        key = (self.id, hd["id"])
                        if key in tq._handoff_queue:
                            tq._handoff_queue.remove(key)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=run, name=f"handoff-{hd['id']}", daemon=True).start()

    def _handoff_progress(self, t, hd):
        """While the target works: mirror running / waiting, and the first time
        it waits on the user, one line in Super Bot's chat (texted back by the
        origin rule). Super Bot never answers for it."""
        new = "waiting" if t.meta.get("status") == "waiting" else "running"
        with self.lock:
            if new == hd.get("state"):
                return
            hd["state"] = new
            ask = new == "waiting" and not hd.get("asked")
            if ask:
                hd["asked"] = True
            if not self._exists(self.id):
                return
            self.save()
        if ask:
            ws = t.meta.get("waiting_on")
            ev = t.event_by_seq(ws) if ws else None
            reason = " ".join(((ev or {}).get("text") or "").split())
            reason = re.sub(r"\s*Approve\?\s*$", "", reason)[:300] or "it is waiting for you"
            self.emit("question", f"{hd.get('target_name') or 'A bot'} needs you at the laptop: {reason} Answer it at the Mac.",
                      source="handoff", handoff=self._handoff_ref(hd), via=hd.get("origin_via"))

    def _handoff_settle(self, t, hd):
        """The target's task ended: read what it wrote since the hand-off
        started (events stamped with this hand-off's via) and report it."""
        hv = self._handoff_via(hd)
        name = hd.get("target_name") or "The bot"
        start = int(hd.get("start_seq") or 0)
        stopped, done, error = False, None, None
        for _, ev in _iter_events(t.events_path):
            if int(ev.get("seq") or 0) <= start or dict(ev.get("via") or {}) != hv:
                continue
            role = ev.get("role")
            if role == "system" and (ev.get("text") or "").strip() == "Stopped":
                stopped = True
            elif role == "done" and ev.get("source") != "build":
                done = ev
            elif role == "error":
                error = ev
        last = getattr(t, "last_task_dir", None)  # (via, folder) of the task that ended last
        task_dir = (last[1] or "") if isinstance(last, tuple) and dict(last[0] or {}) == hv else ""
        if stopped and done is None:
            self._handoff_finish(t, hd, "stopped", f"{name} was stopped before it finished.", task_dir=task_dir)
        elif done is not None:
            self._handoff_finish(t, hd, "done", (done.get("text") or "").strip() or "(no answer text)",
                                 summary=(done.get("summary") or "").strip(), task_dir=task_dir)
        elif error is not None:
            self._handoff_finish(t, hd, "error", (error.get("text") or "").strip() or "error", task_dir=task_dir)
        else:
            self._handoff_finish(t, hd, "error", f"{name} ended without a result; its chat has the details.",
                                 task_dir=task_dir)

    def _handoff_finish(self, t, hd, state, text, summary="", task_dir=""):
        """Record the outcome, put the result card in Super Bot's chat (via the
        asking task's origin) and, when the target ran it, the line in the
        target's chat that says what went up. Nothing is written for a bot
        that has been deleted."""
        with self.lock:
            if hd.get("done_at"):
                return
            hd.update(state=state, done_at=time.time(), result=text[:4000])
            self._handoff_threads.pop(hd["id"], None)
            if not self._exists(self.id):
                return
            self.save()
        if not summary:
            flat = " ".join(text.split())
            m = re.match(r"(.+?[.!?])(?:\s|$)", flat)
            summary = (m.group(1) if m else flat)[:600]
        self.emit("done", text, summary=summary, source="handoff",
                  handoff=self._handoff_ref(hd, task=hd["task"], task_dir=task_dir or ""), via=hd.get("origin_via"))
        if t is not None and self._exists(t.id):
            try:
                t.emit("system", f"Sent to Super Bot: {text[:280]}", source="handoff", via=None)
            except Exception:  # noqa: BLE001
                logger.debug("hand-off line not written on %s", t.id, exc_info=True)

    # -- app tools and app Python ----------------------------------------------
    _tool_calls = 0

    def run_tool(self, app, name, args):
        """`tool`: run one app MCP tool (apptools.py). The first call into an app in a
        task also drops that app's card into the chat."""
        if not apptools.available():
            return f"tool {app} › {name}", "error: app tools are not available in this copy of the app (bundled fused module missing)"
        recs = apptools.registry()
        rec = apptools.find(recs, app, name)
        if rec is None:
            have = ", ".join(sorted({r.app for r in recs})) or "none"
            return f"tool {app or '?'} › {name or '?'}", f"error: no such tool. Apps with tools: {have}. Use exact names from APP TOOLS."
        res = apptools.run_tool(rec, args)
        return self._deliver(f"tool {rec.app} › {rec.name}", res, rec.app_dir, rec.name, args,
                             f"tool-{rec.app}-{rec.name}", tools=rec.tools_in_app, what="the tool")

    def _deliver(self, label, res, app_dir, name, args, save_stem, tools=0, what="the file"):
        """Hand a tool's or a file's RunResult back to the engine: the "Used …" line
        (+ the app's card the first time a task touches that app), unknown-arg note,
        result cap with the full text spilled to the Inbox. Shared by `tool` and `py`."""
        self._tool_calls += 1
        used = getattr(self, "_tool_apps", None)
        if used is None:
            used = self._tool_apps = set()
        extra = {}
        if app_dir not in used:
            used.add(app_dir)
            extra["app"] = {"name": _app_title(app_dir), "dir": app_dir, "tools": tools}
        # A harness line, not bot speech: role `note` (muted, no react/reply).
        self.emit("note", f"Used {_app_title(app_dir)} › {name}", detail=json.dumps(args or {}, ensure_ascii=False)[:400], **extra)
        note = f"\n(ignored unknown args: {', '.join(res.dropped)}; {what} takes only the parameters listed)" if res.dropped else ""
        if not res.ok:
            return label, res.text + note
        text = res.text
        if len(text) > apptools.RESULT_CAP:
            try:
                p = self.save_file(f"{save_stem}-{self._tool_calls}.json", text)
                tail = f"\n(truncated; full result saved to Inbox as {os.path.basename(p)})"
            except Exception as e:  # noqa: BLE001
                tail = f"\n(truncated; could not save the full result: {e})"
            text = text[:apptools.RESULT_CAP] + tail
        return label, "RESULT:\n" + text + note

    def py_ref(self, d):
        """(app_dir or None, file, args) from a `py` decision — one resolution for
        risk, describe and execute alike (the tool_ref rule), so a call can never
        reach execute on a different folder than the gate judged."""
        d = d or {}
        app_dir = apptools.resolve_app(d.get("app") or d.get("name") or "")
        file = str(d.get("file") or "").strip()
        args = d.get("args")
        return app_dir, file, args if isinstance(args, dict) else ({} if args is None else args)

    def run_py(self, d):
        """`py`: load an app's SKILL.md (no file), or run one file it documents through
        the server's own /api/run. Same delivery as `tool`."""
        app_dir, file, args = self.py_ref(d)
        if not app_dir:
            have = ", ".join(a["folder"] for a in apptools.apps()[:40]) or "none"
            return f"py {(d or {}).get('app') or '?'}", f"error: no such app. Use a folder or name from APPS: {have}"
        stem = os.path.basename(app_dir)
        skill = apptools.read_skill(app_dir)
        if skill is None:
            return f"py {stem}", (f"error: {stem} has no SKILL.md, so its Python is not callable. Use its page instead, or, if the "
                                  f"user wants it, `build` with `name` = its folder name ({stem}) and text \"Add a SKILL.md that documents every "
                                  f".py with a main() (authoring skill, App SKILL.md section)\".")
        if not file:
            loaded = self.__dict__.setdefault("_skills_loaded", [])
            if app_dir not in loaded:
                loaded.append(app_dir)
            docs = ", ".join(skill["files"]) or "none"
            return f"py {stem}", f"RESULT:\nLoaded {stem}'s SKILL.md; it is under APP SKILLS from the next step on. Callable files: {docs}."
        name = apptools.skill_file(skill, file)
        if name is None:
            return (f"py {stem} › {file}",
                    f"error: {file} has no section in {stem}'s SKILL.md, so it cannot be called. Documented files: "
                    f"{', '.join(skill['files']) or 'none'}.")
        if not os.path.isfile(os.path.join(app_dir, name)):
            return f"py {stem} › {name}", f"error: {stem}'s SKILL.md documents {name}, but the file is not in the app folder."
        try:
            origin = _server_origin()
        except Exception as e:  # noqa: BLE001
            return f"py {stem} › {name}", f"error: fused-render is not reachable: {e}"
        res = apptools.run_py(origin, app_dir, name, args)
        return self._deliver(f"py {stem} › {name}", res, app_dir, name, args,
                             f"py-{stem}-{name[:-3]}", tools=apptools.count_tools(app_dir))

    def _skill_dirs(self, task):
        """Which apps' SKILL.md the prompt mounts: the ones loaded with `py app`,
        this bot's builds started during this task, then the two apps the task
        names best (`_relevant_apps`). Everything else is one `py app` away."""
        out = list(getattr(self, "_skills_loaded", None) or [])
        since = getattr(self, "task_started", None) or 0
        for bd in reversed(self.meta.get("builds") or []):
            if isinstance(bd, dict) and bd.get("dir") and (bd.get("created_at") or 0) >= since:
                out.append(bd["dir"])
        with_skill = [a for a in apptools.apps() if a.get("skill")]
        out += [a["dir"] for _, a in _relevant_apps(task, with_skill)]
        return out

    def _resolve_app(self, name, obs=None):
        """A built app by name, folder or link, or (no name) the page the bot is on:
        {"name", "dir", "params"} or None. `show` and `offer` share this lookup."""
        name = (name or "").strip()
        app = _app_at(name) if name else None
        if not app and not name:
            app = _app_at((obs or {}).get("url") or "")
        if not app and name:
            want, slug = name.lower(), _slug(name)
            cands = []
            for bd in reversed(self.meta.get("builds") or []):     # this bot's own builds first
                if bd.get("dir") and (want in (bd.get("name") or "").lower() or slug in os.path.basename(bd["dir"])):
                    cands.append(bd["dir"])
            # then every app under the apps root, by folder or by the name its README gives it
            for a in apptools.apps():
                f = a["folder"]
                if (slug and (slug in f or f in slug or want in f.replace("-", " "))) or want == a["name"].lower() or want in a["name"].lower():
                    cands.append(a["dir"])
            app = next((a for a in (_app_at(c) for c in cands) if a), None)
        return app

    def show_app(self, name, obs):
        """`show`: post an app card into the chat for a built app, found by name/folder,
        by a link, or from the page the bot is on."""
        name = (name or "").strip()
        app = self._resolve_app(name, obs)
        if not app:
            have = ", ".join(f"{a['folder']} ({a['name']})" if a["name"] != a["folder"] else a["folder"] for a in apptools.apps())[:600]
            return f"show \"{name or 'this page'}\"", "error: no built app matches" + (f". Apps: {have}" if have else ". No apps have been built yet (use `build`).")
        self._settle_offer()  # showing an app answers any offer still open
        self.emit("thought", f"Here's {app['name']}:", app=app)
        return f"show \"{app['name']}\"", f"the app card is in the chat ({app['dir']}). Now `done` in one line; do not describe the app's contents."

    # -- offers: the bot proposes an app, the user answers with one click -------------
    # An offer is a `question` event carrying `offer` ({kind: use|build, name, dir, spec})
    # and two options. The engine waits OFFER_WAIT_S for the answer: a yes to a build
    # offer starts the build at once (the yes is the approval), a yes to a use offer
    # drops the app card. Unanswered, the task carries on and bot.json keeps the offer
    # as `pending_offer`, so the card stays clickable and a later bare yes/no (see
    # _answer_pending_offer) settles it without a model call. A no is remembered for
    # DECLINED_OFFER_S so the same app is not pushed again.
    def declined_offers(self):
        """{name or folder: ts} of app offers the user turned down within DECLINED_OFFER_S."""
        now = time.time()
        return {k: t for k, t in (self.meta.get("offers_declined") or {}).items() if now - float(t or 0) < DECLINED_OFFER_S}

    def _settle_offer(self, declined=False):
        with self.lock:
            po = self.meta.pop("pending_offer", None)
            if declined and po:
                d = dict(self.meta.get("offers_declined") or {})
                d[(po.get("name") or "").strip().lower()] = time.time()
                if po.get("dir"):
                    d[os.path.basename(po["dir"])] = time.time()
                self.meta["offers_declined"] = dict(sorted(d.items(), key=lambda kv: kv[1])[-40:])
            if po:
                self.save()

    @staticmethod
    def _offer_verdict(answers, yes_label, strict=False):
        """'yes' | 'no' | None (free text) for what the user replied to an offer. `strict`
        only takes a bare answer (a whole message that is a yes or a no)."""
        for a in answers:
            s = " ".join((a or "").split())
            if not s:
                continue
            if s.lower() == yes_label.lower() or (_BARE_YES if strict else _YES).match(s) or (not strict and _OFFER_YES.match(s)):
                return "yes"
            if s.lower() == "not now" or (_BARE_NO if strict else _NO).match(s) or (not strict and _OFFER_NO.match(s)):
                return "no"
        return None

    def _offer_hints(self, task):
        """Step-1 lines that point the model at an `offer`: apps that look relevant,
        or a task that reads like something the user will want again."""
        if getattr(self, "task_origin", "manual") == "routine":
            return []
        declined = self.declined_offers()
        try:
            fits = [a for _, a in _relevant_apps(task, apptools.apps())]
        except Exception:  # noqa: BLE001
            fits = []
        live = [a for a in fits if a["folder"] not in declined and a["name"].strip().lower() not in declined]
        if _ASKS_FOR_APP.search(task or ""):
            near = (f" An app named \"{live[0]['name']}\" ({live[0]['folder']}) already exists: `build` with that exact name updates "
                    "it in place, unless the user wants a separate one." if live else "")
            return ["APP HINT: the user is asking for an app. Go straight to `build` with a clear spec (no `ask`, no `offer`); "
                    "the approval card confirms it with one click." + near]
        hints = [f"APP HINT: the app \"{a['name']}\" ({a['folder']}) looks relevant to this task. If it would serve the user better "
                 "than browsing, `offer` it before you browse (or `show` it if they asked to see it)." for a in live]
        if not fits and _APP_WORTHY.search(task or ""):  # a declined app that fits means: no app for this, do not push another
            hints.append("APP HINT: this task reads like something the user will want again or keep updating. If browsing produces "
                         "a result they would re-check, `offer` to build a small app for it right before `done` (findings in "
                         "`message`); skip the offer if it turns out to be a one-off.")
        return hints

    def _offer(self, d, obs, history):
        """`offer`: propose an app and wait for the one-click answer. Appends what happened
        to `history`; returns True when Stop was pressed meanwhile. Accepts the steps
        engine's spelling (spec in `text`) and the tool table's (`spec`)."""
        d = d or {}
        name = " ".join((d.get("name") or d.get("value") or "").split())[:60]
        spec = (d.get("spec") or d.get("text") or "").strip()
        msg = (d.get("message") or "").strip()
        if getattr(self, "task_origin", "manual") == "routine":
            history.append("offer -> skipped: routine tasks never offer (nobody is there to answer). Finish the task.")
            return False
        if self._offers >= 1:
            history.append("offer -> skipped: one offer per task, and you already made one. Finish the task.")
            return False
        app = self._resolve_app(name, obs) if name else None
        if not app and not name:
            history.append("offer -> error: give `name` (an app from APPS to use, or the name of the app to build)")
            return False
        if not app and not spec:
            history.append(f"offer -> error: no app named \"{name}\" exists, so this is a build offer and needs `text` (the spec); "
                           "to offer an existing app use its exact name or folder from APPS")
            return False
        kind = "use" if app else "build"
        declined = self.declined_offers()
        if (app["name"] if app else name).strip().lower() in declined or (app and os.path.basename(app["dir"]) in declined):
            history.append(f"offer -> skipped: the user declined \"{name}\" recently; do not offer it again, finish without it")
            return False
        self._offers += 1
        yes = "Use it" if app else "Build it"
        if not msg:
            msg = (f"{app['name']} looks like the right tool for this. Want to use it?" if app
                   else f"I could build you a small app for this: \"{name}\". Want me to?")
        offer = {"kind": kind, "name": app["name"] if app else name, "dir": app["dir"] if app else "", "spec": spec[:3000]}
        ev = self.emit("question", msg, options=[yes, "Not now"], offer=offer, **({"app": app} if app else {}))
        with self.lock:
            self.meta["pending_offer"] = {**offer, "seq": ev["seq"], "ts": time.time()}
            self.save()
        self.set_status("waiting", waiting_on=ev["seq"])
        self._offer_seq = ev["seq"]  # send() leaves the offer alone while this wait is up (see there)
        try:
            answers, timed_out = self._await_answer(OFFER_WAIT_S)
        finally:
            self._offer_seq = None
        if self.stop_flag.is_set():
            with self.lock:
                self.meta["waiting_on"] = None  # the engine's stop path sets the final status
                self.save()
            return True
        self.set_status("running", waiting_on=None)
        history.append(f"OFFERED ({kind}): {offer['name']}")
        if timed_out:
            history.append(f"No answer to your offer in {OFFER_WAIT_S // 60} min; the user can still accept it from the chat later. "
                           "Carry on without it: `done` in ONE short line if nothing else remains (the findings are already in the chat).")
            return False
        # A texted answer never settles the offer (a yes starts a build; answered at the Mac, docs §10):
        # it reaches the model as the user's words, below, and any build it leads to goes through the gate.
        verdict = self._offer_verdict([a for a in answers if not chan.is_texted(a)], yes)
        said = " ".join(a for a in answers if a.strip().lower() not in (yes.lower(), "not now"))
        if verdict == "no":
            self._settle_offer(declined=True)
            history.append(f"USER DECLINED your offer ({offer['name']}). Do not offer it again." + (f" USER: {said}" if said else "")
                           + " If nothing else remains, `done` in ONE short line; never repeat the findings.")
            return False
        if verdict == "yes":
            self._settle_offer()
            if kind == "build":
                label, result = self.build(offer["name"], spec + (f"\n\nThe user added when accepting: {said}" if said else ""))
                self.emit("action", label, result=result[:400])
                history.append(f"USER ACCEPTED your offer. {label} -> {result}")
            else:
                self.emit("thought", f"Here's {app['name']}:", app=app)
                history.append(f"USER ACCEPTED your offer to use \"{app['name']}\": its card is in the chat (link {_app_link(app['dir'])})."
                               + (f" USER: {said}" if said else "")
                               + " Now `goto` that link to drive it yourself, call its tools with `tool`, or `done` in one line if the user can take it from here.")
            return False
        history.append("USER ANSWER (about your offer): " + " ".join(answers)
                       + f" — if this reads as a yes, `build` (or `show`) \"{offer['name']}\" now; if it changes the idea, adjust the spec and `build`; if it is a no, finish without it.")
        return False

    def _answer_pending_offer(self, po, text):
        """A bare yes/no typed after the task ended with an offer still open: settle it
        without a model call. Anything else clears the offer and runs as a normal task.
        Returns True when the message was consumed."""
        yes = "Use it" if po.get("kind") == "use" else "Build it"
        verdict = self._offer_verdict([text], yes, strict=True)
        if verdict is None:
            self._settle_offer()
            return False
        if verdict == "no":
            self._settle_offer(declined=True)
            self.emit("done", f"Okay, no app for that. Just ask if you change your mind about \"{po.get('name')}\".")
            return True
        self._settle_offer()

        def go():
            try:
                if po.get("kind") == "use":
                    app = _app_at(po.get("dir") or "")
                    if app:
                        self.emit("done", f"Here's {app['name']}.", app=app)
                    else:
                        self.emit("error", f"\"{po.get('name')}\" is no longer under {_builds_root()}.")
                    return
                label, result = self.build(po.get("name"), po.get("spec") or "")
                self.emit("action", label, result=result[:400])
                if result.startswith("error"):
                    self.emit("error", result)
                    return
                bd = (self.meta.get("builds") or [{}])[-1]
                self.emit("done", f"Building \"{po.get('name')}\" now: {_app_link(bd.get('dir') or '')} . You'll hear here when it is ready.")
            except Exception as e:  # noqa: BLE001
                self.emit("error", f"Could not start the build: {e}")
        threading.Thread(target=go, daemon=True, name=f"offer-{self.id}").start()
        return True

    # -- contacts --------------------------------------------------------------
    def contacts(self):
        """[(label, handle)] the `text` action may message: Settings' contacts, plus Super Bot's own
        iMessage handle on Super Bot (on any other bot the `imessage` key is a leftover and grants nothing)."""
        owner = (self.meta.get("imessage") or "") if is_super(self.meta) else ""
        return imessage.parse_contacts(self.meta.get("imessage_to") or "", owner)

    def contact(self, d):
        """(label, handle) | None for a `text`/`texts` target (`to` / `ref` / `name`)."""
        d = d or {}
        return imessage.resolve_contact(d.get("to") or d.get("ref") or d.get("name") or "", self.contacts())


def bots_section(bot) -> str:
    """Super Bot's BOTS section (docs §11): one line per ordinary bot, read
    from disk (no Bot objects). Empty for every other bot."""
    if not is_super(getattr(bot, "meta", None)):
        return ""
    lines = []
    for bid in _list_ids():
        if bid == getattr(bot, "id", None):
            continue
        try:
            m = _read_meta(bid)
        except Exception:  # noqa: BLE001
            continue
        name = " ".join((m.get("name") or "").split())
        if not name or is_super(m):
            continue
        instr = " ".join((m.get("instructions") or "").split())[:120]
        lines.append(f"- {name} ({m.get('preset') or 'custom'}; {m.get('status') or 'idle'}): {instr}".rstrip(": "))
    return ("\n\nBOTS (the browser bots on this Mac; `handoff` gives one a task):\n"
            + ("\n".join(lines) if lines else "none yet"))


# ------------------------------------------------------------ create / delete ---
def _registry():
    from fused_render.bots import registry
    return registry


def _write_new_meta(bid, meta):
    os.makedirs(bpaths.bot_dir(bid), exist_ok=True)
    store.write_meta(bid, meta)


def create(name="", model="", effort="", instructions="", preset="", kind=""):
    """A new bot: bot.json, a `created` line, the greeting (background). Returns the Bot.
    With `preset` (a key under bots/presets/) its playbooks, brand face, standing
    rules and starter apps are applied before the greeting, so it introduces them.
    `kind="super"` makes Super Bot (KINDS): one per install, a Claude model, its own
    face and standing rules unless the user typed some; a preset does not apply."""
    preset = (preset or "").strip()
    kind = kind if kind in KINDS else "bot"
    if kind == "super":
        if super_id() is not None:
            raise ValueError("there is already a Super Bot; there is one per Mac")
        preset = ""
        if model in LOCAL_MODELS:
            model = DEFAULT_MODEL
    if preset:
        from fused_render.bots import presets as presets_mod
        if presets_mod.get(preset) is None:  # refuse before bot.json exists: no orphan bot
            raise ValueError(f"unknown preset {preset!r}")
    bid = uuid.uuid4().hex[:8]
    n = len(_list_ids())
    meta = {"id": bid, "name": name or f"Bot {n}", "model": model if model in MODELS else DEFAULT_MODEL,
            "effort": effort if effort in EFFORTS else DEFAULT_EFFORT, "status": "idle", "instructions": (instructions or "").strip(),
            "created": time.time(), "task": "", "step": 0, "url": None, "title": None}
    if kind == "super":
        meta.update({"kind": "super", "super_access": "ask", "handoffs": [], "name": name or SUPER_NAME, "face": dict(SUPER_FACE),
                     "instructions": meta["instructions"] or SUPER_INSTRUCTIONS,
                     "pinned": True})  # the one bot per Mac starts pinned (sidebar); the user can unpin it
    _write_new_meta(bid, meta)
    b = _registry().get(bid)
    if preset:
        presets_mod.apply_preset(b, preset)  # before the greeting, so it introduces the playbooks it has
    b.emit("system", f"{meta['name']} created." + (f" Comes with {len(b.skills())} {b.meta['preset']} playbooks." if preset else ""))
    b.greet()
    return b


_CLONE_SKIP = {"SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile", "DevToolsActivePort",
               "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
               "CacheStorage", "Service Worker", "BrowserMetrics", "Crashpad"}


def _copy_lenient(a, b_):
    try:  # the source Chrome is usually running; files may vanish or be locked mid-copy
        shutil.copy2(a, b_)
    except OSError:
        pass


def clone(src_id, name=""):
    """New bot with a copy of another bot's Chrome profile (cookies, local
    storage, saved logins), memory, instructions and skills. Caches and
    Chrome's lock files are skipped."""
    reg = _registry()
    src = reg.get(src_id)
    if is_super(src.meta):
        raise ValueError("Super Bot cannot be cloned; there is one per Mac")
    bid = uuid.uuid4().hex[:8]
    os.makedirs(bpaths.bot_dir(bid), exist_ok=True)
    resealed = False
    if src.browser.sealed() and not src.browser.alive():
        src.browser.unseal()  # copy from plaintext; sealed again below
        resealed = True
    if os.path.isdir(src.browser.profile):
        try:
            shutil.copytree(src.browser.profile, os.path.join(bpaths.bot_dir(bid), "profile"), copy_function=_copy_lenient,
                            ignore=lambda d, names: [n for n in names if n in _CLONE_SKIP], dirs_exist_ok=True)
        except shutil.Error:
            pass  # per-file errors already swallowed; anything left is a listing race
    if resealed:
        try:
            src.browser.seal()
        except Exception:  # noqa: BLE001
            pass
    meta = {"id": bid, "name": name or f"{src.meta.get('name', 'Bot')} copy", "model": src.meta.get("model", DEFAULT_MODEL),
            "effort": src.meta.get("effort", DEFAULT_EFFORT), "instructions": src.meta.get("instructions", ""),
            "approval": src.meta.get("approval", "ask"), "build_access": src.meta.get("build_access", "scoped"),
            "trusted_apps": apptools.clean_trusted_apps(src.meta.get("trusted_apps")),
            "status": "idle", "created": time.time(), "task": "", "step": 0, "url": None, "title": None}
    if src.meta.get("engine"):
        meta["engine"] = src.meta["engine"]
    _write_new_meta(bid, meta)
    b = reg.get(bid)
    if src.memory():
        b.set_memory(src.memory())
    for sk in src.skills():
        b.skill_save(sk["title"], sk["trigger"], sk["body"], name=sk["name"])
    if src.meta.get("encrypt"):
        b.set_encrypt(True)  # seals the fresh copy right away
    b.emit("system", f"{meta['name']} created with {src.meta.get('name', 'the source bot')}'s logins and cookies.")
    b.greet()
    return b


def delete(bid):
    """Stop the bot's task and Chrome, forget it, remove its data and cache folders."""
    reg = _registry()
    b = reg.get(bid)
    b.shutdown()
    reg.forget(bid)
    shutil.rmtree(bpaths.bot_dir(bid), ignore_errors=True)
    shutil.rmtree(bpaths.bot_cache_dir(bid), ignore_errors=True)


# Your own Chrome's profiles (~/Library/Application Support/Google/Chrome/<dir>),
# offered in Settings so a bot can start from a copy of one: same logins,
# cookies, extensions and history you see in that Chrome window. A copy, not a
# link: Chrome holds a lock on its live profile, so the bot never touches it.
PROFILE_SKIP = {"SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile", "DevToolsActivePort",
                "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
                "CacheStorage", "Service Worker", "File System", "BrowserMetrics", "Crashpad"}


def chrome_dir() -> str:
    return os.path.expanduser("~/Library/Application Support/Google/Chrome")


def chrome_profiles():
    out = []
    root = chrome_dir()
    for d in sorted(os.listdir(root) if os.path.isdir(root) else []):
        p = os.path.join(root, d, "Preferences")
        if d in ("Guest Profile", "System Profile") or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                pref = json.load(f)
            email = (pref.get("account_info") or [{}])[0].get("email", "")
            out.append({"dir": d, "name": pref.get("profile", {}).get("name") or d, "email": email})
        except Exception:  # noqa: BLE001
            continue
    return out


def import_profile(b, dir_name):
    """Replace the bot's Chrome profile with a copy of one of yours. Runs in the
    background: a profile with years of history is gigabytes."""
    src = os.path.join(chrome_dir(), os.path.basename(dir_name or ""))
    if not dir_name or not os.path.isfile(os.path.join(src, "Preferences")):
        raise ValueError(f"no Chrome profile named {dir_name!r}")
    label = next((f"{p['name']} ({p['email']})" if p["email"] else p["name"] for p in chrome_profiles() if p["dir"] == dir_name), dir_name)

    def go():
        with b.browser.lock:
            try:
                b.emit("system", f"Copying your Chrome profile “{label}” into this bot; the browser restarts when it is done.")
                b.browser.stop(seal=False)
                shutil.rmtree(b.browser.profile, ignore_errors=True)
                try:
                    shutil.copytree(src, os.path.join(b.browser.profile, "Default"), copy_function=_copy_lenient,
                                    ignore=lambda d, names: [n for n in names if n in PROFILE_SKIP], dirs_exist_ok=True)
                except shutil.Error:
                    pass  # per-file errors already swallowed
                b.meta["chrome_profile"] = label
                b.save()
                if b.meta.get("encrypt"):
                    b.browser.seal()
                b.emit("system", f"Now browsing as “{label}”: your logins, cookies and extensions from that Chrome profile.")
            except Exception as e:  # noqa: BLE001
                b.emit("system", f"Copying the Chrome profile failed: {e}")
    threading.Thread(target=go, daemon=True, name=f"import-{b.id}").start()
