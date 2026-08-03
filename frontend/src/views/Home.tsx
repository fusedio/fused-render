// Home view — lives at "/" itself (old /view/_home sentinel redirects here)
// and is the app's launch landing. Structure, top to bottom:
//   1. Hero card — headline + blurb + the prompt composer (describe an app,
//      haiku names it, POST /api/apps/new scaffolds it). The structured
//      NewAppPanel is exported from here but opens from /apps.
//   2. Doorways — equal cards for the app's entry points: file explorer,
//      apps hub (the Fused workspace dir), templates manager, and — once
//      the bundled learn mount is ready — the Learn lessons.
//   3. Recent — the 10 most recently updated apps (GET /api/apps), sorted
//      once per fetch so the grid never reorders under interaction; keys
//      are stable paths.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { aiComplete, createApp, getApps, statPath } from "../lib/api";
import type { AppInfo, Config } from "../lib/api";
import { currentUrl, navigate, navigateUrl, urlForFsPath } from "../lib/router";
import { ErrorBanner } from "../components/ErrorBanner";
import { TextInput, TextArea } from "../components/field/fields";
import { basename } from "../lib/format";
import { isMod, MOD_LABEL } from "../lib/platform";
import { useDeferredClose, useLearnMountReady } from "../lib/hooks";
import { PANEL_EXIT_MS } from "../lib/exit-animation";
import { timeAgo } from "../components/AppPreviewCard";
import { SkeletonLines } from "../components/Skeleton";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

// Returns the load state plus a reload: bumping the nonce refetches while the
// previous data stays on screen, so a refresh never flashes the loading state.
function useLoad<T>(fetcher: () => Promise<T>): [Loaded<T>, () => void] {
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    fetcher().then(
      (data) => alive && setState({ status: "ok", data }),
      (e: Error) => alive && setState({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce]);
  return [state, () => setNonce((n) => n + 1)];
}

// Mirror the server's app-name rules client-side so obvious rejects give an
// instant inline hint instead of a roundtrip; the server remains the authority
// (its 400 message shows inline the same way).
function appNameError(name: string): string | null {
  if (name.includes("/")) return 'Name cannot contain "/".';
  if (name.includes("\\")) return 'Name cannot contain "\\".';
  if (name.startsWith(".")) return 'Name cannot start with ".".';
  return null;
}

// URL of a file's claude-template chat, attached to a specific live run.
// `_mode` is the shell's template selector; `run` is a plain view param the
// claude template reads through fused.params (its boot resumes that run, so
// a session started server-side is picked up exactly like one the page
// started itself).
export function claudeChatUrl(fsPath: string, runId: string): string {
  const params = new URLSearchParams({ _mode: "claude", run: runId });
  return urlForFsPath(fsPath, "?" + params.toString());
}

// -- Prompt-first creation (the hero composer) --------------------------------

// Kebab-case whatever the model (or, as a fallback, the user's own prompt)
// gave us into a safe app folder name: lowercase, [a-z0-9-] only, at most
// five words. Never returns something _app_name_error would reject.
function kebabName(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .split("-")
      .filter(Boolean)
      .slice(0, 5)
      .join("-")
      .slice(0, 48) || "my-app"
  );
}

const NAME_SYSTEM_PROMPT =
  "You name software projects. Given a description of an app, reply with a " +
  "short kebab-case name for it: 2-4 lowercase words joined by hyphens, " +
  "letters and digits only. Reply with ONLY the name — no quotes, no prose.";

// A kebab-case folder name for an app described by `prompt`: ask the AI relay
// (haiku, the server default — cheap and fast), fall back to slugging the
// prompt's own words when the relay is unavailable or answers garbage.
async function suggestAppName(prompt: string): Promise<string> {
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT).then((t) => t.trim());
    // NAME_SYSTEM_PROMPT asks for a bare kebab-case reply; if the model added
    // prose instead (common even with "no prose" in the ask), `text` won't be
    // pure lowercase-and-hyphens. Taking the first token then would name the
    // folder after whatever word led the prose (e.g. "sure") instead of
    // falling through to the actual prompt slug below.
    if (/^[a-z0-9]+(-[a-z0-9]+)*$/.test(text)) {
      const name = kebabName(text);
      if (name !== "my-app") return name;
    }
  } catch {
    // relay down / claude missing: the slug fallback below still works
  }
  return kebabName(prompt);
}

