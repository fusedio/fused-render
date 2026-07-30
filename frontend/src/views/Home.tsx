// Home view — the `/view/_home` sentinel route and the app's launch landing
// ("/" redirects here). Three sections on one page:
//   1. Apps — fused_app folders (GET /api/apps) as a card grid, plus a
//      "New app" card that opens the create modal (POST /api/apps/new).
//   2. Templates — the resolved template pool (GET /api/templates/inventory),
//      each card deep-linking into the Templates view's Library tab.
//   3. Files — one card into the Fused workspace dir (the file explorer).
// Card lists render in the order the server returns (apps: sorted by name;
// templates: inventory order) and never reorder under interaction — keys are
// stable paths/names, and the "New …" card sits in a fixed slot at the end.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createApp, getApps, getTemplateInventory } from "../lib/api";
import type { AppInfo, Config, TemplateInventory } from "../lib/api";
import { navigate, navigateUrl } from "../lib/router";
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
      // Land on the new app's entry view. If a Claude session was started it
      // is visible there via the file's claude template — no extra screen.
      navigate(res.entry_html, { isDir: false });
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

export default function Home({ config }: { config: Config }) {
  const apps = useLoad(getApps);
  const templates = useLoad<TemplateInventory>(getTemplateInventory);
  const [creating, setCreating] = useState(false);

  return (
    <div className="home-page">
      <div className="home-inner">
        {/* Hero: the product's one magic verb — describe an app, Claude builds
            it. A faux prompt bar (button, not a real input: the modal owns the
            actual fields) with a blinking accent caret; the page's single
            accent moment. */}
        <header className="home-hero">
          <div className="home-hero-eyebrow">fused-render</div>
          <h1 className="home-hero-title">What do you want to build?</h1>
          <button type="button" className="home-prompt" onClick={() => setCreating(true)}>
            <span className="home-prompt-caret" aria-hidden="true" />
            <span className="home-prompt-hint">Describe an app — Claude builds it in your workspace</span>
            <kbd className="home-prompt-key" aria-hidden="true">new app</kbd>
          </button>
        </header>

        <section className="home-section">
          <SectionRule
            label="apps"
            count={apps.status === "ok" ? apps.data.apps.length : undefined}
          />
          {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
          {apps.status === "loading" && <div className="home-loading">Loading…</div>}
          {apps.status === "ok" && apps.data.apps.length === 0 && (
            <div className="home-empty">
              No apps yet. Describe one above — it lands in {basename(config.fused_dir)} as a
              folder you own.
            </div>
          )}
          {apps.status === "ok" && apps.data.apps.length > 0 && (
            <div className="home-apps">
              {/* "New app" holds the FIRST slot (owner call — creation is the
                  page's primary action), then the 9 most recently updated
                  apps. Sort is computed once per fetch: recency (updated_at
                  epoch seconds, missing → last; name breaks ties) — stable
                  under interaction since nothing re-sorts after load. */}
              <button type="button" className="home-app home-app-new" onClick={() => setCreating(true)}>
                <span className="home-app-monogram" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <path d="M12 5v14M5 12h14" />
                  </svg>
                </span>
                <span className="home-app-text">
                  <span className="home-app-title">New app</span>
                  <span className="home-app-sub">describe it, Claude builds it</span>
                </span>
              </button>
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
          {apps.status === "ok" && apps.data.apps.length > 9 && (
            <button
              type="button"
              className="home-rule-action home-apps-all"
              onClick={() => navigate(config.fused_dir, { isDir: true })}
            >
              All apps →
            </button>
          )}
        </section>

        <section className="home-section">
          <SectionRule
            label="templates"
            count={templates.status === "ok" ? templates.data.templates.length : undefined}
            action={
              <button
                type="button"
                className="home-rule-action"
                onClick={() => navigateUrl("/view/_templates")}
              >
                Manage →
              </button>
            }
          />
          {templates.status === "error" && <ErrorBanner>{templates.message}</ErrorBanner>}
          {templates.status === "loading" && <div className="home-loading">Loading…</div>}
          {templates.status === "ok" && (
            // Every chip lands on the Templates view's Library tab (it has no
            // per-template URL selection) — so these are a quiet rail, not
            // cards pretending to be individual destinations.
            <div className="home-chips">
              {templates.data.templates.map((t) => (
                <button
                  type="button"
                  // name is unique in the inventory (a user template shadowing
                  // a core one is emitted once, source="user").
                  key={t.name}
                  className={"home-chip" + (t.source !== "core" ? " home-chip-user" : "")}
                  title={t.path}
                  onClick={() => navigateUrl("/view/_templates?tab=library")}
                >
                  {t.name}
                </button>
              ))}
              <button
                type="button"
                className="home-chip home-chip-new"
                // Creation is a modal local to the Templates view (no URL
                // trigger) — deep-link to its Library tab where the "New
                // template" button lives.
                onClick={() => navigateUrl("/view/_templates?tab=library")}
              >
                + New template
              </button>
            </div>
          )}
        </section>

        <section className="home-section">
          <SectionRule label="files" />
          <button
            type="button"
            className="home-files"
            title={config.fused_dir}
            onClick={() => navigate(config.fused_dir, { isDir: true })}
          >
            <span className="home-files-glyph" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
            </span>
            <span className="home-files-text">
              <span className="home-files-title">Browse your workspace</span>
              <span className="home-files-path">{config.fused_dir.replace(config.home, "~")}</span>
            </span>
            <span className="home-files-arrow" aria-hidden="true">→</span>
          </button>
        </section>
      </div>

      {creating && <NewAppModal onClose={() => setCreating(false)} />}
    </div>
  );
}
