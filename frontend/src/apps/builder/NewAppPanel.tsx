// The structured "New app" creation panel (name + prompt), plus the helpers
// shared with Home's prompt-first hero composer. Lived in views/Home.tsx
// historically; it belongs to the app-builder experience and opens from /apps.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { createApp } from "@platform/lib/api";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TextInput, TextArea } from "@platform/ui/field/fields";
import { isMod, MOD_LABEL } from "@platform/lib/platform";
import { useDeferredClose } from "@platform/lib/hooks";
import { PANEL_EXIT_MS } from "@platform/lib/exit-animation";

// Mirror the server's app-name rules client-side so obvious rejects give an
// instant inline hint instead of a roundtrip; the server remains the authority
// (its 400 message shows inline the same way).
export function appNameError(name: string): string | null {
  if (name.includes("/")) return 'Name cannot contain "/".';
  if (name.includes("\\")) return 'Name cannot contain "\\".';
  if (name.startsWith(".")) return 'Name cannot start with ".".';
  return null;
}

// URL of an app folder's claude_split chat, attached to a specific live run.
// `_mode` is the shell's template selector; `run` is a plain view param the
// claude_split template reads through fused.params (its boot resumes that
// run, so a session started server-side is picked up exactly like one the
// page started itself). Folder-scoped on purpose: the server starts the
// scaffolding session via the claude_split agent on the app FOLDER, so the
// re-attach must land in the same template — same runs dir, same
// .claude-split.json sidecar — never the file-scoped claude template.
export function claudeChatUrl(appDir: string, runId: string): string {
  const params = new URLSearchParams({ _mode: "claude_split", run: runId });
  return urlForFsPath(appDir, "?" + params.toString());
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
      if (res.run_id) navigateUrl(claudeChatUrl(res.path, res.run_id), { isDir: true });
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