// Create the app under a collision-proof name: on 409 retry with -2, -3, …
// Any other failure propagates.
async function createAppUnderFreeName(name: string, prompt: string) {
  for (let i = 1; ; i++) {
    const attempt = i === 1 ? name : `${name}-${i}`;
    try {
      return await createApp(attempt, prompt);
    } catch (e) {
      if ((e as { status?: number }).status !== 409 || i >= 20) throw e;
    }
  }
}

// Starter ideas under the composer (v0-style): an icon + short label on the
// chip, and the verbose brief that actually lands in the box on click (never
// submits) — detailed enough that Claude builds the right thing first pass.
// A shuffle button cycles through the pool three at a time.
const SAMPLE_PROMPTS: { label: string; prompt: string; glyph: ReactNode }[] = [
  {
    label: "Habit tracker",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20 6L9 17l-5-5" />
      </svg>
    ),
    prompt:
      "A habit tracker. Let me define habits with a name and a target cadence " +
      "(daily or specific weekdays), check them off for today, and edit or delete them. " +
      "Show the current streak per habit and a weekly heatmap of completions. " +
      "Persist everything locally so my history survives restarts.",
  },
  {
    label: "Markdown notes",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    ),
    prompt:
      "A markdown notes app. A sidebar lists my notes sorted by last edited; " +
      "I can create, rename, and delete notes, and edit them with a live markdown preview. " +
      "Include full-text search across all notes with matching snippets highlighted. " +
      "Store notes as plain .md files in the app folder so they stay portable.",
  },
  {
    label: "CSV dashboard",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3 3v18h18M8 17V9M13 17V5M18 17v-6" />
      </svg>
    ),
    prompt:
      "A CSV dashboard. Let me drop or pick a CSV file, then show a sortable, filterable " +
      "table of its rows plus summary stats per numeric column (min, max, mean, nulls). " +
      "Let me pick columns to chart as a bar, line, or scatter plot. " +
      "Handle large-ish files gracefully and remember the last file I opened.",
  },
  {
    label: "Mini game",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M6 12h4M8 10v4M15 11h.01M18 13h.01M17.3 5H6.7a4.7 4.7 0 0 0-4.6 5.6l1 5A3 3 0 0 0 8 17.4l.6-1.4h6.8l.6 1.4a3 3 0 0 0 4.9-1.8l1-5A4.7 4.7 0 0 0 17.3 5z" />
      </svg>
    ),
    prompt:
      "A 2048-style sliding tile game. Arrow keys (and touch swipes) slide and merge " +
      "tiles on a 4x4 grid, with smooth animations and a score counter. " +
      "Detect game over and win states with a restart button, " +
      "and keep the best score locally so it survives restarts.",
  },
  {
    label: "Finance calculator",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
      </svg>
    ),
    prompt:
      "A compound-interest and loan calculator. Inputs for principal, rate, term, and " +
      "monthly contribution or payment; show the resulting balance or amortization " +
      "schedule as both a table and a line chart. " +
      "Update results live as inputs change and format all amounts as currency.",
  },
  {
    label: "Pomodoro timer",
    glyph: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="13" r="8" />
        <path d="M12 9v4l2.5 2.5M9 2h6" />
      </svg>
    ),
    prompt:
      "A pomodoro focus timer. Configurable work/short-break/long-break durations, " +
      "a large countdown with start/pause/reset, and an automatic cycle through " +
      "sessions with a chime between them. " +
      "Log completed pomodoros per day and show a simple daily history.",
  },
];

