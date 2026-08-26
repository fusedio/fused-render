// "Current apps" — the sidebar section above Bookmarks (D487): EVERY workspace
// app (<fused_dir>/local/<slug>) that still has a task not filed away, newest
// activity first, uncapped. A row opens the app's PAGE (`/apps/<slug>`,
// shell/AppPage.tsx, D488) — the one door that page has; its cross archives
// every task under it, which is the one gesture that takes an app off this list.
//
// The ORDER is a sequence per app, not the recency it is seeded from
// (current-apps-lib.ts): a row moves only when the user drags it, so new work in
// an app already listed does not reshuffle the list under the cursor. The store
// is the module-level `appOrder` below, hydrated from localStorage at import and
// written back whenever the displayed order changes, so a drag survives a reload
// and the next launch.
//
// Fed by the task pulse store (useTasksPulseRows) rather than a poll of its
// own: the sidebar and the Tasks page already share ONE /api/tasks(/pulse)
// reader and a second one is the double-poll that store exists to prevent.
// `project` rides the compact row for exactly this reader.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { archiveTask, getConfig } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { opensElsewhere } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import {
  appPageTab,
  appPageUrl,
  assignSequences,
  bySequence,
  currentApps,
  moveSlug,
  orderedSlugs,
  parseSavedOrder,
  reorderTo,
  slugFromAppPath,
  type AppOrder,
  type CurrentApp,
} from "@shell/current-apps-lib";

// Read once per mount; the sidebar remounts per navigation, which is cheap
// enough (Scheduled.tsx reads config the same way). Cached at module level so
// the row list does not blink empty on every remount while the config
// round-trips. `fused_dir`, not `home`: the workspace root honours
// FUSED_RENDER_DIR, and a root built from home would list nothing under it.
let knownRoot = "";

export const ORDER_KEY = "fused-render:current-apps-order";

// The displayed order: a module-level Map (it outlives the sidebar's
// per-navigation remount — pushState routing) hydrated from localStorage at
// import, so the order is already in hand before the first render and there is
// no window where recency wins a race against what the user dragged.
//
// Every store touch sits inside a try: a blocked or full store costs the saved
// order, not the section. Reading a JSON array of slugs (top first) rather than
// the sequence numbers themselves keeps the stored shape the one thing that
// matters — nothing on disk has to agree with a numbering scheme this module is
// free to change.
const appOrder: AppOrder = new Map();

function readSavedOrder(): string[] {
  try {
    return parseSavedOrder(localStorage.getItem(ORDER_KEY));
  } catch {
    return [];
  }
}

// Write only a CHANGED order. The effect that calls this runs on every pulse
// (the store hands out a fresh `rows` array each poll, so the memo rebuilds),
// and an unconditional write there is what made a second tab dangerous: it
// would re-save its own pre-drag order over the one this tab just dragged, on
// its next tick (Bugbot, 2026-08-26). With the guard, a tab that has adopted
// the new order writes nothing, which is also what stops the adopt below from
// ping-ponging between two tabs forever.
function saveOrder(slugs: string[]): void {
  try {
    const next = JSON.stringify(slugs);
    if (localStorage.getItem(ORDER_KEY) === next) return;
    localStorage.setItem(ORDER_KEY, next);
  } catch {
    // A blocked store just means the order lasts as long as the page does.
  }
}

/** Take `slugs` as the whole order, replacing what this page held. Empty is NOT
 *  an order — a missing or cleared key must leave the live order alone rather
 *  than flattening it. A slug the incoming list does not mention is genuinely
 *  news to the tab that wrote it, so it gets a fresh sequence and goes on top;
 *  that resolves in ONE round, because assignment only ever adds (see
 *  current-apps-lib.ts) and so the other tab's next read agrees. */
function adoptSavedOrder(slugs: string[]): void {
  if (!slugs.length) return;
  appOrder.clear();
  reorderTo(appOrder, slugs);
}

// Mounted sections, so another tab's drag can repaint this one.
const orderListeners = new Set<() => void>();

