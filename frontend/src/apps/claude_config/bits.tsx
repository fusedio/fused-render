// Shared pieces of the Claude-config app: the ONE list row every tab's items
// render as (`ListRow`), the card/pill/group vocabulary around it, the
// three-state toggle, the change-preview dialog, the load-once-with-reload hook
// every section uses, and the git-status hook the header chip and the History
// page share. They lived in ClaudeConfig.tsx; they sit here so a section and
// the panel can both reach them without importing across pages.
//
// The bias is deliberately towards ONE of each thing. Nine tabs doing the same
// job four ways is four sets of paddings, verbs and action placements for the
// user to re-learn per tab, and it happened here simply because each section
// was written on a different day. So: one row, one toolbar, one skeleton
// length, one empty-state shape.
//
// Flow design language (the .claude/skills/flow-design-language rules): every
// piece here composes the shadcn primitives under @platform/shadcn/ui and the
// flow composites under @platform/ui/flow. Colour is semantic tokens only, and
// the only chromatic colour comes through status-colors.ts via StatusBadge /
// StatusDot. Toasts are @platform/lib/toast (the app-root NotificationHost
// surface).
import {
  useCallback,
  useEffect,
  useId,
  useState,
  type ReactNode,
} from "react";
import { ChevronDown, Plus, RefreshCw } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { pushToast } from "@platform/lib/toast";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import {
  Empty as EmptyRoot,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
} from "@platform/shadcn/ui/empty";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Switch } from "@platform/shadcn/ui/switch";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import { SectionHeading } from "@platform/ui/flow/Typography";
import type { StatusBucket } from "@platform/ui/status-colors";
import * as cc from "./api";
import type { ChangePreview } from "./api";

// Every section gets the same two capabilities: report that the on-disk config
// changed (so the header's git chip re-reads), and nothing else. Sections own
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

// A failure, in the same voice everywhere: shadcn's destructive Alert.
export function ErrorNote({ children }: { children: ReactNode }) {
  if (children == null || children === false) return null;
  return (
    <Alert variant="destructive">
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  );
}

export function Group({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <SectionHeading>{title}</SectionHeading>
      {children}
    </section>
  );
}

// A bordered, square-cornered card (Flow: cards are surfaces, so rounded-lg =
// 0px). Used for the one-off statement blocks — the drift card, the inline
// add forms — that are not lists.
export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("border border-border rounded-lg bg-card px-4 py-3 space-y-1", className)}>
      {children}
    </div>
  );
}

// One bordered card per list, rows inside it separated by hairlines rather
// than free-floating on the page background.
export function List({ children }: { children: ReactNode }) {
  return <EntityList>{children}</EntityList>;
}

// A list's loading state, in the same card shape it will resolve into, so a
// list's "loading" and "loaded" states share one silhouette.
export function ListSkeleton({
  rows = SKELETON_ROWS,
  label = "Loading",
}: {
  rows?: number;
  label?: string;
}) {
  return (
    <EntityList role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border last:border-b-0" key={i}>
          <Skeleton className="h-3 w-28 shrink-0 rounded-full" />
          <Skeleton className="h-3 w-full max-w-64 rounded-full" />
        </div>
      ))}
    </EntityList>
  );
}

export function CardTitle({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return (
    <div className={cn("text-sm font-medium flex items-center gap-2", mono && "font-mono text-xs")}>
      {children}
    </div>
  );
}

export function CardSub({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return <div className={cn("text-sm text-muted-foreground", mono && "font-mono text-xs")}>{children}</div>;
}

export function CardActions({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2 pt-2">{children}</div>;
}

// Inline code inside prose: a command, a key, a filename.
export function Code({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>;
}

// A settings row: label + doc on the left, control on the right. Rows stack
// inside a <List>, which draws the shared border.
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
    <div className="flex items-center justify-between gap-6 px-4 py-2.5 text-sm border-b border-border last:border-b-0">
      <div className="min-w-0 max-w-prose">
        <div className="font-medium">{label}</div>
        {doc ? <div className="text-xs text-muted-foreground mt-0.5">{doc}</div> : null}
      </div>
      <div className="flex items-center gap-2 shrink-0">{control}</div>
    </div>
  );
}

// An empty state, with the control that fixes it where there is one. Every list
// in this app can be empty on a fresh machine, and "nothing here" without a way
// out is a dead end — so `action` is part of the shape rather than something
// each section remembers to add.
export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <EmptyRoot className="border border-dashed border-border py-8">
      <EmptyHeader>
        <EmptyDescription>{children}</EmptyDescription>
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </EmptyRoot>
  );
}

