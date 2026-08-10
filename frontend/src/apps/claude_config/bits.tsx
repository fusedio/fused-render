// Shared pieces of the Claude-config app: the row/card/pill vocabulary its
// sections are built from, the three-state toggle, the change-preview modal,
// the load-once-with-reload hook every section uses, and the two bits of page
// chrome (git badge, split preview pane) that BOTH pages now mount — the
// Config panel (ClaudeConfig.tsx) and the standalone CLAUDE.md page
// (ClaudeMdPage.tsx). They lived in ClaudeConfig.tsx while it was the only
// page; they moved here rather than being duplicated or imported across pages.
//
// Nothing here re-invents something the shell already ships. Toasts are
// @platform/lib/toast (the app-root NotificationHost surface — the original
// app's own fixed-position #toast would have been a second one), the modal
// chassis is @platform/ui/modal/Modal (focus trap, Esc, backdrop, portal),
// loading is @platform/ui/Skeleton and failures @platform/ui/ErrorBanner. What
// IS local is the vocabulary the old app invented — .card/.pill/.row/.group and
// the tri-state switch — because no shell equivalent exists.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { pushToast } from "@platform/lib/toast";
import { EMBED_PREFIX, VIEW_PREFIX } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import * as cc from "./api";
import type { ChangePreview } from "./api";

// Every section gets the same two capabilities: report that the on-disk config
// changed (so the nav's git badge re-reads), and nothing else. Sections own
// their own data and reload it themselves.
export interface SectionProps {
  onChanged: () => void;
}

export function toastOk(msg: string): void {
  pushToast({ msg, tone: "info" });
}

export function toastErr(msg: string): void {
  pushToast({ msg, tone: "error" });
}

// Await a module call, reporting a transport failure as a toast and resolving
// null instead of throwing — the `run()` wrapper of the original app. In-band
// `{ok: false}` returns are NOT handled here (see api.ts); the caller branches.
export async function guard<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    toastErr((e as Error).message);
    return null;
  }
}

// -- data loading -------------------------------------------------------------

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  reload: () => void;
}

// Load once per identity of `load`, with an explicit reload. `load` must be a
// useCallback so a re-render doesn't refetch; the section's own mutations call
// `reload()` the way the original app called render().
export function useModuleData<T>(load: () => Promise<T>): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    let alive = true;
    setError(null);
    load().then(
      (d) => {
        if (alive) setData(d);
      },
      (e) => {
        if (alive) setError((e as Error).message);
      },
    );
    return () => {
      alive = false;
    };
  }, [load, epoch]);
  return { data, error, reload: useCallback(() => setEpoch((n) => n + 1), []) };
}

// -- layout vocabulary --------------------------------------------------------

export function Group({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="cc-group">
      <h3 className="cc-group-title">{title}</h3>
      {children}
    </div>
  );
}

// A dir/path heading keeps its own casing and monospace (the CLAUDE.md tab's
// groups are filesystem paths, not category names).
export function PathGroup({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="cc-group">
      <h3 className="cc-group-title cc-group-path cc-mono">{title}</h3>
      {children}
    </div>
  );
}

export function Card({ children }: { children: ReactNode }) {
  return <div className="cc-card">{children}</div>;
}