try {
  adoptSavedOrder(readSavedOrder());
  // `storage` fires only in OTHER documents, which makes it exactly the
  // cross-tab channel — the same wiring App.tsx uses to hear the chat's
  // activity stamp. Without it the two tabs disagree until a reload.
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== ORDER_KEY) return;
    adoptSavedOrder(parseSavedOrder(e.newValue));
    for (const listener of orderListeners) listener();
  });
} catch {
  // No store and no window: the order lives and dies with this page.
}

function useFusedDir(): string {
  const [root, setRoot] = useState(knownRoot);
  useEffect(() => {
    if (knownRoot) return;
    getConfig().then(
      (c) => {
        knownRoot = c.fused_dir || "";
        setRoot(knownRoot);
      },
      () => {},
    );
  }, []);
  return root;
}

interface RowDragProps {
  onDragStart: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}

function CurrentAppRow({
  app,
  active,
  drag,
}: {
  app: CurrentApp;
  active: boolean;
  drag: RowDragProps;
}) {
  const [busy, setBusy] = useState(false);
  const href = appPageUrl(app.slug);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // `active` is slug-only (the row lights up on either tab); the destination
    // is the OVERVIEW, so from the Tasks tab the click still goes — it is how
    // the sidebar gets back to the running app. Only a click that would land
    // exactly where the page already is stays a no-op.
    if (!active || appPageTab(location.search) !== "overview") navigateUrl(href);
  };
  const onArchive = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      // Every task under the app, tolerant of one failing: the row leaves the
      // list only once the pulse re-reads, so a half-archived app simply stays
      // with fewer tasks rather than lying about being gone.
      await Promise.allSettled(app.taskKeys.map((k) => archiveTask(k)));
    } finally {
      setBusy(false);
      pokeTasks();
    }
  };
  const n = app.taskKeys.length;
  const tip = `${app.dir} — ${n} ${n === 1 ? "task" : "tasks"}${app.running ? ", running" : ""}`;
  return (
    <div
      className={"bookmark-row current-app-row" + (active ? " active" : "")}
      title={tip}
      draggable
      {...drag}
    >
      <span className="bookmark-glyph current-app-glyph" aria-hidden="true">
        {app.running ? <span className="sidebar-rail-dot is-running" /> : "▣"}
      </span>
      <a
        className="bookmark-name"
        href={href}
        draggable={false}
        aria-current={active ? "page" : undefined}
        onClick={onOpen}
      >
        {app.slug}
      </a>
      <span className="bookmark-actions">
        <button
          className="icon-btn delete-btn current-app-archive"
          title={`Archive ${n === 1 ? "its task" : `all ${n} tasks`}`}
          aria-label={`Archive all tasks for ${app.slug}`}
          disabled={busy}
          onClick={onArchive}
        >
          ✕
        </button>
      </span>
    </div>
  );
}