// The hero's prompt box — the claude.ai / v0 "what do you want to build?"
// composer. Submitting names the app (haiku via /api/ai), scaffolds it, and
// lands in the new folder's claude chat exactly like the New-app panel does.
function HeroComposer({ onCreated }: { onCreated: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [phase, setPhase] = useState<"idle" | "naming" | "creating">("idle");
  const [error, setError] = useState<string | null>(null);
  // Which window of three starter chips is showing; shuffle advances it.
  const [sampleOffset, setSampleOffset] = useState(0);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const busy = phase !== "idle";
  const canSubmit = prompt.trim().length > 0 && !busy;

  const submit = async () => {
    if (!canSubmit) return;
    const trimmed = prompt.trim();
    setError(null);
    setPhase("naming");
    try {
      const name = await suggestAppName(trimmed);
      if (!alive.current) return;
      setPhase("creating");
      const res = await createAppUnderFreeName(name, trimmed);
      // The folder exists from here on, so the Recent grid is stale — refresh it
      // now, since the session-error branch below stays on this page.
      onCreated();
      // Same landing logic as NewAppPanel: a session error must not read as
      // success, and a live run means the claude chat is the right landing.
      if (res.session_error) {
        if (alive.current) {
          setError(`App created, but Claude didn't start: ${res.session_error}`);
          setPhase("idle");
        }
        return;
      }
      if (res.run_id) navigateUrl(claudeChatUrl(res.entry_html, res.run_id));
      else navigate(res.entry_html, { isDir: false });
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setPhase("idle");
      }
    }
  };

  return (
    <div className="home-composer-wrap">
      <div className={"home-composer" + (busy ? " is-busy" : "")}>
        <TextArea
          className="home-composer-input"
          placeholder="What do you want to build?"
          aria-label="What do you want to build?"
          value={prompt}
          rows={3}
          disabled={busy}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            // Enter submits (the composer is a one-shot prompt, not a
            // document); Shift+Enter keeps the newline for longer briefs.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="home-composer-bar">
          <span className="home-composer-hint">
            {phase === "naming" && "Naming your app…"}
            {phase === "creating" && "Creating the app…"}
            {phase === "idle" && (
              <>
                <kbd>↵</kbd> to build · <kbd>⇧↵</kbd> for a new line
              </>
            )}
          </span>
          <button
            type="button"
            className="home-composer-send"
            aria-label="Build it"
            title="Build it"
            disabled={!canSubmit}
            onClick={submit}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
      </div>
      <div className="home-composer-samples">
        {[0, 1, 2].map((i) => {
          const s = SAMPLE_PROMPTS[(sampleOffset + i) % SAMPLE_PROMPTS.length];
          return (
            <button
              key={s.label}
              type="button"
              className="home-composer-sample"
              title={s.prompt}
              disabled={busy}
              onClick={() => setPrompt(s.prompt)}
            >
              <span className="home-composer-sample-glyph" aria-hidden="true">
                {s.glyph}
              </span>
              {s.label}
            </button>
          );
        })}
        <button
          type="button"
          className="home-composer-sample home-composer-shuffle"
          aria-label="More ideas"
          title="More ideas"
          disabled={busy}
          onClick={() => setSampleOffset((o) => (o + 3) % SAMPLE_PROMPTS.length)}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v5h-5" />
          </svg>
        </button>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// The three explainer cards above the form: what a fused app is, in one glance.
// Pure CSS visuals (inline SVG glyph tinted from the file-icon palette, same
// grammar as the Doorway cards on the page behind the panel).
const APP_FACTS: { hue: string; title: string; desc: string; glyph: ReactNode }[] = [
  {
    hue: "var(--icon-html)",
    title: "Describe it",
    desc: "Write a prompt; Claude builds the app in its own folder.",
    glyph: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3l2.2 5.3L20 10l-5.8 1.7L12 17l-2.2-5.3L4 10l5.8-1.7z" />
      </svg>
    ),
  },
  {
    hue: "var(--icon-code)",
    title: "Python-powered",
    desc: "Views are HTML rendered by python — live data, no build step.",
    glyph: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M8 6l-5 6 5 6M16 6l5 6-5 6" />
      </svg>
    ),
  },
  {
    hue: "var(--icon-folder)",
    title: "AI powered by design",
    desc: "Your HTML can access inference locally from your Claude Code subscription.",
    glyph: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3zM19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9L19 16z" />
      </svg>
    ),
  },
];

// The "how it works" timeline under the fact cards: the path from prompt to
// running app, numbered. Pure presentation — nothing here is interactive.
const APP_STEPS: { title: string; desc: string }[] = [
  {
    title: "Start a project",
    desc: "Give it a name and describe what you want it to do.",
  },
  {
    title: "Claude Code builds it",
    desc: "A session starts in the app's folder — you stay in control of what gets built.",
  },
  {
    title: "Watch it take shape",
    desc: "Switch the mode to View to see the app render right there as it's built.",
  },
  {
    title: "Ready to use",
    desc: "Your app lives on Home — open it any time, keep iterating whenever you like.",
  },
];

