// THE ICON AS A BUTTON (`task_icons_enabled`). A task's status ring — or
// the face it wears in its place — opens the sidebar's own picker
// (platform/ui/IconPicker) over the task icon set — filter, shuffle, Recent,
// and Remove, which takes the face away and gives the ring back. The pick is
// stamped server-side (`setTaskIcon`) and the listing repaints it everywhere.
//
// AT ANY TIME (Akshil, 2026-11-15: "click on status icon, when it is fav
// let's use change icon"). It was a button only while the title was being
// edited (owner, 2026-11-10/11) — a door nobody found. `onlyWhileEditing`
// keeps that older gate for the heads that still stand behind the unified
// head's flag.
//
// ANY TASK, NOT ONLY A FAVOURITE (owner, 2026-11-16: the icon and the star
// are two things). Until then only a starred task had a face to press; now
// the ring of any task opens the picker, a pick is worn starred or not, and
// Remove gives the ring back — offered only while a face is drawn
// (task-icons `taskFaceShown`, StatusIcon's own rule). A favourite can so
// wear the ring; a new star gives its icon back (server-side). With the
// experiment off, or on a task with no number yet (nothing to stamp the
// pick on — the server refuses it with a 409, swallowed below), the children
// render as they are: nothing to press, no extra box.
//
// THE PICKER OUTLIVES A RENAME: its filter box takes the keyboard, the
// title input's blur SAVES the rename and ends the edit — the open picker
// stays (its state lives here, not on the button) until a pick, Escape or a
// click elsewhere. While `editing`, `onMouseDown` is swallowed so the press
// itself never blurs the title before the click lands.
//
// PORTALLED TO THE BODY: the picker is `position: fixed` against the glyph's
// viewport rect, and its hosts (the Tasks side peek, the rail's card) are
// containing blocks for fixed boxes — drawn in place it landed a peek's
// width off screen. React still bubbles its events to the host, so the
// card's focus hold sees the filter box, and the picker's own
// `data-peek-keep` keeps a press on it from reading as blank page.
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { setTaskIcon, type HttpError, type Task } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { useTaskIcons } from "@platform/lib/task-icons-flag";
import IconPicker, { type IconPickerSet } from "@platform/ui/IconPicker";
import { TASK_ICON_NAMES, TASK_ICON_SPRITE, taskFaceShown } from "./task-icons";
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
  editing = false,
  onlyWhileEditing = false,
  nested = false,
  onDone,
  onOpenChange,
  children,
}: {
  task: Pick<Task, "key" | "task_id" | "icon_slot">;
  /** True while the title input stands (the press must not blur it). */
  editing?: boolean;
  /** The older gate: a button only during a rename (flag-off heads). */
  onlyWhileEditing?: boolean;
  /** Draw a `<span role="button">` — for a host that is itself a `<button>`
   *  (the Board card, TaskStar's `nested`). That host is a DRAG SOURCE, so the
   *  span swallows its own mousedown: a press on the ring must not lift the
   *  card (owner, 2026-11-16: the ring is the picker on the Board too). */
  nested?: boolean;
  /** After the server answered — a host that reloads its own facts. */
  onDone?: () => void;
  /** The picker opened or closed — a host with something of its own on the
   *  glyph (the rail card's Archive seat) keeps it away meanwhile. Pass a
   *  STABLE function (a state setter): it is an effect dependency. */
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const iconsOn = useTaskIcons();
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
  // THE RING STILL SAYS WHAT IT SAYS. The button's name is "Choose icon",
  // which hides the ring's own label ("Done, unread") from a screen reader —
  // and on a Tasks row the ring is the only place the status is spoken (the
  // row's link names the title). Since every numbered task's ring is this
  // button now (2026-11-16), not only a favourite's face, the status rides
  // as the button's DESCRIPTION, read off the ring inside it.
  const faceId = useId();
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
          (e: unknown) => {
            // NO NUMBER YET (409): a task the store could not number in time
            // has nothing to stamp the pick on. Quiet — the ring stays a ring
            // and the press works a listing later.
            if ((e as HttpError | null)?.status === 409) return;
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
  // A button on any NUMBERED task, the experiment on (2026-11-16).
  const has = iconsOn && !!task.task_id;
  // …and Remove only while a face is drawn (StatusIcon's own rule): a ring
  // has nothing to remove.
  const worn = taskFaceShown(task.icon_slot, iconsOn);
  // THE BUTTON WENT AWAY UNDER AN OPEN PICKER (the experiment switched off):
  // the picker goes with it for good — left set, `anchor` kept the host's
  // Archive seat hidden and brought the picker back, unasked, the moment the
  // button returned (Bugbot, PR #2253). The same for a picker left behind by
  // a head that moved on to another task.
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
    removeLabel: "Remove",
    removeTitle: "Remove the icon — show the status ring",
  };
  const picker =
    anchor &&
    createPortal(
      // A PRESS IN THE PICKER IS THE PICKER'S (Akshil, 2026-11-16: "in recent
      // chats when I click on the selection icon, it opens the task as well").
      // The picker is portalled to the body, but React still bubbles its
      // events up through this button's ANCESTORS — and a Recent-chats row is
      // a `role="button"` div whose own click and Enter open the chat. Picking
      // an icon, or pressing Enter in the filter, opened it. Stopped here, in
      // the bubble phase only: a rail card's capture-phase watchers (its
      // press-inside and focus holds) still see the picker as their own.
      <div
        style={{ display: "contents" }}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        onKeyUp={(e) => e.stopPropagation()}
      >
        <IconPicker
          anchor={anchor}
          set={set}
          {...(worn ? { onRemove: () => save(null) } : {})}
          onClose={close}
          toggleSelector=".task-icon-pick"
        />
      </div>,
      document.body,
    );
  if (onlyWhileEditing && !editing) {
    return (
      <>
        {children}
        {picker}
      </>
    );
  }
  const toggle = (el: HTMLElement) => {
    const rect = el.getBoundingClientRect();
    setAnchor((cur) => (cur && cur.key === key ? null : { key, top: rect.top, left: rect.left }));
  };
  // `display: contents` (schedule.css): the ring stays the button's own grid
  // item, so nothing moves.
  const face = (
    <span id={faceId} className="task-icon-pick-face">
      {children}
    </span>
  );
  const common = {
    className: "task-icon-pick",
    "aria-label": "Choose icon",
    title: "Choose icon",
    "aria-haspopup": "dialog" as const,
    "aria-expanded": anchor !== null,
    "aria-describedby": faceId,
  };
  if (nested) {
    return (
      <>
        <span
          {...common}
          role="button"
          tabIndex={0}
          onMouseDown={(e) => {
            // No drag, no focus moving to the card under it.
            e.preventDefault();
            e.stopPropagation();
          }}
          onClick={(e) => {
            // Inside a door that is a <button>: the press is the ring's alone.
            e.preventDefault();
            e.stopPropagation();
            toggle(e.currentTarget);
          }}
          onKeyDown={(e) => {
            if (e.key !== "Enter" && e.key !== " ") return;
            e.preventDefault();
            e.stopPropagation();
            toggle(e.currentTarget);
          }}
        >
          {face}
        </span>
        {picker}
      </>
    );
  }
  return (
    <>
      <button
        type="button"
        {...common}
        onMouseDown={editing ? (e) => e.preventDefault() : undefined}
        onClick={(e) => {
          // A control inside a door (a row, a card): the press is its own.
          e.stopPropagation();
          toggle(e.currentTarget);
        }}
      >
        {face}
      </button>
      {picker}
    </>
  );
}
