// Home view — the `/view/_home` sentinel route and the app's launch landing
// ("/" redirects here). Structure, top to bottom:
//   1. Hero card — headline + blurb with the page's two verbs as buttons:
//      "New app" (create modal → POST /api/apps/new) and "Browse files".
//   2. Doorways — three equal cards for the app's entry points: file
//      explorer, apps hub (the Fused workspace dir), templates manager.
//   3. Recent — the 9 most recently updated apps (GET /api/apps), sorted
//      once per fetch so the grid never reorders under interaction; keys
//      are stable paths.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createApp, getApps } from "../lib/api";
import type { AppInfo, Config } from "../lib/api";
import { navigate, navigateUrl, urlForFsPath } from "../lib/router";
import { Modal } from "../components/modal/Modal";
import { ErrorBanner } from "../components/ErrorBanner";
import { TextInput, TextArea } from "../components/field/fields";
import { basename } from "../lib/format";
import { isMod } from "../lib/platform";

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

function NewAppModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

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

  // Cmd/Ctrl+Enter submits from either field (plain Enter in the textarea
  // inserts a newline as usual).
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && isMod(e)) {
      e.preventDefault();
      create();
    }
  };

  return (
    <Modal
      title="New app"
      onClose={onClose}
      busy={busy}
      dirty={trimmedName.length > 0 || prompt.trim().length > 0}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={create} disabled={!canCreate}>
            {busy ? "Creating…" : "Create app"}
          </button>
        </>
      }
    >
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
    </Modal>
  );
}

// Each app tile carries a tinted monogram (the app's initial) whose hue is
// picked deterministically from the shell's file-icon palette — stable per
// name, so a tile never changes colour across visits, and the hues are the
// same family the listing already paints file icons with.
const APP_HUES = [
  "var(--icon-folder)",
  "var(--icon-code)",
  "var(--icon-data)",
  "var(--icon-json)",
  "var(--icon-image)",
  "var(--icon-geo)",
  "var(--icon-db)",
  "var(--icon-media)",
];

function hueFor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return APP_HUES[Math.abs(h) % APP_HUES.length];
}

function AppCard({ app }: { app: AppInfo }) {
  const open = () => {
    // An app with an entry opens straight into its "/" route view; a manifest
    // without one falls back to the folder listing so the card is never dead.
    if (app.entry_html) navigate(app.entry_html, { isDir: false });
    else navigate(app.path, { isDir: true });
  };
  const title = app.title || app.name;
  return (
    <button type="button" className="home-app" onClick={open} title={app.path}>
      <span className="home-app-monogram" aria-hidden="true" style={{ color: hueFor(app.name) }}>
        {title.charAt(0).toUpperCase()}
      </span>
      <span className="home-app-text">
        <span className="home-app-title">{title}</span>
        {/* The folder name only earns a line when the manifest title differs
            from it — "application / application" is noise. */}
        {title !== app.name && <span className="home-app-sub">{app.name}</span>}
      </span>
    </button>
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
  const [creating, setCreating] = useState(false);

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
          <div className="home-hero-actions">
            <button type="button" className="btn btn-primary home-hero-cta" onClick={() => setCreating(true)}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
                <path d="M12 5v14M5 12h14" />
              </svg>
              New app
            </button>
            <button
              type="button"
              className="btn btn-secondary home-hero-cta"
              onClick={() => navigate(config.fused_dir, { isDir: true })}
            >
              Browse files
            </button>
          </div>
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
            desc={`Every folder in ${basename(config.fused_dir)} is a project with its own entry page.`}
            titleAttr={config.fused_dir}
            onClick={() => navigate(config.fused_dir, { isDir: true })}
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
                  onClick={() => navigate(config.fused_dir, { isDir: true })}
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
              No apps yet. Hit “New app” above — it lands in {basename(config.fused_dir)} as a
              folder you own.
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

      {creating && <NewAppModal onClose={() => setCreating(false)} />}
    </div>
  );
}
