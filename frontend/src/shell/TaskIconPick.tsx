// THE ICON AS A BUTTON, while a title is being edited (`task_icons_enabled`,
// owner 2026-11-10: "click edit title and click on icon should … change
// that"; 2026-11-11: "instead of giving a random one, we let user pick by
// showing a menu similar to the one we show for project icon selection in
// the sidebar"): wraps the head's StatusIcon and, only during the rename,
// makes it open the sidebar's own picker (platform/ui/IconPicker) over the
// task icon set — filter, shuffle, Recent, and Reset for the face the task's
// number gives it. The pick is stamped server-side (`setTaskIcon`) and the
// listing repaints it everywhere.
//
// ONLY A FAVOURITE HAS A SLOT (the listing hands a face to starred tasks
// alone), so outside the rename, or with the experiment off, or on a task
// that is not starred, the children render as they are: nothing to press,
// no extra box.
//
// THE PICKER OUTLIVES THE RENAME: its filter box takes the keyboard, the
// title input's blur SAVES the rename and ends the edit, and the button goes
// with it — the open picker stays (its state lives here, not on the button)
// until a pick, Escape or a click elsewhere. `onMouseDown` is swallowed so
// the press itself never blurs the title before the click lands.
//
// PORTALLED TO THE BODY: the picker is `position: fixed` against the glyph's
// viewport rect, and both hosts (the Tasks side peek, the rail's card) are
// containing blocks for fixed boxes — drawn in place it landed a peek's
// width off screen. React still bubbles its events to the host, so the
// card's focus hold sees the filter box, and the picker's own
// `data-peek-keep` keeps a press on it from reading as blank page.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { setTaskIcon, type Task } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { useTaskIcons } from "@platform/lib/task-icons-flag";
import IconPicker, { type IconPickerSet } from "@platform/ui/IconPicker";
import { TASK_ICON_NAMES, TASK_ICON_SPRITE } from "./task-icons";
import { pokeTasks } from "./tasksPulse";

// The whole set, in slot order; a cell's id is the glyph's name.
const CELLS = TASK_ICON_NAMES.map((name) => ({ id: name, title: name.replace(/-/g, " ") }));
const SLOT_OF = new Map(TASK_ICON_NAMES.map((name, slot) => [name, slot]));

function glyph(name: string) {
  return (
    <svg className="size-[18px]" fill="currentColor" aria-hidden="true" focusable="false">
      <use href={`${TASK_ICON_SPRITE}#ti-${name}`} />
    </svg>
  );
}

export function TaskIconPick({
  task,
  editing,
  onDone,
  onOpenChange,
  children,
}: {
  task: Pick<Task, "key" | "icon_slot">;
  /** True while the title input stands. */
  editing: boolean;
  /** After the server answered — a host that reloads its own facts. */
  onDone?: () => void;
  /** The picker opened or closed — a host with something of its own on the
   *  glyph (the rail card's Archive seat) keeps it away meanwhile. Pass a
   *  STABLE function (a state setter): it is an effect dependency. */
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const iconsOn = useTaskIcons();
  const favsOn = useTaskFavourites();
  // Where the picker opened, AND FOR WHICH TASK: a head that swaps to another
  // chat under an open picker (the rail card's next ring, the side peek's
  // next row) must not hand the next pick to the newcomer.
  const [at, setAnchor] = useState<{ key: string; top: number; left: number } | null>(null);
  const key = task.key;
  const anchor = at && at.key === key ? at : null;
  // One write at a time, in the order pressed: the shuffle button stays up to
  // be pressed again, and two fast draws answered out of order would keep the
  // earlier face (the star's rule, platform/ui/TaskStar).
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const save = useCallback(
    (slot: number | null) => {
      chain.current = chain.current
        .catch(() => {})
        .then(() => setTaskIcon(key, slot))
        .then(
          () => {
            pokeTasks();
            onDone?.();
          },
          () => {
            // The face stays as it was; the icon is still there to press.
            notify({ title: "The icon could not be saved", tone: "error" });
          },
        );
    },
    [key, onDone],
  );
  const close = useCallback(() => setAnchor(null), []);
  const open = anchor !== null;
  // Told on open, and taken back on close OR unmount (a card that goes away
  // under an open picker must not leave its host believing one is up).
  useEffect(() => {
    if (!open) return;
    onOpenChange?.(true);
    return () => onOpenChange?.(false);
  }, [open, onOpenChange]);
  // The glyph is drawn (StatusIcon's own rule): both switches on, and a slot.
  const has = iconsOn && favsOn && task.icon_slot !== null && task.icon_slot !== undefined;
  // THE GLYPH WENT AWAY UNDER AN OPEN PICKER (unstarred in another window, a
  // switch flipped): the picker goes with it for good — left set, `anchor`
  // kept the host's Archive seat hidden and brought the picker back, unasked,
  // the moment the task was starred again (Bugbot, PR #2253). The same for a
  // picker left behind by a head that moved on to another task.
  const stale = at !== null && (!has || at.key !== key);
  useEffect(() => {
    if (stale) setAnchor(null);
  }, [stale]);
  if (!has) return <>{children}</>;
  const set: IconPickerSet = {
    label: "Icons",
    recent: "task",
    cells: CELLS,
    render: glyph,
    onPick: (name) => {
      const slot = SLOT_OF.get(name);
      if (slot !== undefined) save(slot);
    },
    removeLabel: "Reset",
    removeTitle: "Back to this task's own icon",
  };
  const picker =
    anchor &&
    createPortal(
      <IconPicker
        anchor={anchor}
        set={set}
        onRemove={() => save(null)}
        onClose={close}
        toggleSelector=".task-icon-pick"
      />,
      document.body,
    );
  if (!editing) {
    return (
      <>
        {children}
        {picker}
      </>
    );
  }
  return (
    <>
      <button
        type="button"
        className="task-icon-pick"
        aria-label="Choose icon"
        title="Choose icon"
        aria-haspopup="dialog"
        aria-expanded={anchor !== null}
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          setAnchor((cur) => (cur && cur.key === key ? null : { key, top: rect.top, left: rect.left }));
        }}
      >
        {children}
      </button>
      {picker}
    </>
  );
}
