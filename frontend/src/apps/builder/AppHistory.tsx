// The history panel — a git-log-like view over what Claude Code knows about
// one file, opened from an app card's History button (/apps hub).
//
// Two linked halves, both read-only, both from GET /api/claude/related:
//
//   * CHECKPOINTS — the file-history versions of the card's entry file.
//     Version numbers are per-session and restart across sessions, so rows
//     are ordered by time (the server already does) and each names its
//     session; the row whose content matches the disk right now is marked.
//   * SESSIONS — every consulted transcript that touched the file. A session
//     row expands in place (GET /api/claude/sessions/{id}/files) into
//     everything ELSE that session touched, each a real link — which is the
//     "from the transcript, go to all relevant files" direction of the pivot.
//
// Nothing here mutates: no revert, no delete. The panel is a viewer over
// stores other tools own, exactly like the listing sources that feed it.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  getClaudeRelated,
  getClaudeSessionFiles,
  type AppInfo,
  type ClaudeFileSession,
  type ClaudeRelated,
  type ClaudeSessionFile,
} from "@platform/lib/api";
import { entryOf } from "@platform/lib/appEntry";
import { PANEL_EXIT_MS } from "@platform/lib/exit-animation";
import { basename, formatSize } from "@platform/lib/format";
import { useDeferredClose } from "@platform/lib/hooks";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { timeAgo } from "./AppPreviewCard";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

// One expandable session row. The file list is fetched on first expand and
// kept — collapsing and re-expanding must not refetch a transcript parse.
function SessionRow({ session, file }: { session: ClaudeFileSession; file: string }) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<Loaded<ClaudeSessionFile[]>>({ status: "loading" });
  const fetched = useRef(false);

  const toggle = () => {
    setOpen((o) => !o);
    if (fetched.current) return;
    fetched.current = true;
    getClaudeSessionFiles(session.session).then(
      (r) => setFiles({ status: "ok", data: r.files }),
      (e: Error) => setFiles({ status: "error", message: e.message }),
    );
  };

  const label = session.prompt || session.cwd || session.session;
  const ago = timeAgo(session.last_ts ?? session.updated_at);
  return (
    <li className="apph-session">
      <button type="button" className="apph-session-head" onClick={toggle} aria-expanded={open}>
        <span className={"apph-chevron" + (open ? " is-open" : "")} aria-hidden="true">
          ▸
        </span>
        <span className="apph-session-label" title={session.transcript}>
          {label}
        </span>
        <span className="apph-dim">
          {session.writes} write{session.writes === 1 ? "" : "s"}
          {ago ? ` · ${ago}` : ""}
        </span>
      </button>
      {open && (
        <div className="apph-session-files">
          {files.status === "loading" && <span className="apph-dim">Loading files…</span>}
          {files.status === "error" && <ErrorBanner>{files.message}</ErrorBanner>}
          {files.status === "ok" &&
            files.data.map((f) => (
              <a
                key={f.path}
                className={"apph-file" + (f.path === file ? " is-current" : "") + (f.exists ? "" : " is-gone")}
                href={urlForFsPath(f.path)}
                title={f.path}
                onClick={(e) => {
                  // Modified clicks stay the browser's (new tab); a plain one
                  // is an in-app navigation, same split as the app cards.
                  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                  e.preventDefault();
                  navigate(f.path);
                }}
              >
                <span className="apph-file-name">{basename(f.path)}</span>
                <span className="apph-dim">
                  {f.path === file ? "this file · " : ""}
                  {f.exists ? "" : "deleted · "}
                  {f.writes} write{f.writes === 1 ? "" : "s"}
                </span>
              </a>
            ))}
        </div>
      )}
    </li>
  );
}

export function AppHistoryPanel({ app, onClose }: { app: AppInfo; onClose: () => void }) {
  const file = entryOf(app) ?? app.path;
  const { closing, requestClose } = useDeferredClose(onClose, PANEL_EXIT_MS);
  const [open, setOpen] = useState(false);
  const [related, setRelated] = useState<Loaded<ClaudeRelated>>({ status: "loading" });

  useEffect(() => {
    const id = requestAnimationFrame(() => setOpen(true));
    return () => cancelAnimationFrame(id);
  }, []);
  useEffect(() => {
    let alive = true;
    getClaudeRelated(file).then(
      (data) => alive && setRelated({ status: "ok", data }),
      (e: Error) => alive && setRelated({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, [file]);

  // Esc closes, like every overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") requestClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [requestClose]);

  return createPortal(
    <div
      className={"app-panel-overlay" + (open && !closing ? " is-open" : "")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) requestClose();
      }}
    >
      <div
        className="app-panel apph-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="app-history-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="app-panel-head">
          <div className="app-panel-head-text">
            <span className="app-panel-eyebrow">History</span>
            <h2 id="app-history-title">{app.title || app.name}</h2>
          </div>
          <button type="button" className="app-panel-close" aria-label="Close" title="Close" onClick={requestClose}>
            ✕
          </button>
        </div>

        <div className="app-panel-body">
          <p className="apph-path" title={file}>
            {file}
          </p>
          {related.status === "loading" && <span className="apph-dim">Reading Claude Code's records…</span>}
          {related.status === "error" && <ErrorBanner>{related.message}</ErrorBanner>}
          {related.status === "ok" && (
            <>
              <div className="app-panel-section">
                <h3 className="app-panel-section-title">Checkpoints</h3>
                {related.data.versions.length === 0 ? (
                  <p className="apph-dim">
                    Claude Code recorded no checkpoints of this file — it checkpoints a file only
                    when a session is about to change it.
                  </p>
                ) : (
                  <ul className="apph-list">
                    {related.data.versions.map((v) => (
                      <li key={v.id} className="apph-version">
                        <span className="apph-version-id">v{v.version}</span>
                        <span className="apph-dim">session {v.session.slice(0, 8)}</span>
                        <span className="apph-dim">{timeAgo(v.mtime) ?? ""}</span>
                        <span className="apph-dim">{formatSize(v.size)}</span>
                        {/* What restoring this checkpoint would DO, or the one
                            row that holds exactly what is on disk. */}
                        {v.differs ? (
                          v.exact && (
                            <span className="apph-delta">
                              {v.added != null && v.added > 0 ? `+${v.added} ` : ""}
                              {v.removed != null && v.removed > 0 ? `−${v.removed}` : ""}
                            </span>
                          )
                        ) : (
                          <span className="apph-current">on disk now</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="app-panel-section">
                <h3 className="app-panel-section-title">Sessions that touched it</h3>
                {related.data.sessions.length === 0 ? (
                  <p className="apph-dim">
                    No recent Claude Code session touched this file. Only the most recent
                    transcripts are consulted, so an old session may simply have aged out.
                  </p>
                ) : (
                  <ul className="apph-list">
                    {related.data.sessions.map((s) => (
                      <SessionRow key={s.session + s.transcript} session={s} file={related.data.file} />
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
