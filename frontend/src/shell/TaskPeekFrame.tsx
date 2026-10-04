// THE PAIR the side peek lives in — one flex row holding the page ("the
// frame") and the panel as DOM siblings, the panel lifted out of flow over
// the row's right edge so its slide never reflows the frame under it
// (.claude-design/task-side-peek/design.md, Layout model). Lifted out of
// Scheduled.tsx on 2026-09-20 so the app page's Tasks tab can host the very
// same peek with the very same numbers: there the frame is the WHOLE app page
// (header, tab strip and panel), not the Tasks section inside it, so the
// panel runs the full height of the content area exactly as it does on
// `/tasks` — a peek that started under a tab bar would be a different design.
//
// Two hosts, one component:
//
//   * `/tasks` (Scheduled.tsx, unscoped) renders the frame itself and hands
//     the panel in as `peek` — the two are siblings in the same tree.
//   * `/apps/<folder>?_tab=tasks` (AppPage.tsx) renders the frame around the
//     whole page and NO panel: the panel's data (the tasks, whether they have
//     loaded, the missing folders) belongs to the Scheduled mounted inside the
//     Tasks tab, so that Scheduled portals its `<TaskPeek>` into the row
//     through `useTaskPeekSlot()`. Same DOM shape either way — frame, then
//     panel, inside `.tasks-peek-host` — which is what every selector in
//     styles/task-peek.css and the store's `measureTasksBaseline` rely on.
//
// Off (flag off, wrong tab, or a page that never asked) it renders its
// children bare: the page is exactly the page it was.
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useTaskPeekLayout } from "./TaskPeek";
import { closePeek, frameClickCloses, peekGutter, refreshPeekBaseline } from "./task-peek-store";

/** The row element a portalled panel mounts into — null where no frame is
 *  hosting (the page then renders no panel and every press navigates). */
const PeekSlotContext = createContext<HTMLElement | null>(null);

/** Where a Scheduled mounted INSIDE someone else's frame puts its panel. */
export function useTaskPeekSlot(): HTMLElement | null {
  return useContext(PeekSlotContext);
}