export function CardTitle({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return <div className={"cc-card-title" + (mono ? " cc-mono" : "")}>{children}</div>;
}

export function CardSub({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return <div className={"cc-card-sub" + (mono ? " cc-mono" : "")}>{children}</div>;
}

export function CardActions({ children }: { children: ReactNode }) {
  return <div className="cc-card-actions">{children}</div>;
}

// A settings row: label + doc on the left, control on the right.
export function Row({
  label,
  doc,
  control,
}: {
  label: ReactNode;
  doc?: ReactNode;
  control: ReactNode;
}) {
  return (
    <div className="cc-row">
      <div className="cc-row-meta">
        <div className="cc-row-label">{label}</div>
        {doc ? <div className="cc-row-doc">{doc}</div> : null}
      </div>
      <div className="cc-row-control">{control}</div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="cc-empty">{children}</div>;
}

export type PillTone = "neutral" | "on" | "ro" | "err";

export function Pill({ tone = "neutral", children }: { tone?: PillTone; children: ReactNode }) {
  return <span className={"cc-pill cc-pill-" + tone}>{children}</span>;
}

// -- three-state toggle -------------------------------------------------------

// on / off / unset. Unset is a THIRD state (indeterminate), never rendered as
// off: a Claude default that happens to be `true` would otherwise read as an
// explicit `false` the user chose. `indeterminate` has no JSX attribute — it is
// a DOM property only — hence the ref write.
export function Toggle3({
  value,
  label,
  disabled,
  onChange,
}: {
  // null = unset (using Claude's own default).
  value: boolean | null;
  label: string;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = value === null;
  }, [value]);
  return (
    <span className="cc-switch">
      <input
        ref={ref}
        type="checkbox"
        aria-label={label}
        checked={value === true}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="cc-slider" aria-hidden="true" />
    </span>
  );
}

// -- change-preview modal -----------------------------------------------------

export interface PreviewButton<T> {
  label: string;
  value: T;
  primary?: boolean;
  danger?: boolean;
}

export interface AskOptions<T> {
  title: string;
  // The {files, settings} pair git_ops returns; omitted for a plain confirm.
  preview?: ChangePreview;
  // Prose shown when there is nothing to diff (the MCP OAuth hand-off), or a
  // confirm's warning.
  note?: ReactNode;
  buttons: PreviewButton<T>[];
}

interface AskState {
  opts: AskOptions<unknown>;
  resolve: (v: unknown) => void;
}

// An imperative confirm/preview modal: `await ask({...})` resolves the clicked
// button's value, or `false` if the modal was dismissed (Esc / backdrop / ✕).
// Promise-shaped rather than declarative because the flows it serves are
// sequential — a profile switch previews, then may ask to commit first, then
// switches — and expressing that as nested render state would scatter one
// decision across three components.
export function useChangePreview(): { node: ReactNode; ask: <T>(o: AskOptions<T>) => Promise<T | false> } {
  const [state, setState] = useState<AskState | null>(null);

  const ask = useCallback(
    <T,>(opts: AskOptions<T>) =>
      new Promise<T | false>((resolve) => {
        setState({
          opts: opts as AskOptions<unknown>,
          resolve: resolve as (v: unknown) => void,
        });
      }),
    [],
  );

  const settle = (value: unknown) => {
    setState(null);
    state?.resolve(value);
  };

  if (!state) return { node: null, ask };

  const { title, preview, note, buttons } = state.opts;
  const files = preview?.files ?? [];
  const settings = preview?.settings ?? [];
  const hasDiff = files.length > 0 || settings.length > 0;

  const node = (
    <Modal
      title={title}
      width={620}
      onClose={() => settle(false)}
      footer={buttons.map((b) => (
        <button
          key={b.label}
          type="button"
          className={
            "btn" + (b.primary ? (b.danger ? " btn-danger" : " btn-primary") : "")
          }
          onClick={() => settle(b.value)}
        >
          {b.label}
        </button>
      ))}
    >
      {note ? <p className="cc-note">{note}</p> : null}
      {settings.length > 0 && (
        <>
          <h4 className="cc-delta-head">Settings changes</h4>
          {settings.map((d) => (
            <div className="cc-delta" key={d.key}>
              <span className="cc-delta-key">{d.key}</span>{" "}
              <span className="cc-delta-fromto cc-mono">
                {JSON.stringify(d.from)} → {JSON.stringify(d.to)}
              </span>
            </div>
          ))}
        </>
      )}
      {files.length > 0 && (
        <>
          <h4 className="cc-delta-head">Files</h4>
          {files.map((f) => (
            <div className="cc-delta" key={f.status + f.path}>
              <span className={"cc-fstat cc-fstat-" + f.status}>{f.status}</span>{" "}
              <span className="cc-mono">{f.path}</span>
            </div>
          ))}
        </>
      )}
      {!hasDiff && !note && <Empty>No changes.</Empty>}
    </Modal>
  );
  return { node, ask };
}

// -- page chrome --------------------------------------------------------------

// The git badge: the repo's uncommitted-change count, and the way to commit it.
// Clean, it is just a re-check; dirty, it previews the drift first — a commit
// here folds every pending edit into one, so what it sweeps up has to be
// visible before it happens.
export function StatusBadge({
  epoch,
  onCommitted,
}: {
  epoch: number;
  onCommitted: () => void;
}) {
  const [status, setStatus] = useState<cc.GitStatus | null>(null);
  const [failed, setFailed] = useState(false);
  // A plain re-check is the badge's own business: it must not remount the
  // section the way a commit does.
  const [recheck, setRecheck] = useState(0);
  const { node: modal, ask } = useChangePreview();

  useEffect(() => {
    let alive = true;
    cc.gitOps.status().then(
      (s) => alive && setStatus(s),
      () => alive && setFailed(true),
    );
    return () => {
      alive = false;
    };
  }, [epoch, recheck]);

  const click = async () => {
    if (!status?.dirty) {
      setRecheck((n) => n + 1);
      return;
    }
    const drift = await guard(cc.gitOps.drift());
    if (!drift) return;
    const choice = await ask<"commit" | false>({
      title: "Uncommitted changes",
      preview: drift,
      buttons: [
        { label: "Close", value: false },
        { label: "Commit", value: "commit", primary: true },
      ],
    });
    if (choice !== "commit") return;
    if (!(await guard(cc.gitOps.commit()))) return;
    toastOk("Committed");
    onCommitted();
  };

  const label = failed
    ? "status unavailable"
    : !status
      ? "checking…"
      : status.dirty
        ? `${status.files.length} uncommitted change(s)`
        : "✓ all changes committed";

  return (
    <>
      {modal}
      <button
        type="button"
        className={"cc-badge" + (status?.dirty ? " dirty" : status ? " clean" : "")}
        disabled={failed}
        title={status?.dirty ? "Review and commit the pending changes" : "Re-check git status"}
        onClick={click}
      >
        {label}
      </button>
    </>
  );
}

// fs path -> the encoded tail of an /explorer/view/ or /explorer/embed/ URL.
// Same codec as router.urlForFsPath, spelled out here because that helper picks
// the prefix from the CURRENT page's mode and the preview pane needs both.
function encodePath(fsPath: string): string {
  return fsPath
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
}

// The split preview pane: the shell's OWN chrome-free view of the file
// (/explorer/embed/<path>), which is how any markdown gets rendered here — this
// app has no renderer of its own and should not grow one.
export function PreviewPane({ path, onClose }: { path: string; onClose: () => void }) {
  return (
    <aside className="cc-preview">
      <div className="cc-preview-head">
        {/* Ellipsized from the LEFT (direction: rtl) so the filename tail stays
            readable; <bdi dir="ltr"> keeps the path's own characters in logical
            order, without which the leading "/" renders at the far end. */}
        <span className="cc-preview-path cc-mono">
          <bdi dir="ltr">{path}</bdi>
        </span>
        <button
          type="button"
          className="btn"
          title="Open in the file explorer (new tab)"
          onClick={() => window.open(VIEW_PREFIX + encodePath(path), "_blank")}
        >
          ↗
        </button>
        <button type="button" className="btn" title="Close preview" onClick={onClose}>
          ✕
        </button>
      </div>
      <iframe
        className="cc-preview-frame"
        title={`Preview of ${path}`}
        src={EMBED_PREFIX + encodePath(path)}
      />
    </aside>
  );
}

// -- misc ---------------------------------------------------------------------

// Read a File as base64 with the data: prefix stripped — what the profiles
// module's b64 kwargs expect.
export function fileToB64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("could not read file"));
    r.readAsDataURL(file);
  });
}

// Feather-style stroke icons for the card actions. stroke=currentColor so each
// one inherits its button's text colour, including a danger hover.
export function Icon({ name }: { name: "edit" | "eye" | "folder" | "trash" | "refresh" }) {
  const paths: Record<string, ReactNode> = {
    edit: <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />,
    eye: (
      <>
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
    folder: <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />,
    trash: (
      <>
        <polyline points="3 6 5 6 21 6" />
        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      </>
    ),
    refresh: (
      <>
        <polyline points="23 4 23 10 17 10" />
        <path d="M20.5 15a9 9 0 1 1-2.6-9.4L23 10" />
      </>
    ),
  };
  return (
    <svg
      className="cc-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}
