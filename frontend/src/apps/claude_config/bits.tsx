// Shared pieces of the Claude-config app: the ONE list row every tab's items
// render as (`ListRow`), the card and pill vocabulary around it, the
// three-state toggle, the confirm modal, and the load-once-with-reload hook
// every section uses.
//
// The bias is deliberately towards ONE of each thing: one row, one skeleton
// length, one empty-state shape, so every tab's paddings, verbs and action
// placements are the same to re-learn.
//
// Nothing here re-invents something the shell already ships. Toasts are
// @platform/lib/notifications (the app-root NotificationHost surface — the
// original app's own fixed-position #toast would have been a second one), the modal
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
import { notify } from "@platform/lib/notifications";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Modal } from "@platform/ui/modal/Modal";

// Every section receives the page's name filter. Sections own their own data
// and reload it themselves.
export interface SectionProps {
  filter?: string;
}

/** The tab's name and its static description. The section draws the head
 *  itself so the description can carry a live fact once data is in. */
export type HeadedSectionProps = SectionProps & { title: string; blurb: string };

/** "1 file", "7 projects". */
export function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The heading every tab opens with: the title with the tab's actions on its
 *  right, and one muted line beneath. `lead` sits before the title (a back
 *  button). */
export function Head({
  title,
  blurb,
  lead,
  aside,
}: {
  title: ReactNode;
  blurb: ReactNode;
  lead?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="settings-head">
      <div className="settings-head-row">
        <div className="cc-skill-headline">
          {lead}
          <h2>{title}</h2>
        </div>
        {aside && <div className="cc-skill-actions">{aside}</div>}
      </div>
      <p>{blurb}</p>
    </div>
  );
}

export function RefreshButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="cc-iconbtn" title="Refresh" aria-label="Refresh" onClick={onClick}>
      <Icon name="refresh" />
    </button>
  );
}

export function toastOk(msg: string): void {
  notify({ title: msg, tone: "info" });
}

export function toastErr(msg: string): void {
  notify({ title: msg, tone: "error" });
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

// The control that opens an add form inline above the list. Adding is the rarer
// act, so the form starts closed and the same button cancels.
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
// MCP and Memory.

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

// -- confirm modal -----------------------------------------------------------

export interface PreviewButton<T> {
  label: string;
  value: T;
  primary?: boolean;
  danger?: boolean;
}

export interface AskOptions<T> {
  title: string;
  // Prose shown in the body: a confirm's warning, or the MCP OAuth hand-off.
  note?: ReactNode;
  buttons: PreviewButton<T>[];
}

interface AskState {
  opts: AskOptions<unknown>;
  resolve: (v: unknown) => void;
}

// An imperative confirm modal: `await ask({...})` resolves the clicked button's
// value, or `false` if the modal was dismissed (Esc / backdrop / ✕).
export function useConfirm(): { node: ReactNode; ask: <T>(o: AskOptions<T>) => Promise<T | false> } {
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

  const { title, note, buttons } = state.opts;

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
    </Modal>
  );
  return { node, ask };
}

// Feather-style stroke icons for the card actions. stroke=currentColor so each
// one inherits its button's text colour, including a danger hover.
export function Icon({
  name,
}: {
  name: "folder" | "trash" | "refresh" | "copy" | "chevron" | "plus" | "kebab";
}) {
  const paths: Record<string, ReactNode> = {
    // Per-marketplace menu, in the Plugins rail: one trailing slot for every
    // action a row has, so the 180px column goes to the name and count.
    kebab: (
      <>
        <circle cx="12" cy="5" r="1.4" />
        <circle cx="12" cy="12" r="1.4" />
        <circle cx="12" cy="19" r="1.4" />
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
    copy: (
      <>
        <rect x="9" y="9" width="12" height="12" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
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