export function TaskPeekFrame({
  peekable,
  children,
  peek,
}: {
  /** Is there a peek on this page at all (the flag, and the page's own gate)? */
  peekable: boolean;
  /** The page — what the panel slides in beside. */
  children: ReactNode;
  /** The panel, when the host holds its data itself (the `/tasks` page). A
   *  host that does not knows its Scheduled will portal one in instead. */
  peek?: ReactNode;
}) {
  const layout = useTaskPeekLayout(peekable);
  // THE MIDDLE PANE'S BASELINE (design.md, Widths v2). What is kept here is the
  // WATCH; the measurement itself is the store's (`measureTasksBaseline`), for
  // a reason worth stating where a reader would come looking for it: this
  // effect is passive, and `useTaskPeekHost`'s adoption of a `?peek=` deep link
  // is a LAYOUT effect — it runs first, so a link-opened visit would freeze a
  // baseline this observer had never had a chance to take. The store reads the
  // page itself when it is asked for a number it does not have, and this watch
  // is only the cheap path for the ordinary case.
  const frameRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!peekable) return;
    const frame = frameRef.current;
    if (!frame) return;
    const read = () => refreshPeekBaseline();
    read();
    const ro = new ResizeObserver(read);
    ro.observe(frame);
    // The page's own sections arrive after the first fetch, so the element the
    // measurement needs may not exist on the first tick.
    const mo = new MutationObserver(read);
    mo.observe(frame, { childList: true, subtree: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [peekable]);
  // The row itself, as STATE rather than a ref: a portal needs the element on
  // a render, and the first render has not made one yet. One extra paint on
  // mount, none after.
  const [host, setHost] = useState<HTMLDivElement | null>(null);

  // OFF, THE SAME TWO ELEMENTS, WEARING NOTHING. The app page flips `peekable`
  // every time the reader enters or leaves the Tasks tab, and a frame that
  // rendered a bare fragment when off and a `div > div` when on gave React a
  // different tree each time — it remounted the whole page under it, and with
  // it the Overview's `keepMounted` iframe, whose live app state is the one
  // thing that mode exists to keep (Bugbot, PR #1249). So the two wrappers
  // are always there; off, they are `display: contents` shells with neutral
  // names (styles/task-peek.css) that no peek selector can match, so a page
  // with the feature off lays out exactly as it did — and the slot is null,
  // so a Scheduled inside knows there is nowhere to put a panel.
  if (!peekable) {
    return (
      <div className="peek-shell">
        <div ref={frameRef} className="peek-shell-frame">
          <PeekSlotContext.Provider value={null}>{children}</PeekSlotContext.Provider>
        </div>
      </div>
    );
  }

  // SCROLL, DON'T FOLD (Akshil, 2026-09-15). `data-floored` used to switch on
  // at the middle pane's floor only, and the row ladder folded marks on the way
  // down to it. With the floor at a flat 500 that meant hiding meta across the
  // whole 1094→500 range — so the switch is now `tight` (frame narrower than
  // the column): under it the list's content is held at the widest row's need
  // and the pane scrolls sideways. Floored is a subset of tight (500 < any
  // baseline), so nothing the floor did is lost.
  const scrolls = layout.open && layout.tight;
  // In COVER mode the frame takes nothing off its width (rule 4): there is no
  // usable frame left at that size, so the panel is laid over it whole rather
  // than squeezing the view to a sliver. ONE number drives both halves of the
  // 200ms — the frame's width is `100% − <what the peek takes>`, and the
  // peek's own transform runs off the same value.
  const taken = layout.open && !layout.cover ? layout.width : 0;
  // THE FLOOR, handed to the stylesheet as a length (design.md, Widths v2).
  // `layout.floor` is a FRAME width — a baseline that counts the page's
  // gutters — and what the views need is the width of the content inside
  // those gutters, so the gutters come back off here rather than being
  // guessed at in CSS.
  const contentFloor = Math.max(0, Math.round(layout.floor - peekGutter()));
  return (
    <div className="tasks-peek-host" ref={setHost}>
      <div
        ref={frameRef}
        className={"tasks-frame" + (layout.instant ? " is-instant" : "")}
        // `data-floored` is the switch and `--tasks-floor` the number: below the
        // floor the views stop reflowing and scroll sideways inside the frame
        // instead (styles/task-peek.css). The toolbar is deliberately NOT under
        // it — it stays one line at every width and folds its own way.
        data-floored={scrolls ? "1" : undefined}
        // …and `data-tight` a little earlier: once the frame is narrower than
        // the column plus its gutters there are no centred margins left to give
        // and the page's side padding is just two dark bands (design.md, Polish
        // batch 3). Written off the same baseline the floor is.
        data-tight={layout.open && layout.tight ? "1" : undefined}
        style={
          {
            width: `calc(100% - ${taken}px)`,
            "--tasks-floor": `${contentFloor}px`,
          } as CSSProperties
        }
        // CLICKING BLANK FRAME CLOSES (design.md, Close triggers — and Akshil's
        // decision to keep Notion's behaviour). Everything that is a control or
        // an item does its own thing: rows and cards carry the walk's own
        // attribute, the toolbar's chips are buttons, and a menu or a dialog
        // portalled over the page is neither. What is left is page background.
        onClick={(e) => {
          if (!layout.open) return;
          // A CLICK FROM THE PANEL IS NOT A CLICK ON THE FRAME. On the app page
          // the panel is portalled here from a Scheduled mounted INSIDE this
          // frame, and React bubbles a portal's events up its component tree,
          // not the DOM's — so a press on the panel's own seam (the click a
          // drag ends with) arrived here as if it were page background and
          // closed the peek the reader had just resized (Akshil, 2026-09-21).
          // The DOM is the truth about where a click landed.
          const hit = e.target as Element | null;
          if (hit && !frameRef.current?.contains(hit)) return;
          if (frameClickCloses(hit)) closePeek();
        }}
      >
        <PeekSlotContext.Provider value={host}>{children}</PeekSlotContext.Provider>
      </div>
      {peek}
    </div>
  );
}
