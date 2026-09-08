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
import { useCallback, useRef, useState } from "react";
import "../styles/home.css";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@platform/shadcn/ui/tabs";
import type { SessionRow } from "../protocol/types";
import {
  computeLists,
  type ListCounts,
  type ListName,
} from "./lists-visibility";
import { RecentRow } from "./RecentRow";

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
  /** `null` = the sessions read has not answered; `[]` = it answered empty,
   *  which hides the section entirely — no heading, no empty state, no error
   *  (T:18452-18477). */
  recent: SessionRow[] | null;
  /** PR4 fills these; PR1 renders the panels so the tab rules are already
   *  whole. */
  artifacts?: readonly unknown[] | null;
  snaps?: readonly unknown[] | null;
  snapsFailed?: boolean;
  onOpen(sessionId: string): void;
  onNavigate?(url: string): void;
  disabled?: boolean;
}

export function Lists({
  file,
  recent,
  artifacts = null,
  snaps = null,
  snapsFailed = false,
  onOpen,
  onNavigate,
  disabled,
}: ListsProps) {
  // Which list is showing is the BLOCK's state rather than the page's: leaving
  // for a chat and coming back keeps the tab you were on (T:18260).
  const [tab, setTab] = useState<ListName>("recent");
  const listRef = useRef<HTMLDivElement | null>(null);

  const counts: ListCounts = {
    recent: recent === null ? null : recent.length,
    artifacts: artifacts === null ? null : artifacts.length,
    snaps: snaps === null ? null : snaps.length,
    snapsFailed,
  };
  const view = computeLists(counts, tab);

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

  /** Artifacts and Snapshots take their rows as props and say nothing until PR4
   *  hands them real ones; the SECTIONS exist now so the tab arithmetic above
   *  is already the finished rule. */
  const panels: Record<ListName, React.ReactNode> = {
    recent: recentPanel,
    artifacts:
      (artifacts?.length ?? 0) > 0 ? (
        <div className="c-list-empty">…</div>
      ) : null,
    snaps: snapsFailed ? (
      <div className="c-list-empty">Snapshots could not be read.</div>
    ) : (snaps?.length ?? 0) > 0 ? (
      <div className="c-list-empty">…</div>
    ) : null,
  };

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
        className="c-listtabs h-auto w-full justify-start rounded-none bg-transparent p-0"
      >
        {order
          .filter((name) => view.tabShown[name])
          .map((name) => (
            <TabsTrigger
              key={name}
              value={name}
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
            <div className="c-head">
              <span>{LIST_LABELS[name]}</span>
            </div>
            {panels[name]}
          </TabsContent>
        ))}
    </Tabs>
  );
}
