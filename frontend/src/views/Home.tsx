// Home view — lives at "/" itself (old /view/_home sentinel redirects here)
// and is the app's launch landing. Structure, top to bottom:
//   1. Hero card — headline + blurb + the prompt composer (describe an app,
//      haiku names it, POST /api/apps/new scaffolds it). The structured
//      NewAppPanel is exported from here but opens from /apps.
//   2. Doorways — three equal cards for the app's entry points: file
//      explorer, apps hub (the Fused workspace dir), templates manager.
//   3. Recent — the 9 most recently updated apps (GET /api/apps), sorted
//      once per fetch so the grid never reorders under interaction; keys
//      are stable paths.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { aiComplete, createApp, getApps } from "../lib/api";
import type { Config } from "../lib/api";
import { navigate, navigateUrl, urlForFsPath } from "../lib/router";
import { ErrorBanner } from "../components/ErrorBanner";
import { TextInput, TextArea } from "../components/field/fields";
import { basename } from "../lib/format";
import { isMod, MOD_LABEL } from "../lib/platform";
import { AppCard } from "../components/AppCard";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

function useLoad<T>(fetcher: () => Promise<T>): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
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
  }, []);
  return state;
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
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT);
    const name = kebabName(text.trim().split(/\s+/)[0] ?? "");
    if (name !== "my-app") return name;
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

// Starter ideas under the composer: clicking one fills the box (never
// submits) so the user can edit before building.
const SAMPLE_PROMPTS = [
  "A habit tracker with streaks and a weekly heatmap",
  "A markdown notes app with full-text search",
  "A dashboard that charts my CSV files",
];

// The hero's prompt box — the claude.ai / v0 "what do you want to build?"
// composer. Submitting names the app (haiku via /api/ai), scaffolds it, and
// lands in the new folder's claude chat exactly like the New-app panel does.
function HeroComposer() {
  const [prompt, setPrompt] = useState("");
  const [phase, setPhase] = useState<"idle" | "naming" | "creating">("idle");
  const [error, setError] = useState<string | null>(null);
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
        {SAMPLE_PROMPTS.map((s) => (
          <button
            key={s}
            type="button"
            className="home-composer-sample"
            disabled={busy}
            onClick={() => setPrompt(s)}
          >
            {s}
          </button>
        ))}
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
// scrim click, Esc, or ✕, all gated by `busy`.
export function NewAppPanel({ onClose }: { onClose: () => void }) {
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
      // The folder exists either way, but a prompt that never reached Claude
      // must not look like success: navigating straight to a boilerplate view
      // is exactly how "it sent nothing to Claude" reads as working. Stay put
      // and say why; the app is listed on Home once the modal closes.
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
      if (e.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, [busy, onClose]);

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
      className={"app-panel-overlay" + (open ? " is-open" : "")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
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
            onClick={onClose}
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
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
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
  count,
  action,
}: {
  label: string;
  count?: number;
  action?: ReactNode;
}) {
  return (
    <div className="home-rule">
      <span className="home-rule-label">{label}</span>
      {typeof count === "number" && <span className="home-rule-count">{count}</span>}
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

export default function Home({ config }: { config: Config }) {
  const apps = useLoad(getApps);

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
            Build your next
            <br />
            <span className="home-hero-accent">local app</span>
          </h1>
          <p className="home-hero-sub">
            Describe an app and Claude builds it in your workspace — or explore your files
            with interactive templates. Everything lives as plain folders you own.
          </p>
          {/* The hero's only verb, prompt-first: describe the app right here
              and a named, scaffolded folder + claude session comes back. The
              structured (name-it-yourself) NewAppPanel lives on /apps now,
              and file browsing has its doorway card below. */}
          <HeroComposer />
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
        </div>

        <section className="home-section">
          <SectionRule
            label="recent"
            count={apps.status === "ok" ? apps.data.apps.length : undefined}
            action={
              apps.status === "ok" && apps.data.apps.length > 9 ? (
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
          {apps.status === "loading" && <div className="home-loading">Loading…</div>}
          {apps.status === "ok" && apps.data.apps.length === 0 && (
            <div className="home-empty">
              No apps yet. Describe one in the box above — it lands in{" "}
              {basename(config.fused_dir)}/local as a folder you own.
            </div>
          )}
          {apps.status === "ok" && apps.data.apps.length > 0 && (
            <div className="home-apps">
              {/* The 9 most recently updated apps. Sort is computed once per
                  fetch: recency (updated_at epoch seconds, missing → last;
                  name breaks ties) — stable under interaction since nothing
                  re-sorts after load. */}
              {apps.data.apps
                .slice()
                .sort(
                  (a, b) =>
                    (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.name.localeCompare(b.name),
                )
                .slice(0, 9)
                .map((app) => (
                  <AppCard key={app.path} app={app} />
                ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
