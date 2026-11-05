// THE FAVOURITE STAR LEFT OF A TASK'S ID, ALWAYS SHOWN, in every view that lists tasks
// (owner, 2026-11-05: "star everywhere") — the pane's Recent rows, the Tasks
// page's List rows, Board cards, the Detailed view, the Cards wall, the
// calendar's popover and the side peek's head. The rail card's own star
// (shell/claude-rail/RailPeek) in the same skin: lucide `Star`, filled grey
// when on, outlined when off, one accessible name with the state in
// `aria-pressed`.
//
// ONLY FOR A STARTED CHAT (`session_id`): the server keys a favourite on a
// session, so a draft or a task that never ran has nothing to star — and
// NOTHING AT ALL while the favourites switch is off (`useTaskFavourites`).
//
// THE STAR SHOWS THE PRESS AT ONCE (RailPeek's `starMark`, in miniature):
// the row's `starred` arrives on the next tasks pulse — seconds on a busy
// Mac — and a star that stays hollow after its press reads as a dead button.
// So the press is marked locally before the write leaves, corrected to the
// server's answer when it lands, dropped once the row's own `starred`
// agrees AND no write is still out (two quick presses: the first's repaint
// must not flip the star before the second lands), and dropped after STAR_MARK_MS whatever the row says (another
// window may have flipped it; the row is then the truth). A failed write
// drops the mark and says why.
//
// After the write the tasks store is poked (`announceTasksChanged`, which
// shell/App.tsx forwards to `pokeTasks` — platform may not import the shell
// store itself), so every list and the rail read the new star now.
//
// A CONTROL INSIDE A DOOR: every host is a row, card or head that opens the
// task on a press, so the star's press stops there (`stopPropagation` +
// `preventDefault`), and it sits above a row's stretched link (task-star.css).
// `nested` draws it as a `<span role="button">` for a host that is itself a
// `<button>` (the Board card), where a real button would be invalid markup.
import { Star } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { setTaskFavourite, type Task } from "@platform/lib/api";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { notify } from "@platform/lib/notifications";
import { announceTasksChanged } from "@platform/lib/tasksChanged";
import "./task-star.css";

/** How long a local mark may stand at most (rail-peek-lib's STAR_MARK_MS). */
export const STAR_MARK_MS = 6000;

/** Glyph size in px — one size in every view. */
export const STAR_SIZE = 14;

export const STAR_ON_TITLE = "Remove from favourites";
export const STAR_OFF_TITLE = "Add to favourites";

export interface TaskStarProps {
  task: Pick<Task, "key" | "session_id" | "starred">;
  /** Draw as `<span role="button">` — for a host that is a `<button>`. */
  nested?: boolean;
  className?: string;
}

/** Whether a task gets a star at all (the flag aside): a started chat. */
export function starrable(task: Pick<Task, "session_id">): boolean {
  return !!task.session_id;
}

function failWords(e: unknown): string {
  const body = (e as { body?: unknown } | null)?.body;
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string" && detail) return detail;
  return (e as Error | null)?.message || "The star could not be saved";
}

export function TaskStar(props: TaskStarProps) {
  const favsOn = useTaskFavourites();
  if (!favsOn || !starrable(props.task)) return null;
  return <TaskStarButton {...props} />;
}

function TaskStarButton({ task, nested = false, className }: TaskStarProps) {
  const starred = !!task.starred;
  const [mark, setMark] = useState<{ on: boolean; seq: number } | null>(null);
  const seq = useRef(0);
  // One write at a time per star: two fast presses must land in order.
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  // Writes still out. While any is, the mark stands even if the row already
  // agrees with it: an earlier write's repaint must not flip the star back
  // before the later press lands.
  const [pending, setPending] = useState(0);

  // The row agrees AND no write is out: the mark has done its job.
  useEffect(() => {
    if (mark && pending === 0 && mark.on === starred) setMark(null);
  }, [mark, starred, pending]);
  // …or it has had its chance to.
  const markSeq = mark?.seq ?? 0;
  useEffect(() => {
    if (!markSeq) return;
    const id = window.setTimeout(() => setMark((m) => (m && m.seq === markSeq ? null : m)), STAR_MARK_MS);
    return () => window.clearTimeout(id);
  }, [markSeq]);

  const on = mark ? mark.on : starred;
  const key = task.key;

  const press = (e: MouseEvent | KeyboardEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const want = !on;
    const mine = ++seq.current;
    setMark({ on: want, seq: mine });
    setPending((n) => n + 1);
    chain.current = chain.current
      .catch(() => {})
      .then(() => setTaskFavourite(key, want))
      .then(
        (res) => {
          if (seq.current === mine && res.starred !== want) setMark({ on: res.starred, seq: mine });
          setPending((n) => n - 1);
          announceTasksChanged();
        },
        (err) => {
          if (seq.current === mine) setMark(null);
          setPending((n) => n - 1);
          notify({ title: failWords(err), tone: "error" });
        },
      );
  };

  const cls =
    "task-star" +
    (on ? " is-on" : "") +
    (className ? " " + className : "");
  const glyph = <Star size={STAR_SIZE} fill={on ? "currentColor" : "none"} aria-hidden="true" />;
  const common = {
    className: cls,
    "aria-pressed": on,
    // One label, the state in `aria-pressed` (RailPeek's star).
    "aria-label": "Favourite",
    title: on ? STAR_ON_TITLE : STAR_OFF_TITLE,
    "data-task-star": key,
    onClick: press,
  };
  if (nested) {
    return (
      <span
        {...common}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") press(e);
        }}
      >
        {glyph}
      </span>
    );
  }
  return (
    <button type="button" {...common}>
      {glyph}
    </button>
  );
}
