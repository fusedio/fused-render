// Shared pieces of the Claude-config app: the ONE list row every tab's items
// render as (`ListRow`), the card/pill/group vocabulary around it, the
// three-state toggle, the change-preview modal, the load-once-with-reload hook
// every section uses, and the git-status hook the tab strip and the History
// page share. They lived in ClaudeConfig.tsx; they sit here so a section and
// the panel can both reach them without importing across pages.
//
// The bias is deliberately towards ONE of each thing. Nine tabs doing the same
// job four ways is four sets of paddings, verbs and action placements for the
// user to re-learn per tab, and it happened here simply because each section
// was written on a different day. So: one row, one toolbar, one skeleton
// length, one empty-state shape.
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
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { pushToast } from "@platform/lib/toast";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
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

// One bordered card per list, rows inside it separated by hairlines rather
// than free-floating on the page background — see claude-config.css's .cc-list
// for the defect this fixes (a row's content stopping well short of the page's
// full-bleed width, with a hairline running under the void it left).
export function List({ children }: { children: ReactNode }) {
  return <div className="cc-list">{children}</div>;
}

// A list's loading state, in the same card shape it will resolve into —
// shadcn's own Skeleton primitive rather than the app-wide shimmer-bar
// pattern (SkeletonLines), so a list's "loading" and "loaded" states share one
// silhouette instead of a generic bar block that vanishes into a differently
// shaped card once the rows land.
export function ListSkeleton({
  rows = SKELETON_ROWS,
  label = "Loading",
}: {
  rows?: number;
  label?: string;
}) {
  return (
    <div className="cc-list" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div className="cc-lrow" key={i}>
          <div className="cc-lrow-line">
            {/* Skeleton's own default fill is shadcn's bg-muted — the
                Tailwind grayscale palette, not this app's tokens. In light
                theme that resolves within ~1% contrast of .cc-list's own
                --bg-alt, so every list's loading state was an empty bordered
                card. --border is what every hairline in this app already
                uses for "visible against --bg-alt in both themes". */}
            <Skeleton className="h-3 w-28 shrink-0 rounded-full bg-[var(--border)]" />
            <Skeleton className="h-3 w-full max-w-64 rounded-full bg-[var(--border)]" />
          </div>
        </div>
      ))}
    </div>
  );
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

// An empty state, with the control that fixes it where there is one. Every list
// in this app can be empty on a fresh machine, and "nothing here" without a way
// out is a dead end — so `action` is part of the shape rather than something
// each section remembers to add.
export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="cc-empty">
      <div>{children}</div>
      {action ? <div className="cc-empty-action">{action}</div> : null}
    </div>
  );
}

// -- one toolbar --------------------------------------------------------------