export default function CurrentAppsSection() {
  const rows = useTasksPulseRows();
  const fusedDir = useFusedDir();
  // A drop mutates `appOrder`, which React cannot see; this counter is what
  // turns that mutation into a render. The pulse's own ticks re-run the memo
  // anyway, so without it a drag would land only on the next poll.
  const [orderEpoch, setOrderEpoch] = useState(0);
  const apps = useMemo(() => {
    const found = currentApps(rows, fusedDir);
    // Assigning during render is safe because it is idempotent: an app that
    // already has a sequence keeps it, so a double-invoked render (StrictMode)
    // or a re-run on the same rows cannot renumber anything.
    assignSequences(appOrder, found);
    return bySequence(found, appOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orderEpoch is the drag signal
  }, [rows, fusedDir, orderEpoch]);
  // Write the order back after it settles — a drag, or an app nothing had seen
  // before. Guarded on a non-empty list for the same reason `assignSequences`
  // is: the first render (and any moment the pulse has not loaded) has no apps,
  // and saving from that state would write an order derived from nothing.
  // `saveOrder` itself skips an unchanged order, so running this on every pulse
  // costs one read.
  useEffect(() => {
    if (apps.length) saveOrder(orderedSlugs(appOrder));
  }, [apps]);

  // Repaint when another tab drags. The adopt already happened at the module
  // listener; this is only the render half of it.
  useEffect(() => {
    const bump = () => setOrderEpoch((n) => n + 1);
    orderListeners.add(bump);
    return () => {
      orderListeners.delete(bump);
    };
  }, []);

  // Which row is the page on screen. Read at render: the sidebar remounts on
  // every navigation (App.tsx), so a stale read cannot outlive a route change.
  const onSlug = slugFromAppPath(location.pathname);

  // ---- reordering by drag ----------------------------------------------------
  // A flat list, so the only question a drop asks is "above or below this row",
  // answered by the row's own midpoint. Deliberately NOT the bookmarks tree's
  // machinery (BookmarksSection): no folders, no subtree guard, no drop-into.
  // The zone and fade CLASSES are that section's, though — the rows already
  // carry `bookmark-row`, so `.dragging` / `.drag-above` / `.drag-below` are
  // painted by sidebar.css with nothing new added.
  const draggedRef = useRef<string | null>(null);
  // Cleared by query rather than by ref: the row that started the drag may
  // already be detached when the drop lands, and any row still on screen must
  // not keep wearing a class from a finished gesture.
  const clearDrag = () => {
    const marks = ["drag-above", "drag-below", "dragging"];
    const sel = marks.map((m) => `.current-app-row.${m}`).join(", ");
    document.querySelectorAll(sel).forEach((el) => el.classList.remove(...marks));
  };
  const isBelow = (e: React.DragEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY > r.top + r.height / 2;
  };
  const dragProps = (slug: string): RowDragProps => ({
    onDragStart: (e) => {
      draggedRef.current = slug;
      e.currentTarget.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", slug); // Firefox needs a payload to start
    },
    onDragOver: (e) => {
      const from = draggedRef.current;
      if (from === null || from === slug) return;
      e.preventDefault(); // required to allow a drop
      e.dataTransfer.dropEffect = "move";
      const after = isBelow(e);
      e.currentTarget.classList.toggle("drag-above", !after);
      e.currentTarget.classList.toggle("drag-below", after);
    },
    onDragLeave: (e) => e.currentTarget.classList.remove("drag-above", "drag-below"),
    onDrop: (e) => {
      const from = draggedRef.current;
      // Reset BEFORE the re-render: it detaches the source row, and Chrome
      // skips dragend on a removed element (the lesson BookmarksSection
      // records at its own drop handler).
      draggedRef.current = null;
      clearDrag();
      if (from === null || from === slug) return;
      e.preventDefault();
      const order = apps.map((a) => a.slug);
      reorderTo(appOrder, moveSlug(order, from, slug, isBelow(e)));
      setOrderEpoch((n) => n + 1);
    },
    onDragEnd: () => {
      // Fires on an Escape-cancelled drag too — the universal cleanup.
      draggedRef.current = null;
      clearDrag();
    },
  });

  const render = useCallback(
    (app: CurrentApp) => (
      <CurrentAppRow
        key={app.dir}
        app={app}
        active={app.slug === onSlug}
        drag={dragProps(app.slug)}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dragProps closes over `apps`
    [onSlug, apps],
  );
  // The + opens the /apps composer in a modal (D489). The section therefore
  // ALWAYS renders now — it first hid itself with zero current apps (D487),
  // but a door to "make one" is exactly what an empty desk wants, and hiding
  // the heading would hide the door with it. Empty = heading + plus, no rows.
  const [composing, setComposing] = useState(false);
  return (
    <div className="sidebar-section sidebar-current-apps">
      <div className="sidebar-heading current-apps-heading">
        Current apps
        <button
          type="button"
          className="icon-btn current-apps-add"
          title="New app"
          aria-label="New app"
          onClick={() => setComposing(true)}
        >
          +
        </button>
      </div>
      {apps.map(render)}
      {composing && (
        // The SAME composer /apps and /home show (apps/builder/HomeHero.tsx):
        // it names, scaffolds and navigates into the new app's chat itself,
        // and that navigation remounts the sidebar (App.tsx), which is what
        // unmounts this modal. `onCreated` closes it for the case where the
        // composer stays put (no chat run started).
        <Modal
          title="New app"
          onClose={() => setComposing(false)}
          width={640}
          dialogClassName="current-apps-compose"
          // The composer arrives with its own skin (chips, pickers, the round
          // send button); the chassis' form vocabulary would re-style every
          // button in it. Owner: "we should not be redesigning anything".
          plainBody
        >
          <HeroComposer onCreated={() => setComposing(false)} />
        </Modal>
      )}
    </div>
  );
}
