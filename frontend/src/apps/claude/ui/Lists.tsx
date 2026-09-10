// The landing page's three cross-session lists, and the tab bar over them
// (T:4266-4330 markup, T:18227-18338 behaviour, inventory 05 §B).
//
// Recent chats, published artifacts and Claude's own file checkpoints answer
// the same question about the same target — what has happened here before —
// from three different stores, so they share one block and, when more than one
// of them has something to say, one tab bar. None of them is the page's subject
// (the composer is), so the whole thing is allowed to be absent.
//
// The counts ARE the state: `null` means the read has not answered yet, which
// is NOT the same as zero — a skeleton is drawn into Recent while the sessions
// read is in flight, and it has to be on screen to be a skeleton of anything.
import { useCallback, useMemo, useRef, useState } from "react";
import "../styles/home.css";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@platform/shadcn/ui/tabs";
import type { Artifact } from "../protocol/artifacts";
import type { SessionRow } from "../protocol/types";
import { ArtifactRow } from "./ArtifactRow";
import {
  computeLists,
  type ListCounts,
  type ListName,
  nextTab,
  rememberTab,
  rememberedTab,
} from "./lists-visibility";
import { RecentRow } from "./RecentRow";
import { sessionTitle } from "./list-rows";
import { Snapshots } from "./Snapshots";
import type { SnapshotsState } from "./useSnapshots";

/** T:4269-4283 — the bar's labels, and each section's own heading. */
export const LIST_LABELS: Record<ListName, string> = {
  recent: "Recent chats",
  artifacts: "Artifacts",
  snaps: "Claude snapshots",
};

/** Content-shaped placeholder for the session list; the bars' widths are the
 *  template's (T:18227-18239). Drawn only when no row is already present — a
 *  re-read over a drawn list repaints in place, and blinking good rows into
 *  placeholder bars would make every retry look like a loss (T:18411). */
export function RecentSkeleton() {
  return (
    <>
      {[72, 58, 44].map((pct) => (
        <div className="c-skel-row" key={pct}>
          <span className="c-skel-dot" />
          <span
            className="c-skel-bar is-title"
            style={{ maxWidth: `${pct}%` }}
          />
          <span className="c-skel-bar is-sub" />
        </div>
      ))}
    </>
  );
}

export interface ListsProps {
  file: string | null;
  /** The template folder holding `agent.py`, for the snapshot plan/revert calls
   *  the rows make. Without it the snapshots panel does not mount. */
  agentDir?: string | null;
  /** `null` = the sessions read has not answered; `[]` = it answered empty,
   *  which hides the section entirely — no heading, no empty state, no error
   *  (T:18452-18477). */
  recent: SessionRow[] | null;
  /** Every page published from this target's working directory
   *  (`useArtifacts`). Same `null` vs `[]` rule. */
  artifacts?: Artifact[] | null;
  /** The file-history timeline and its read state (`useSnapshots`). */
  snaps?: SnapshotsState;
  onOpen(sessionId: string): void;
  onNavigate?(url: string): void;
  disabled?: boolean;
}

