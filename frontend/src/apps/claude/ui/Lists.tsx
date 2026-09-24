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
import type { Task } from "@platform/lib/api";
import { TaskRowItem } from "@shell/ScheduleTaskViews";
import { isDraftTask, taskHref, taskListKeys, upcomingEditEntry } from "@shell/tasks-lib";
import { useProjectQueueEnabled } from "../chat-prefs";
import type { Artifact } from "../protocol/artifacts";
import { ArtifactRow } from "./ArtifactRow";
import {
  computeLists,
  type ListCounts,
  type ListName,
  listTabKey,
  nextTab,
  rememberTab,
  rememberedTab,
} from "./lists-visibility";
import { paneChatUrl, taskPane } from "./list-rows";
import { SCHEDULE_URL } from "../sched/scheduled";
import { noteQueueClaim, reloadRecentTasks, seedSessionTask } from "./useRecentTasks";
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
  /** The chats about this target, as TASKS — the Tasks page's own model, drawn
   *  in the Tasks page's own row (`TaskRowItem`, .claude-design/design.md §B).
   *  Already narrowed to this pane and in the TASKS PAGE'S OWN order — status
   *  lanes, drafts at a lane's head, time inside that (`useRecentTasks`, which
   *  spends `sortForList`).
   *
   *  `null` = the listing has not answered; `[]` = it answered empty, which
   *  hides the section entirely — no heading, no empty state, no error
   *  (T:18452-18477). */
  recent: Task[] | null;
  /** Every page published from this target's working directory
   *  (`useArtifacts`). Same `null` vs `[]` rule. */
  artifacts?: Artifact[] | null;
  /** The file-history timeline and its read state (`useSnapshots`). */
  snaps?: SnapshotsState;
  onOpen(sessionId: string): void;
  /**
   * A DRAFT ROW, PRESSED — EITHER KIND (Akshil, 2026-09-15).
   *
   * The words go into the composer the reader is already looking at, and the
   * page does not move. The host resolves the text (`list-rows.draftTextOf`)
   * because a chat draft's full body has to be fetched; this list only says
   * WHICH row was pressed.
   *
   * Absent — a host that does not offer the gesture — and a draft row has no
   * press at all, which is the state it was in before either door existed.
   */
  onFillDraft?(task: Task): void;
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
  onFillDraft,
  onNavigate,
  disabled,
}: ListsProps) {
  /**
   * Which list is showing is the BLOCK's state rather than the page's: leaving
   * for a chat and coming back keeps the tab you were on (T:18260-18265). This
   * component unmounts on the way into a chat, so the value lives in
   * `lists-visibility`'s page-scoped memory and this state only mirrors it —
   * enough to re-render on a press, never the place the answer is kept.
   *
   * KEYED ON THIS MOUNT'S TARGET (batch review F3): the memory is per
   * `agentDir + file`, so two wall tiles on different files no longer answer
   * for each other, and a target change reads its OWN remembered tab back
   * rather than carrying the previous file's across. The key change is adopted
   * during render — the documented shape for "adjust state when a prop changes"
   * — so the first paint after a target switch is already on the right panel
   * instead of flashing the old one for a frame.
   */
  const memoKey = listTabKey(agentDir ?? null, file);
  const [tab, setTabState] = useState<ListName>(() => rememberedTab(memoKey));
  const seenKey = useRef(memoKey);
  if (seenKey.current !== memoKey) {
    seenKey.current = memoKey;
    setTabState(rememberedTab(memoKey));
  }
  const setTab = useCallback(
    (name: ListName) => {
      rememberTab(memoKey, name);
      setTabState(name);
    },
    [memoKey],
  );
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
   *  back to the session's short id (T:18847-18867).
   *
   *  The title is the SERVER'S now (`Task.title`, whose `title_source` records
   *  which of the four it won from), which is the same string the row beside it
   *  shows — a snapshot chain and the chat it came from can no longer be named
   *  two different things by two different title functions. */
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of recent || []) {
      if (!t.session_id || !t.title) continue;
      if (t.title !== t.session_id) map.set(t.session_id, t.title);
    }
    return map;
  }, [recent]);

  /**
   * THE ROWS' REACT KEYS — the Tasks list's own rule, borrowed rather than
   * reinvented (`tasks-lib.taskListKeys`), because these ARE the Tasks list's
   * rows.
   *
   * A message waiting in a folder's line is listed as `pending:<entry>` and is
   * re-keyed to its session id the moment the queue dispatches it. Keyed on that
   * name, the row unmounted and a new one mounted in its place, so a task the
   * reader had just skipped blinked out of Recent chats for a beat and came back
   * running (Akshil QA, 2026-09-18). The number survives the rekey; the name does
   * not. With the queue off every key is `task.key`, exactly as before.
   */
  const queueOn = useProjectQueueEnabled();
  const rowKeys = useMemo(() => taskListKeys(recent ?? [], queueOn), [recent, queueOn]);

  /** Up/Down walk the rows and Enter opens — a keyboard's copy of the pointer's
   *  own reach down the list. */
  const onRowKeys = useCallback((ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    // THE TAB STOP, WHICH IS NOT ALWAYS THE ROW. A task row that has somewhere
    // to go carries a stretched `<a>` and the row div is a plain container; a
    // row that opens IN PLACE has no href and the div is the button
    // (ScheduleTaskViews' own note on `.tasks-rowlink`). Exactly one of the two
    // per row, so a document-order query over both is still the list's order —
    // and it is the element `document.activeElement` can actually be.
    const rows = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>(
        ".tasks-rowlink, .tasks-row[tabindex]",
      ) ?? [],
    );
    const at = rows.indexOf(document.activeElement as HTMLElement);
    if (at < 0 || rows.length < 2) return;
    ev.preventDefault();
    const step = ev.key === "ArrowDown" ? 1 : rows.length - 1;
    rows[(at + step) % rows.length].focus();
  }, []);

  /**
   * ONE ROW'S PRESS, and the split is the same one `RecentRow` made: a chat
   * about THIS pane's own file becomes the current conversation in place — a
   * param write, a history load and a live-run adopt, no reload (T:18189-18197)
   * — and a chat about another file does not belong in this pane at all, so the
   * HOST is sent to that file with the session attached (T:18183-18188).
   *
   * `taskPane` is the second case's test and its path in one answer (""
   * means "this pane"), and `paneChatUrl` is the same URL the Tasks list opens
   * a task on. A task with no session yet — a never-sent draft — has no chat to
   * open either way, and gets no press at all: `TaskRowItem` draws that row
   * inert rather than lit and dead.
   *
   * A LOCKED BLOCK REFUSES EVERY ROW (P4-23): no press and no href, so the
   * stretched link cannot navigate either.
   *
   * AND AN UPCOMING ROW OPENS ITS CARD (Akshil, 2026-09-16). A DRAFT goes to the
   * New task card on its own record, through the host — `onFillDraft` is handed
   * the ROW and `list-rows.draftHref` decides the URL, so the press is the same
   * one the Tasks page makes and there is exactly one of it to learn. A
   * SCHEDULED-LATER row — not a draft, one message waiting — opens the Edit task
   * card on that message, which is the same card the Tasks page opens for it,
   * SESSION OR NO SESSION: a scheduled follow-up on a thread is still a message
   * that has not gone out, and its row's press is still the card (Bugbot,
   * PR #1180).
   *
   * The two intervening rounds are worth naming so neither comes back. Round 1
   * made a draft a door out of the app; round 2 made it no door at all ("fill
   * the composer, caret after the text") — which read as the row doing nothing
   * from any host but the landing, and left the row in the composer's OWN
   * folder behaving differently from its neighbours. One record, one card, one
   * press.
   *
   * Neither `isChatDraftTask` nor `draft_id` is consulted here: the two kinds of
   * draft differ only in how their record is addressed, which is a question this
   * list does not ask.
   *
   * …AND A QUEUED CHAT OPENS ITS CHAT (PR #1124, merged 2026-09-17). A message
   * a reader typed into a composer and that is waiting behind somebody else's
   * run in the same folder is not a draft and not a form: it has no record to
   * edit and no card to edit it in, and the conversation IS the row. It is the
   * one row with no session that still has a door, and `taskHref` is the whole
   * test for it.
   */
  const pressFor = (task: Task): { href: string | null; onPress?: () => void } => {
    if (disabled) return { href: null };
    if (isDraftTask(task)) {
      if (!onFillDraft) return { href: null };
      return { href: null, onPress: () => onFillDraft(task) };
    }
    // A CHAT THAT HAS NEVER RUN (the project queue, PR #1124). It has no
    // session id, so neither door below can open it: it is opened by the ENTRY
    // it is waiting as, through `chatUrl`'s `queued` param, and always as a
    // navigation — there is no transcript to swap into place, and the pane has
    // to mount knowing its leader (`ClaudeChat`'s `QUEUED_PARAM`). Its row is
    // the ordinary row: same height, same columns, same place in the sort, with
    // `queued` on its ring.
    //
    // ASKED BEFORE THE CARD BELOW, and that order is the whole reconciliation
    // of the two branches (merge, 2026-09-17). Such a row IS in the `queued`
    // lane with one message waiting, so `upcomingEditEntry` would answer for it
    // and send the press to the Edit card — taking the conversation away from
    // the one row whose entire content is a conversation. `taskHref` is the
    // narrower question and therefore the earlier one; it is also the order the
    // Tasks page itself reads these two in (`activate`'s thread arm before its
    // edit arm, tasks-lib `taskHref`).
    if (!task.session_id) {
      // ONE DOOR PER TASK, and it is `taskHref`'s (shell/tasks-lib): a row is a
      // chat to open only when it is `queued`, was put in the line by a CHAT
      // (`entry_origin`), and names a folder — an Upcoming one-off and a
      // scheduled FORM are also `pending:<entry>` rows and fall through to the
      // card below, as they do on the Tasks page.
      const queued = taskHref(task);
      if (queued) {
        return {
          href: queued,
          onPress: () => {
            // Seeded like every other navigating arm below, so the header on
            // the pane that opens does not wait out the whole listing.
            seedSessionTask(task);
            onNavigate?.(queued);
          },
        };
      }
    }
    // A MESSAGE WAITING TO GO OUT — WHETHER OR NOT IT HAS A THREAD BEHIND IT
    // (Bugbot, PR #1180). The interesting content of such a row is the
    // instruction that has NOT run, and the card that can change or stop it is
    // the only thing its press could mean; a transcript answers a different
    // question. `upcomingEditEntry` owns all three conditions — the lane,
    // exactly one message, which entry — and it has never asked about a
    // session, which is why the Tasks List and Board open the card for a
    // scheduled follow-up on an existing thread. Asking here as well is what
    // keeps Recent chats from being the one list that disagrees.
    //
    // NULL FALLS THROUGH, and on a row with a session that means the
    // transcript: a repeating task with past runs, or a row the schedule names
    // no pending entry for, is a thread to read rather than a message to edit.
    const entry = onNavigate ? upcomingEditEntry(task) : null;
    if (entry) {
      const href = `${SCHEDULE_URL}?edit=${encodeURIComponent(entry)}`;
      return { href, onPress: () => onNavigate?.(href) };
    }
    if (!task.session_id) return { href: null };
    const pane = taskPane(task, file);
    if (!pane) {
      return {
        href: null,
        onPress: () => {
          // THE HEADER'S IDENTITY, HANDED OVER AT THE PRESS (Akshil,
          // 2026-09-14). The chat this is about to become is entered in place,
          // and `useSessionTask` would otherwise have to wait out a whole
          // `/api/tasks` round trip — 800+ rows — before the top line could
          // stop saying `✻ Claude`. The row IS that answer, and the reader just
          // pointed at it.
          seedSessionTask(task);
          onOpen(task.session_id);
        },
      };
    }
    const href = paneChatUrl(pane, task.session_id);
    return {
      href,
      onPress: () => {
        // THE HOP IS SEEDED TOO, and on a folder pane it is the ONLY arm that
        // runs (Akshil QA, 2026-09-14). A folder's chats are all about files
        // inside it, so `taskPane` answers with a path for every row and every
        // press is this one — seeding only the in-place arm meant the header on
        // the page that opens still waited out the whole listing, which is the
        // bug the seed was written for. `seedSessionTask` stashes it for the
        // trip; see its note.
        seedSessionTask(task);
        onNavigate?.(href);
      },
    };
  };

  const recentPanel = (
    <div ref={listRef} onKeyDown={onRowKeys}>
      {recent === null ? (
        <RecentSkeleton />
      ) : (
        // The Tasks page's own frame around the Tasks page's own rows: the
        // border, the rounded end rows and the hairlines between them all hang
        // off it (styles/tasks.css), and without it the rows read as a column
        // of floating lines rather than as one list.
        <div className="tasks-list-frame">
          {recent.map((task, ix) => (
            <TaskRowItem
              key={rowKeys[ix]}
              task={task}
              {...pressFor(task)}
              // RUN NEXT'S TWO HALVES (Akshil QA, 2026-09-16). The row's own
              // skip needs somewhere to put the claim it just made and a way to
              // ask for the truth; unwired, the press was a request with no
              // visible answer. Both go to the recents' store rather than to
              // state in this component — it unmounts on the way into a chat,
              // and the claim has to outlive that (`useRecentTasks`).
              onQueued={noteQueueClaim}
              onReload={reloadRecentTasks}
            />
          ))}
        </div>
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
      // A LOCKED WALL LOCKS THE KEYBOARD PATH TOO (P4-23, batch review F7).
      // With `listsDisabled` the rows already refuse activation, but the arrows
      // still moved `aria-selected` and swapped the visible panel — which is
      // the same navigation by another gesture, and the gesture P4-23 was filed
      // to guard. Refused before the key test, so nothing about a locked bar
      // is preventDefault'ed either.
      if (disabled) return;
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      const next = nextTab(counts, from, ev.key === "ArrowRight" ? 1 : -1);
      if (!next) return;
      // Only once there IS somewhere to go: an arrow on a lone tab is not this
      // handler's key, and swallowing it would cost the column its scroll.
      ev.preventDefault();
      // AND BASE UI'S OWN ROVING-FOCUS HANDLER IS NOT ALSO RUN (batch review
      // F8). It is bound on this same tab and does not promise to honour
      // `defaultPrevented`, so without this the press moved focus TWICE — ours
      // to `next`, theirs one further along — and the selected tab and the
      // focused tab came apart on the first arrow. Ours is the walk that knows
      // which tabs are actually on the bar (`nextTab`), so it is the one that
      // should win.
      ev.stopPropagation();
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
    [counts, setTab, disabled],
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
