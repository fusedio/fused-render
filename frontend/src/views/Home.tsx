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
import { useEffect, useRef, useState } from "react";
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

function AppCard({ app }: { app: AppInfo }) {
  const open = () => {
    // An app with an entry opens straight into its "/" route view; a manifest
    // without one falls back to the folder listing so the card is never dead.
    if (app.entry_html) navigate(app.entry_html, { isDir: false });
    else navigate(app.path, { isDir: true });
  };
  return (
    <button type="button" className="home-card" onClick={open} title={app.path}>
      <span className="home-card-glyph" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="3" />
          <path d="M3 9h18" />
          <circle cx="6.4" cy="6" r="0.5" fill="currentColor" />
          <circle cx="9.4" cy="6" r="0.5" fill="currentColor" />
        </svg>
      </span>
      <span className="home-card-title">{app.title || app.name}</span>
      <span className="home-card-sub">{app.name}</span>
    </button>
  );
}

export default function Home({ config }: { config: Config }) {
  const apps = useLoad(getApps);
  const templates = useLoad<TemplateInventory>(getTemplateInventory);
  const [creating, setCreating] = useState(false);

  return (
    <div className="templates-page home-page">
      <div className="templates-header">
        <h1>Home</h1>
        <p className="templates-subtitle">Your apps, templates, and files in one place.</p>
      </div>

      <section className="home-section">
        <h2 className="home-section-title">Apps</h2>
        {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
        {apps.status === "loading" && <div className="deploy-muted">Loading…</div>}
        {apps.status === "ok" && (
          <div className="home-grid">
            {apps.data.apps.map((app) => (
              <AppCard key={app.path} app={app} />
            ))}
            <button
              type="button"
              className="home-card home-card-new"
              onClick={() => setCreating(true)}
            >
              <span className="home-card-glyph" aria-hidden="true">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M12 5v14M5 12h14" />
                </svg>
              </span>
              <span className="home-card-title">New app</span>
              <span className="home-card-sub">Describe it, Claude builds it</span>
            </button>
          </div>
        )}
      </section>

      <section className="home-section">
        <h2 className="home-section-title">Templates</h2>
        {templates.status === "error" && <ErrorBanner>{templates.message}</ErrorBanner>}
        {templates.status === "loading" && <div className="deploy-muted">Loading…</div>}
        {templates.status === "ok" && (
          <div className="home-grid">
            {templates.data.templates.map((t) => (
              <button
                type="button"
                // name is unique in the inventory (a user template shadowing a
                // core one is emitted once, source="user").
                key={t.name}
                className="home-card"
                title={t.path}
                // The Templates view has no per-template URL selection; land on
                // its Library tab where this template's row lives.
                onClick={() => navigateUrl("/view/_templates?tab=library")}
              >
                <span className="home-card-glyph" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="3" width="7" height="7" rx="1" />
                    <rect x="14" y="3" width="7" height="7" rx="1" />
                    <rect x="3" y="14" width="7" height="7" rx="1" />
                    <rect x="14" y="14" width="7" height="7" rx="1" />
                  </svg>
                </span>
                <span className="home-card-title">{t.name}</span>
                <span className="home-card-sub">
                  {templates.data.sources.find((s) => s.id === t.source)?.label || t.source}
                </span>
              </button>
            ))}
            <button
              type="button"
              className="home-card home-card-new"
              // Creation is a modal local to the Templates view (no URL
              // trigger) — deep-link to its Library tab where the "New
              // template" button lives.
              onClick={() => navigateUrl("/view/_templates?tab=library")}
            >
              <span className="home-card-glyph" aria-hidden="true">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M12 5v14M5 12h14" />
                </svg>
              </span>
              <span className="home-card-title">New template</span>
              <span className="home-card-sub">Custom preview for a file type</span>
            </button>
          </div>
        )}
      </section>

      <section className="home-section">
        <h2 className="home-section-title">Files</h2>
        <button
          type="button"
          className="home-card home-card-wide"
          title={config.fused_dir}
          onClick={() => navigate(config.fused_dir, { isDir: true })}
        >
          <span className="home-card-glyph" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
            </svg>
          </span>
          <span className="home-card-title">File explorer</span>
          <span className="home-card-sub">{basename(config.fused_dir) || config.fused_dir}</span>
        </button>
      </section>

      {creating && <NewAppModal onClose={() => setCreating(false)} />}
    </div>
  );
}