// The header row every tab opens with: a plain summary of what you are looking
// at on the left, the tab's own controls, then — in the same slot on every tab —
// the refresh icon. Before this, each tab hand-rolled its own header, which is
// how MCP ended up with a text `Refresh` button INSIDE its add-a-server form,
// where refreshing has nothing to do with adding.
//
// `summary` is deliberately a fact and not a title ("12 marketplaces", "4
// servers · 3 connected"): the caption above already names the file, and the tab
// strip already names the tab.
export function SectionToolbar({
  summary,
  children,
  onRefresh,
  // Override only where "refresh" is the wrong word for the same act (MD Files
  // re-runs a filesystem scan; Statusline re-runs the preview command).
  refreshLabel = "Refresh",
  refreshBusy,
}: {
  summary: ReactNode;
  children?: ReactNode;
  onRefresh?: () => void;
  refreshLabel?: string;
  // Disables the refresh while its work is in flight. Offered rather than
  // assumed, because most tabs' refresh is an idempotent read whose worst case
  // is a wasted fetch — but Statusline's runs the USER'S OWN command through
  // `sh -c`, where three fast clicks are three concurrent processes and the
  // results land in completion order, so a slow earlier run can overwrite a
  // newer one. A control that spawns a process should not be re-entrant just
  // because it is drawn as an icon.
  refreshBusy?: boolean;
}) {
  return (
    <div className="cc-toolbar">
      <span className="cc-summary" title={typeof summary === "string" ? summary : undefined}>
        {summary}
      </span>
      {/* Every control after the summary travels as ONE flex item — see
          .cc-toolbar-controls in claude-config.css. That is what stops the
          toolbar wrapping into a ragged stack at a narrow width: the summary
          drops to its own line first, and the controls keep their single row
          for as long as there is room for any of them. `-solo` (no children,
          just the refresh icon — Memory, Skills) is the one exception: at a
          narrow width the base rule still forces the summary onto its own
          full line even though a single icon button had room to share it,
          which read as a dead row under "10 skill(s)". The modifier lets the
          narrow media query recognize that case and keep them together. */}
      {(children || onRefresh) && (
        <div className={"cc-toolbar-controls" + (children ? "" : " cc-toolbar-controls-solo")}>
          {children}
          {onRefresh && (
            <button
              type="button"
              className="cc-iconbtn"
              title={refreshBusy ? `${refreshLabel} — running…` : refreshLabel}
              aria-label={refreshLabel}
              aria-busy={refreshBusy || undefined}
              disabled={refreshBusy}
              onClick={onRefresh}
            >
              <Icon name="refresh" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// The toolbar control that opens an add form inline above the list. Three tabs
// (Marketplaces, MCP, Profiles) used to OPEN with their add form expanded —
// Profiles with two of them stacked — so the content you came for started half
// a page down on a tab you mostly visit to look at what's already there.
// Adding is the rarer act, so it asks first, and the same button cancels.
export function DisclosureButton({
  open,
  controls,
  label,
  onToggle,
}: {
  open: boolean;
  // The id of the form this reveals — aria-controls, so the relationship is on
  // the element and not just in the layout.
  controls: string;
  label: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={"btn" + (open ? " cc-btn-on" : "")}
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
    >
      {open ? "Cancel" : <><Icon name="plus" />{label}</>}
    </button>
  );
}

// -- one list row -------------------------------------------------------------

// Every list in this app renders through this: Plugins, Marketplaces, Skills,
// MCP, Profiles, Memory and History's log. (MD Files keeps its card grid —
// browse-and-preview is a different job — and Preferences keeps .cc-row, which
// is label-plus-control, not an item.) Before this there were four shapes for
// one job, which is four sets of paddings, fonts and action placements for the
// user to re-learn per tab.
//
// One line AT REST, never wrapping, so every row is the same height and twenty
// of them can be scanned. But one line must not mean information thrown away:
// where a row has more to say than fits — a description, a file list, extra
// fields — it EXPANDS. `details` is what makes a row expandable, so a row with
// nothing behind it has no chevron and no dead affordance.
//
// Two triggers open it: the row body and the chevron. That is deliberate (the
// body is the big target; the chevron is the one that LOOKS like a disclosure)
// and both carry aria-expanded/aria-controls over the same panel, which is what
// ARIA expects of two buttons controlling one region. The open state is the
// row's own — a tab change remounts the section, so it resets by construction
// and never belongs in the URL.
export function ListRow({
  lead,
  name,
  nameMono,
  pills,
  secondary,
  secondaryMono,
  // Hover text for the ellipsized secondary: the inline text may be cut, so the
  // full string stays reachable without expanding.
  secondaryTitle,
  // One extra class on the secondary. Exists for exactly one case — a PATH,
  // which must ellipsize from the left so the tail survives (MD Files).
  secondaryClass,
  meta,
  actions,
  details,
}: {
  lead?: ReactNode;
  name?: ReactNode;
  nameMono?: boolean;
  pills?: ReactNode;
  secondary?: ReactNode;
  secondaryMono?: boolean;
  secondaryTitle?: string;
  secondaryClass?: string;
  meta?: ReactNode;
  actions?: ReactNode;
  details?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const toggle = () => setOpen((v) => !v);

  const inner = (
    <>
      {name != null && <span className={"cc-lrow-name" + (nameMono ? " cc-mono" : "")}>{name}</span>}
      {pills}
      {secondary != null && (
        <span
          className={
            "cc-lrow-sub" +
            (secondaryMono ? " cc-mono" : "") +
            (secondaryClass ? " " + secondaryClass : "")
          }
          title={secondaryTitle}
        >
          {secondary}
        </span>
      )}
    </>
  );

  return (
    <div className={"cc-lrow" + (open ? " open" : "")}>
      <div className="cc-lrow-line">
        {/* A fixed-width slot rather than the control's own natural width —
            it keeps a lead control (Toggle3) from becoming the loudest thing
            on the line just because it happens to be the widest. */}
        {lead != null && <span className="cc-lrow-lead">{lead}</span>}
        {details ? (
          <button
            type="button"
            className="cc-lrow-body"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={toggle}
          >
            {inner}
          </button>
        ) : (
          <span className="cc-lrow-body cc-lrow-body-flat">{inner}</span>
        )}
        {meta}
        <div className="cc-lrow-actions">
          {actions}
          {details && (
            <button
              type="button"
              className="cc-iconbtn cc-lrow-chev"
              aria-expanded={open}
              aria-controls={panelId}
              aria-label={open ? "Hide details" : "Show details"}
              title={open ? "Hide details" : "Show details"}
              onClick={toggle}
            >
              <Icon name="chevron" />
            </button>
          )}
        </div>
      </div>
      {details && open && (
        <div className="cc-lrow-details" id={panelId}>
          {details}
        </div>
      )}
    </div>
  );
}

// The rows a list skeleton stands in for. One number for every tab: the lists
// are the same kind of thing, and 3-here-4-there was only ever whoever wrote
// the section that day.
export const SKELETON_ROWS = 4;

export type PillTone = "neutral" | "on" | "ro" | "err";

export function Pill({ tone = "neutral", children }: { tone?: PillTone; children: ReactNode }) {
  return <span className={"cc-pill cc-pill-" + tone}>{children}</span>;
}

// -- three-state toggle -------------------------------------------------------

// on / off / unset. Unset is a THIRD state, never silently rendered as off: a
// Claude default that happens to be `true` must not read as an explicit `false`
// the user chose. It has two renderings, because there are two kinds of unset:
//
//   * we know the documented default -> `inherited`: the switch sits at that
//     position, so the page states what Claude will actually do, but stays
//     dashed and muted so it reads as inherited rather than chosen. The
//     "Claude default" text beside it is what carries set-vs-unset in words.
//   * we don't -> `value: null`, the indeterminate middle. Reserved for
//     genuinely not knowing. (`indeterminate` has no JSX attribute — it is a DOM
//     property only — hence the ref write.)
export function Toggle3({
  value,
  label,
  disabled,
  inherited,
  onChange,
}: {
  // null = unset with no known default (renders indeterminate).
  value: boolean | null;
  label: string;
  disabled?: boolean;
  // The position shown is Claude's, not the user's. Styling only — the value
  // still round-trips as itself.
  inherited?: boolean;
  onChange: (next: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = value === null;
  }, [value]);
  return (
    <span className={"cc-switch" + (inherited ? " inherited" : "")}>
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

// -- git status ---------------------------------------------------------------

export interface GitStatusState {
  // null until the first read lands; stays null if it failed.
  status: cc.GitStatus | null;
  failed: boolean;
  // Re-read WITHOUT bumping the caller's epoch — a plain re-check must not
  // remount anything the way a commit does.
  recheck: () => void;
}

// One `git status` read per epoch, shared by the two places that need it: the
// tab strip's History button (which only renders the dirty dot) and the History
// page's "Uncommitted changes" card (which acts on it). A hook rather than a
// component because those two render the same fact completely differently, and
// the thing worth sharing is the fetch — one status call per epoch, no more.
export function useGitStatus(epoch: number): GitStatusState {
  const [status, setStatus] = useState<cc.GitStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [n, setN] = useState(0);

  useEffect(() => {
    let alive = true;
    cc.gitOps.status().then(
      (s) => {
        if (!alive) return;
        setStatus(s);
        // Clearing this is not tidiness — it is the difference between a
        // recoverable failure and a dead page. A stale `failed` outlives the
        // success that fixed it, and the History card would then render
        // "Status unavailable" ABOVE a live "Review & commit" button.
        setFailed(false);
      },
      () => alive && setFailed(true),
    );
    return () => {
      alive = false;
    };
  }, [epoch, n]);

  return { status, failed, recheck: useCallback(() => setN((v) => v + 1), []) };
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
export function Icon({
  name,
}: {
  name:
    | "edit"
    | "eye"
    | "folder"
    | "trash"
    | "refresh"
    | "clock"
    | "copy"
    | "chevron"
    | "plus"
    | "undo"
    | "info"
    | "lock";
}) {
  const paths: Record<string, ReactNode> = {
    // The Preferences unset-value hint (title= carries the actual text —
    // see .cc-row-hint in claude-config.css for why it isn't inline text).
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <line x1="12" y1="11" x2="12" y2="16" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </>
    ),
    // A read-only marketplace, in the Plugins rail — a muted fact, not a
    // warning, so it is a quiet glyph rather than a coloured pill (round 2:
    // the pill overflowed the rail's fixed width and was tinted --warning,
    // a hue this app reserves for uncommitted git state).
    lock: (
      <>
        <rect x="5" y="11" width="14" height="9" rx="2" />
        <path d="M8 11V7a4 4 0 0 1 8 0v4" />
      </>
    ),
    // The Preferences "reset to default" ghost icon button — a corner-arrow
    // undo, deliberately not the refresh icon's rotation glyph, so the two
    // don't read as the same action.
    undo: (
      <>
        <polyline points="9 14 4 9 9 4" />
        <path d="M20 20v-7a4 4 0 0 0-4-4H4" />
      </>
    ),
    // Points down at rest; .cc-lrow.open rotates it (CSS, so no second glyph).
    chevron: <polyline points="6 9 12 15 18 9" />,
    plus: (
      <>
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <polyline points="12 7 12 12 15.5 14" />
      </>
    ),
    copy: (
      <>
        <rect x="9" y="9" width="12" height="12" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </>
    ),
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
