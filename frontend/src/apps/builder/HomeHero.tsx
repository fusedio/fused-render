// The Home hero — wordmark, headline, and the prompt-first composer that
// names (haiku via /api/ai), scaffolds (POST /api/apps/new), and lands in the
// new app's claude chat. Shared by Home ("/") and the /apps hub, which is why
// it lives in the builder app rather than the shell.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { aiComplete, createApp } from "@platform/lib/api";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { TextArea } from "@platform/ui/field/fields";

// URL of an app folder's claude chat, attached to a specific live run.
// `_mode` is the shell's template selector; `run` is a plain view param the
// claude template reads through fused.params (its boot resumes that
// run, so a session started server-side is picked up exactly like one the
// page started itself). Folder-scoped on purpose: the server starts the
// scaffolding session via the claude agent on the app FOLDER, so the
// re-attach must land in the same template — same runs dir, same
// .claude-split.json sidecar. (There is only one chat template now, so "which
// chat" is no longer a question at all; the `_mode` still has to be spelled out
// because the folder's default mode is the app itself, not the chat.)
// An ORDINARY explorer URL for the folder. It used to be the builder route
// (/apps/<tag>/<name>, rebuilt from the folder's last two path segments); that
// namespace is gone, and urlForFsPath takes the whole abspath the server
// returned — including its Windows-backslash normalization, which the old
// segment split had to do by hand or silently take the drive-rooted path as one
// segment.
export function claudeChatUrl(appDir: string, runId: string): string {
  const params = new URLSearchParams({ _mode: "claude", run: runId });
  return urlForFsPath(appDir.replace(/\/+$/, ""), "?" + params.toString());
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
      if (res.run_id) navigateUrl(claudeChatUrl(res.path, res.run_id), { isDir: true });
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

// The hero card itself — wordmark, headline, and the prompt composer.
// Exported so /apps can show the exact same hero above its grid; `onCreated`
// lets each page refresh its own app list once the folder exists.
export function HomeHero({ onCreated }: { onCreated: () => void }) {
  return (
    <header className="home-hero">
      {/* Fused mark + headline. The "App" wordmark that used to sit between
          them is gone — the sidebar entry that got you here already names the
          page — so the mark stands alone beside the tagline. Both theme
          renders are in the DOM; CSS shows the one matching data-theme. */}
      <h1 className="home-hero-brand">
        <img className="home-hero-logo home-hero-logo-dark" src={logoMarkDark} alt="" aria-hidden="true" />
        <img className="home-hero-logo home-hero-logo-light" src={logoMarkLight} alt="" aria-hidden="true" />
        <span className="home-hero-tagline">
          Build your next <span className="home-hero-accent">local app</span>
        </span>
      </h1>
      {/* The hero's only verb, prompt-first: describe the app right here
          and a named, scaffolded folder + claude session comes back. */}
      <HeroComposer onCreated={onCreated} />
    </header>
  );
}