// "New app" opens as a Notion-style slide-over: a full-height panel pinned to
// the left edge (60vw, full width under 800px) over a dim scrim. Deliberately
// NOT the shared Modal chassis — that centres a width-clamped dialog, which
// fights an edge-anchored panel. Close behaviour matches the modal it replaces:
// scrim click, Esc, or ✕, all gated by `busy`. `onCreated` fires once the folder
// exists (session error or not) so the caller's list can refresh underneath —
// it never closes the panel, whose own success/error state still has to be read.
export function NewAppPanel({ onClose, onCreated }: { onClose: () => void; onCreated?: () => void }) {
  // Slide OUT as well as in. The caller unmounts this panel, so closing is
  // deferred by the slide duration and `is-open` comes off immediately — the
  // same CSS that animated the entry runs backwards (lib/exit-animation).
  const { closing, requestClose } = useDeferredClose(onClose, PANEL_EXIT_MS);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Mounts off-screen, then flips to `is-open` on the next frame so the CSS
  // translate actually animates (a class present on first paint does not).
  const [open, setOpen] = useState(false);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  useEffect(() => {
    const id = requestAnimationFrame(() => setOpen(true));
    return () => cancelAnimationFrame(id);
  }, []);

  const trimmedName = name.trim();
  const nameError = appNameError(trimmedName);
  const canCreate = trimmedName.length > 0 && !nameError && !busy;

  const create = async () => {
    if (!canCreate) return;
    setBusy(true);
    setError(null);
    try {
      const res = await createApp(trimmedName, prompt.trim());
      onCreated?.();
      // The folder exists either way, but a prompt that never reached Claude
      // must not look like success: navigating straight to a boilerplate view
      // is exactly how "it sent nothing to Claude" reads as working. Stay put
      // and say why; `onCreated` above already listed it behind the panel.
      if (res.session_error) {
        if (alive.current) {
          setError(`App created, but Claude didn't start: ${res.session_error}`);
          setBusy(false);
        }
        return;
      }
      // A session is running: land IN the entry file's claude chat rather than
      // on its (still-boilerplate) rendered view, so the user watches the work
      // they just asked for instead of the page it hasn't produced yet.
      // `?_mode=claude` selects the claude template for this file (SPEC PT-9)
      // and `run` is the template's own re-attach param — its boot reads it,
      // enters chat, and streams the live run (agent.py `poll`), replaying the
      // prompt as the user turn. Without a prompt there is no session, so the
      // default (rendered) view is the right landing.
      if (res.run_id) navigateUrl(claudeChatUrl(res.entry_html, res.run_id));
      else navigate(res.entry_html, { isDir: false });
    } catch (e) {
      // 409 (collision) and 400 (bad name) both carry the server's message.
      if (alive.current) {
        setError((e as Error).message);
        setBusy(false);
      }
    }
  };

  // Esc closes (document-level, so it works wherever focus sits), gated by busy
  // exactly like the modal chassis this panel replaced.
  useEffect(() => {
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) requestClose();
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, [busy, requestClose]);

  // Cmd/Ctrl+Enter submits from either field (plain Enter in the textarea
  // inserts a newline as usual).
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && isMod(e)) {
      e.preventDefault();
      create();
    }
  };

  return createPortal(
    <div
      className={"app-panel-overlay" + (open && !closing ? " is-open" : "")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) requestClose();
      }}
    >
      <div
        className="app-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-app-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="app-panel-head">
          <div className="app-panel-head-text">
            <span className="app-panel-eyebrow">Fused</span>
            <h2 id="new-app-title">Create a new app</h2>
          </div>
          <button
            type="button"
            className="app-panel-close"
            aria-label="Close"
            title="Close"
            disabled={busy}
            onClick={requestClose}
          >
            ✕
          </button>
        </div>

        <div className="app-panel-body">
          {/* Explainer: what a fused app is, before the two fields that make one. */}
          <p className="app-panel-lede">
            A fused app is a folder in your workspace rendered as python-powered HTML pages —
            describe it here and Claude scaffolds it for you.
          </p>
          <div className="app-panel-facts">
            {APP_FACTS.map((f) => (
              <div className="app-panel-fact" key={f.title}>
                <span className="app-panel-fact-glyph" aria-hidden="true" style={{ color: f.hue }}>
                  {f.glyph}
                </span>
                <span className="app-panel-fact-title">{f.title}</span>
                <span className="app-panel-fact-desc">{f.desc}</span>
              </div>
            ))}
          </div>

          {/* The prompt-to-app path, numbered — presentation only. */}
          <div className="app-panel-section">
            <h3 className="app-panel-section-title">How it works</h3>
            <ol className="app-panel-steps">
              {APP_STEPS.map((s, i) => (
                <li className="app-panel-step" key={s.title}>
                  <span className="app-panel-step-num" aria-hidden="true">
                    {i + 1}
                  </span>
                  <span className="app-panel-step-text">
                    <span className="app-panel-step-title">{s.title}</span>
                    <span className="app-panel-step-desc">{s.desc}</span>
                  </span>
                </li>
              ))}
            </ol>
          </div>

          <div className="app-panel-section">
            <h3 className="app-panel-section-title">Your app</h3>
            <div className="app-panel-form">
            <div className="templates-field">
              <label htmlFor="new-app-name">Name</label>
              <TextInput
                id="new-app-name"
                type="text"
                placeholder="my-app"
                value={name}
                autoFocus
                disabled={busy}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={onKey}
              />
              {nameError && <div className="templates-key-error">{nameError}</div>}
            </div>
            <div className="templates-field">
              <label htmlFor="new-app-prompt">What should this app do?</label>
              <TextArea
                id="new-app-prompt"
                placeholder="Describe the app — a Claude session starts in its folder with this prompt."
                value={prompt}
                rows={5}
                disabled={busy}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={onKey}
              />
            </div>
              {error && <ErrorBanner>{error}</ErrorBanner>}
            </div>
          </div>
        </div>

        <div className="app-panel-foot">
          <span className="app-panel-hint">
            <kbd>{MOD_LABEL}</kbd> + <kbd>↵</kbd> to create
          </span>
          <button type="button" className="btn btn-secondary" onClick={requestClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={create} disabled={!canCreate}>
            {busy ? "Creating…" : "Create app"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// Section heading: mono eyebrow + count, hairline rule filling the middle,
// optional trailing action link. The mono face is the shell's existing code
// voice (listings, paths), so the labels read as part of the tool, not chrome.
function SectionRule({
  label,
  action,
}: {
  label: string;
  action?: ReactNode;
}) {
  return (
    <div className="home-rule">
      <span className="home-rule-label">{label}</span>
      <span className="home-rule-line" aria-hidden="true" />
      {action}
    </div>
  );
}

// One doorway card per top-level entry point. The glyph square borrows the
// listing's file-icon hues (set inline as `color`; fill/border derive from it
// via color-mix) so the three cards are distinguishable at a glance without
// inventing new palette.
function Doorway({
  hue,
  title,
  desc,
  onClick,
  glyph,
  titleAttr,
}: {
  hue: string;
  title: string;
  desc: string;
  onClick: () => void;
  glyph: ReactNode;
  titleAttr?: string;
}) {
  return (
    <button type="button" className="home-door" onClick={onClick} title={titleAttr}>
      <span className="home-door-glyph" aria-hidden="true" style={{ color: hue }}>
        {glyph}
      </span>
      <span className="home-door-title">{title}</span>
      <span className="home-door-desc">{desc}</span>
      <span className="home-door-arrow" aria-hidden="true">
        →
      </span>
    </button>
  );
}

// One row in the Recent list: name, tag, last-used — no icon. Same open
// behavior as the /apps cards: entry HTML if present, else the folder.
function RecentRow({ app }: { app: AppInfo }) {
  const open = () => {
    if (app.entry_html) navigate(app.entry_html, { isDir: false });
    else navigate(app.path, { isDir: true });
  };
  const title = app.title || app.name;
  return (
    <button type="button" className="home-recent" onClick={open} title={app.path}>
      <span className="home-recent-name">{title}</span>
      <span className="home-app-tag">{app.tag}</span>
      <span className="home-recent-when">{timeAgo(app.updated_at) ?? "—"}</span>
    </button>
  );
}

export default function Home({ config }: { config: Config }) {
  const [apps, reloadApps] = useLoad(getApps);
  // The boot-time config snapshot's learn_mount_ready is stale in both
  // directions (see useLearnMountReady) — without the bounded re-poll the
  // Learn doorway would essentially never appear.
  const learnMountReady = useLearnMountReady(config.learn_mount_ready);

  // Same landing logic as the Sidebar's Learn entry (D123): the bundled
  // learn.zip is mounted read-only at `${mounts_root}/learn`; prefer its
  // index.html as the landing page, fall back to the mount folder.
  const openLearn = async () => {
    if (!config.mounts_root) return;
    const root = `${config.mounts_root.replace(/\/+$/, "")}/learn`;
    // The stat can be slow (mount-backed read); if the user navigated
    // elsewhere while it was in flight, don't yank them back.
    const before = currentUrl();
    let dest = root;
    let destIsDir = true;
    try {
      const st = await statPath(`${root}/index.html`);
      if (!st.is_dir) {
        dest = `${root}/index.html`;
        destIsDir = false;
      }
    } catch {
      // stat 404s (or the mount is briefly not attached) — open the folder.
    }
    if (currentUrl() === before) navigate(dest, { isDir: destIsDir });
  };

  return (
    <div className="home-page">
      <div className="home-inner">
        {/* Hero card: the product's pitch plus its two verbs. "New app" is the
            page's primary action (opens the create modal — the actual fields
            live there); "Browse files" is the everyday doorway. */}
        <header className="home-hero">
          <div className="home-hero-badge">
            <span className="home-hero-dot" aria-hidden="true" />
            fused-render
          </div>
          <h1 className="home-hero-title">
            Build your next <span className="home-hero-accent">local app</span>
          </h1>
          {/* The hero's only verb, prompt-first: describe the app right here
              and a named, scaffolded folder + claude session comes back. The
              structured (name-it-yourself) NewAppPanel lives on /apps now,
              and file browsing has its doorway card below. */}
          <HeroComposer onCreated={reloadApps} />
        </header>

        {/* Doorways: one card per entry point. */}
        <div className="home-doors">
          <Doorway
            hue="var(--icon-folder)"
            title="File explorer"
            desc="Navigate your workspace and open any file with its template."
            titleAttr={config.fused_dir}
            onClick={() => navigate(config.fused_dir, { isDir: true })}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
            }
          />
          <Doorway
            hue="var(--icon-code)"
            title="Apps"
            desc={`Every folder inside a tag folder in ${basename(config.fused_dir)} is a project with its own entry page.`}
            titleAttr={config.fused_dir}
            onClick={() => navigateUrl("/apps")}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="7" height="7" rx="1.5" />
                <rect x="14" y="3" width="7" height="7" rx="1.5" />
                <rect x="3" y="14" width="7" height="7" rx="1.5" />
                <rect x="14" y="14" width="7" height="7" rx="1.5" />
              </svg>
            }
          />
          <Doorway
            hue="var(--icon-json)"
            title="Templates"
            desc="Build a custom interactive view for any file extension."
            onClick={() => navigateUrl("/view/_templates")}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M8 6l-5 6 5 6M16 6l5 6-5 6" />
              </svg>
            }
          />
          {learnMountReady && (
            <Doorway
              hue="var(--icon-data)"
              title="Learn"
              desc="Guided lessons that teach fused-render by example, right in the app."
              onClick={openLearn}
              glyph={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 7v13" />
                  <path d="M3 6a2 2 0 0 1 2-2h4a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H3Z" />
                  <path d="M21 6a2 2 0 0 0-2-2h-4a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H21Z" />
                </svg>
              }
            />
          )}
        </div>

        <section className="home-section">
          <SectionRule
            label="recent"
            action={
              apps.status === "ok" && apps.data.apps.length > 5 ? (
                <button
                  type="button"
                  className="home-rule-action"
                  onClick={() => navigateUrl("/apps")}
                >
                  View all →
                </button>
              ) : undefined
            }
          />
          {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
          {apps.status === "loading" && <SkeletonLines rows={3} label="Loading apps" />}
          {apps.status === "ok" && apps.data.apps.length === 0 && (
            <div className="home-empty">
              No apps yet. Describe one in the box above — it lands in{" "}
              {basename(config.fused_dir)}/local as a folder you own.
            </div>
          )}
          {apps.status === "ok" && apps.data.apps.length > 0 && (
            <div className="home-recents">
              {/* The 5 most recently updated apps. Sort is computed once per
                  fetch: recency (updated_at epoch seconds, missing → last;
                  name breaks ties) — stable under interaction since nothing
                  re-sorts after load. */}
              {apps.data.apps
                .slice()
                .sort(
                  (a, b) =>
                    (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.name.localeCompare(b.name),
                )
                .slice(0, 5)
                .map((app) => (
                  <RecentRow key={app.path} app={app} />
                ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