// -- one toolbar --------------------------------------------------------------

// The header row every tab opens with: a plain summary of what you are looking
// at on the left, the tab's own controls, then — in the same slot on every tab —
// the refresh icon.
//
// `summary` is deliberately a fact and not a title ("12 marketplaces", "4
// servers · 3 connected"): the caption above already names the file, and the tab
// strip already names the tab.
export function SectionToolbar({
  summary,
  children,
  onRefresh,
  // Override only where "refresh" is the wrong word for the same act
  // (Statusline re-runs the preview command).
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
    <div className="flex flex-wrap items-center gap-2 min-h-8">
      <span
        className="text-sm text-muted-foreground truncate min-w-0"
        title={typeof summary === "string" ? summary : undefined}
      >
        {summary}
      </span>
      {(children || onRefresh) && (
        <div className="ml-auto flex items-center gap-2">
          {children}
          {onRefresh && (
            <Button
              variant="ghost"
              size="icon-sm"
              title={refreshBusy ? `${refreshLabel} — running…` : refreshLabel}
              aria-label={refreshLabel}
              aria-busy={refreshBusy || undefined}
              disabled={refreshBusy}
              onClick={onRefresh}
            >
              <RefreshCw className={cn(refreshBusy && "motion-safe:animate-spin")} />
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

// The toolbar control that opens an add form inline above the list. Adding is
// the rarer act, so it asks first, and the same button cancels.
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
    <Button
      variant={open ? "secondary" : "outline"}
      size="sm"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
    >
      {open ? "Cancel" : <><Plus />{label}</>}
    </Button>
  );
}

// -- one list row -------------------------------------------------------------

// Every list in this app renders through this: Plugins, Skills, MCP, Profiles,
// Memory and History's log. (Preferences keeps <Row>, which is label-plus-
// control, not an item.)
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
//
// Local rather than the flow EntityRow: that composite is a single flex line
// and cannot host the details panel under it.
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
  // Clamp the secondary to two lines instead of one (Skills, Memory — where the
  // description IS the content).
  secondaryClamp2,
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
  secondaryClamp2?: boolean;
  meta?: ReactNode;
  actions?: ReactNode;
  details?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const toggle = () => setOpen((v) => !v);

  const inner = (
    <>
      {name != null && (
        <span className={cn("font-medium shrink-0 truncate max-w-[40%]", nameMono && "font-mono text-xs")}>
          {name}
        </span>
      )}
      {pills}
      {secondary != null && (
        <span
          className={cn(
            "text-muted-foreground min-w-0",
            secondaryClamp2 ? "line-clamp-2 text-xs" : "truncate",
            secondaryMono && "font-mono text-xs",
          )}
          title={secondaryTitle}
        >
          {secondary}
        </span>
      )}
    </>
  );

  const bodyCls = "flex-1 min-w-0 flex items-center gap-2 text-left text-sm";

  return (
    <div className="border-b border-border last:border-b-0 group/row">
      <div className="flex items-center gap-3 px-4 py-2 min-h-10">
        {/* A fixed-width slot rather than the control's own natural width —
            it keeps a lead control (Toggle3) from becoming the loudest thing
            on the line just because it happens to be the widest. */}
        {lead != null && <span className="shrink-0 flex items-center w-8">{lead}</span>}
        {details ? (
          <button
            type="button"
            className={cn(bodyCls, "cursor-pointer rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50")}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={toggle}
          >
            {inner}
          </button>
        ) : (
          <span className={bodyCls}>{inner}</span>
        )}
        {meta != null && (
          <span className="shrink-0 flex items-center gap-3 text-xs text-muted-foreground">{meta}</span>
        )}
        {(actions || details) && (
          <div className="shrink-0 flex items-center gap-1">
            {actions}
            {details && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-expanded={open}
                aria-controls={panelId}
                aria-label={open ? "Hide details" : "Show details"}
                title={open ? "Hide details" : "Show details"}
                onClick={toggle}
              >
                <ChevronDown className={cn("motion-safe:transition-transform", open && "rotate-180")} />
              </Button>
            )}
          </div>
        )}
      </div>
      {details && open && (
        <div className="px-4 pb-3 pt-1 text-sm space-y-2" id={panelId}>
          {details}
        </div>
      )}
    </div>
  );
}

// Meta text inside a ListRow's `meta` slot: tiny, muted, optionally mono.
export function Meta({ children, mono, className }: { children: ReactNode; mono?: boolean; className?: string }) {
  return <span className={cn("text-xs text-muted-foreground", mono && "font-mono", className)}>{children}</span>;
}

// The rows a list skeleton stands in for. One number for every tab: the lists
// are the same kind of thing, and 3-here-4-there was only ever whoever wrote
// the section that day.
export const SKELETON_ROWS = 4;

// -- pills --------------------------------------------------------------------

// neutral = a fact ("default", "not installed"); on = healthy/active; ro =
// waiting on the user (read-only, drift, needs auth); err = broken. The three
// chromatic tones are status buckets, single-sourced in status-colors.ts.
export type PillTone = "neutral" | "on" | "ro" | "err";

const TONE_BUCKET: Record<Exclude<PillTone, "neutral">, StatusBucket> = {
  on: "green",
  ro: "orange",
  err: "red",
};

export function Pill({ tone = "neutral", children }: { tone?: PillTone; children: ReactNode }) {
  if (tone === "neutral") return <Badge variant="secondary">{children}</Badge>;
  return <StatusBadge bucket={TONE_BUCKET[tone]}>{children}</StatusBadge>;
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
//   * we don't -> `value: null`, the unknown middle. Reserved for genuinely not
//     knowing. A role="switch" has no indeterminate state, so this renders as
//     the same dashed, muted treatment at the off position and the caller's
//     sr-only text carries the fact in words.
export function Toggle3({
  value,
  label,
  disabled,
  inherited,
  onChange,
}: {
  // null = unset with no known default.
  value: boolean | null;
  label: string;
  disabled?: boolean;
  // The position shown is Claude's, not the user's. Styling only — the value
  // still round-trips as itself.
  inherited?: boolean;
  onChange: (next: boolean) => void;
}) {
  const muted = inherited || value === null;
  return (
    <Switch
      size="sm"
      aria-label={label}
      data-inherited={muted || undefined}
      checked={value === true}
      disabled={disabled}
      onCheckedChange={(next) => onChange(next)}
      className={cn(muted && "border-dashed border-muted-foreground opacity-70 data-checked:bg-primary/50")}
    />
  );
}

// -- change-preview dialog ----------------------------------------------------

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

// git status letters → status bucket: added is fresh, modified is in flight,
// deleted is gone. Anything else (renamed, untracked) stays neutral.
const FSTAT_BUCKET: Record<string, StatusBucket> = { A: "green", M: "yellow", D: "red" };

// An imperative confirm/preview dialog: `await ask({...})` resolves the clicked
// button's value, or `false` if the dialog was dismissed (Esc / backdrop / ✕).
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
    <Dialog open onOpenChange={(o) => !o && settle(false)}>
      <DialogContent className="sm:max-w-[620px]">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {note ? <DialogDescription>{note}</DialogDescription> : null}
        </DialogHeader>
        {settings.length > 0 && (
          <div className="space-y-1">
            <SectionHeading className="text-xs">Settings changes</SectionHeading>
            {settings.map((d) => (
              <div className="flex items-baseline gap-2 text-sm" key={d.key}>
                <span className="font-medium">{d.key}</span>
                <span className="font-mono text-xs text-muted-foreground truncate">
                  {JSON.stringify(d.from)} → {JSON.stringify(d.to)}
                </span>
              </div>
            ))}
          </div>
        )}
        {files.length > 0 && (
          <div className="space-y-1">
            <SectionHeading className="text-xs">Files</SectionHeading>
            <div className="bg-neutral-950 rounded-lg p-3 font-mono text-xs text-neutral-200 space-y-0.5 max-h-64 overflow-y-auto">
              {files.map((f) => (
                <div className="flex items-baseline gap-2" key={f.status + f.path}>
                  <StatusBadge bucket={FSTAT_BUCKET[f.status] ?? "neutral"} className="font-mono w-6 justify-center px-0">
                    {f.status}
                  </StatusBadge>
                  <span className="truncate">{f.path}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {!hasDiff && !note && <Empty>No changes.</Empty>}
        <DialogFooter>
          {buttons.map((b) => (
            <Button
              key={b.label}
              variant={b.primary ? (b.danger ? "destructive" : "default") : "outline"}
              onClick={() => settle(b.value)}
            >
              {b.label}
            </Button>
          ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
// header's git chip (which only renders the dirty dot) and the History page's
// "Uncommitted changes" card (which acts on it). A hook rather than a component
// because those two render the same fact completely differently, and the thing
// worth sharing is the fetch — one status call per epoch, no more.
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