export function Lists({
  file,
  agentDir,
  recent,
  artifacts = null,
  snaps,
  onOpen,
  onNavigate,
  disabled,
}: ListsProps) {
  /**
   * Which list is showing is the BLOCK's state rather than the page's: leaving
   * for a chat and coming back keeps the tab you were on (T:18260-18265). This
   * component unmounts on the way into a chat, so the value lives in
   * `lists-visibility`'s page-scoped memory and this state only mirrors it —
   * enough to re-render on a press, never the place the answer is kept.
   */
  const [tab, setTabState] = useState<ListName>(rememberedTab);
  const setTab = useCallback((name: ListName) => {
    rememberTab(name);
    setTabState(name);
  }, []);
  const listRef = useRef<HTMLDivElement | null>(null);

  const timeline = snaps?.timeline;
  const snapsFailed = !!snaps?.failed;
  // `undefined` is "this target has no panel" (a folder) and reads as zero;
  // `null` is "mounted and reading", which keeps the panel standalone so the
  // note has somewhere to be without yet earning a tab.
  const snapCount =
    timeline === undefined
      ? 0
      : timeline === null
        ? null
        : timeline.available
          ? timeline.versions.length
          : 0;

  const counts: ListCounts = {
    recent: recent === null ? null : recent.length,
    artifacts: artifacts === null ? null : artifacts.length,
    snaps: snapCount,
    snapsFailed,
  };
  const view = computeLists(counts, tab);

  /** sessionId -> the name its chat goes by, so a checkpoint chain is titled by
   *  what the user asked for in it. Filled from the SAME rows the Recent list
   *  draws, and it races that read rather than waiting on it: a miss just falls
   *  back to the session's short id (T:18847-18867). */
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of recent || []) {
      const title = sessionTitle(s);
      if (title && title !== s.id) map.set(s.id, title);
    }
    return map;
  }, [recent]);

  /** Up/Down walk the rows and Enter opens — a keyboard's copy of the pointer's
   *  own reach down the list. */
  const onRowKeys = useCallback((ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    const rows = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>(".c-chat-row") ?? [],
    );
    const at = rows.indexOf(document.activeElement as HTMLElement);
    if (at < 0 || rows.length < 2) return;
    ev.preventDefault();
    const step = ev.key === "ArrowDown" ? 1 : rows.length - 1;
    rows[(at + step) % rows.length].focus();
  }, []);

  const recentPanel = (
    <div ref={listRef} onKeyDown={onRowKeys}>
      {recent === null ? (
        <RecentSkeleton />
      ) : (
        recent.map((session) => (
          <RecentRow
            key={session.id}
            session={session}
            file={file}
            onOpen={onOpen}
            onNavigate={onNavigate}
            disabled={disabled}
          />
        ))
      )}
    </div>
  );

  const panels: Record<ListName, React.ReactNode> = {
    recent: recentPanel,
    artifacts: (
      <div>
        {(artifacts || []).map((a) => (
          <ArtifactRow
            key={a.remote_url}
            artifact={a}
            {...(disabled ? { disabled } : {})}
          />
        ))}
      </div>
    ),
    snaps:
      agentDir && file && snaps && timeline !== undefined ? (
        <Snapshots
          agentDir={agentDir}
          file={file}
          timeline={timeline}
          failed={snapsFailed}
          error={snaps.error}
          names={names}
          onReloaded={(next) => (next ? snaps.adopt(next) : snaps.reload())}
          {...(disabled ? { disabled } : {})}
        />
      ) : null,
  };

  /** The retry is only ever present after a FAILED read — it IS the retry, and
   *  a "try again" for something that has not failed is a control for nothing
   *  (T:3395-3405). It stays on the heading line even in the tabbed dress,
   *  where the label beside it is the tab's job. */
  const heads: Partial<Record<ListName, React.ReactNode>> = {
    // The count sits on the section's OWN heading, next to the rows it counts,
    // and never on a tab: no sibling tab states its number, so the one that did
    // read as the odd tab rather than as the informative one (T:4278-4288).
    artifacts:
      !view.tabbed && artifacts && artifacts.length ? (
        <span className="c-head-count">· {artifacts.length}</span>
      ) : null,
    snaps: snapsFailed ? (
      <button
        type="button"
        className="c-snapsretry"
        onClick={() => snaps?.reload()}
      >
        try again
      </button>
    ) : null,
  };

  /**
   * ARROWS SELECT, NOT JUST FOCUS (T:18326-18338, esp. `selectListTab(next.name)`
   * *and* `focus()` at 18332-18336). Base UI's tabs move focus across the bar on
   * their own and wrap correctly, but they do not activate on focus, so
   * `aria-selected` never moved and the visible panel was unchanged — the whole
   * point of the gesture.
   *
   * Written explicitly rather than switched to Base UI's activate-on-focus mode,
   * because T's rule is not "the focused tab is the selected one": it is "walk
   * the tabs that are actually ON the bar", and `nextTab` is the function that
   * already knows which those are.
   *
   * BOUND PER TAB, with the tab's own name in the closure — T binds
   * `tab.onkeydown` on each tab for the same reason: the handler needs to know
   * where the walk starts from, and reading that back out of the event target's
   * ancestry is a DOM query for something the render already knew.
   */
  const onTabKeys = useCallback(
    (from: ListName, ev: React.KeyboardEvent<HTMLElement>) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      const next = nextTab(counts, from, ev.key === "ArrowRight" ? 1 : -1);
      if (!next) return;
      // Only once there IS somewhere to go: an arrow on a lone tab is not this
      // handler's key, and swallowing it would cost the column its scroll.
      ev.preventDefault();
      setTab(next);
      // AND THE FOCUS FOLLOWS THE SELECTION (T:18336). Without it the caret is
      // left on a tab that is no longer active, so the next arrow walks from
      // the wrong place — and a screen reader is told about a tab nobody is on.
      //
      // Found through the BAR rather than through a ref: the shadcn `TabsTrigger`
      // wrapper is a plain function component, so a ref handed to it is dropped
      // with React's own "Function components cannot be given refs" warning. T
      // reaches its tab by id for the same reason — the node, not a handle.
      const bar = ev.currentTarget?.parentElement;
      bar
        ?.querySelector<HTMLElement>(`[data-list-tab="${next}"]`)
        ?.focus({ preventScroll: true });
    },
    [counts, setTab],
  );

  const order: ListName[] = ["recent", "artifacts", "snaps"];

  if (!view.tabbed) {
    // One list is just that list under its own heading, exactly as before: a
    // tab bar with one tab is a label pretending to be a control (T:3311).
    return (
      <div className="c-lists">
        {order
          .filter((name) => view.shown[name])
          .map((name) => (
            <div className="c-list-panel" key={name}>
              <div className="c-head">
                <span>{LIST_LABELS[name]}</span>
                {heads[name]}
              </div>
              {panels[name]}
            </div>
          ))}
      </div>
    );
  }

  return (
    <Tabs
      value={view.selected}
      onValueChange={(next) => setTab(next as ListName)}
      className="c-lists is-tabbed gap-0"
    >
      {/* Only the lists that have rows get a tab: a tab for an empty list is a
          promise of rows that are not there (T:18309). No count on any tab
          (Akshil, 2026-08-24). */}
      <TabsList
        variant="line"
        aria-label="Past chats, published pages and snapshots"
        // The geometry lives in `home.css`'s `.chat-root .c-listtabs` (FIX-7),
        // which outranks the shadcn base classes — `h-auto` and `justify-start`
        // did not, so they are gone rather than left looking load-bearing.
        className="c-listtabs w-full rounded-none bg-transparent p-0"
      >
        {order
          .filter((name) => view.tabShown[name])
          .map((name) => (
            <TabsTrigger
              key={name}
              value={name}
              data-list-tab={name}
              onKeyDown={(ev: React.KeyboardEvent<HTMLElement>) =>
                onTabKeys(name, ev)
              }
              className="c-list-tab h-auto flex-none rounded-none border-0 px-0 pt-0 pb-1 text-[11px] font-semibold text-[var(--c-faint)] after:hidden data-active:bg-transparent data-active:text-[var(--c-fg)] data-active:shadow-none"
            >
              {LIST_LABELS[name]}
            </TabsTrigger>
          ))}
      </TabsList>
      {order
        .filter((name) => view.tabShown[name])
        .map((name) => (
          <TabsContent key={name} value={name} className="c-list-panel">
            {/* The bar already says the active list's name, so the section's
                own heading would say it twice — but the snapshots line is also
                where the retry sits, so what goes is the LABEL, not the row
                (T:3346-3353). */}
            <div className="c-head">
              <span className="c-head-label">{LIST_LABELS[name]}</span>
              {heads[name]}
            </div>
            {panels[name]}
          </TabsContent>
        ))}
    </Tabs>
  );
}
