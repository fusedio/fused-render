// A download, counting — one drawing for the rail row's corner AND the stage
// header's byte line, because it is one fact and two copies of it would drift
// apart the way the header's old borrowed bar already had.
//
// A RING and not a bar: in the row the slot is a square the width of an icon,
// and a 3px bar in it is four pixels of fill nobody can read. 14px, so the ring
// stands exactly where the download arrow stood and nothing changes shape when
// a pull starts.
//
// The arc is drawn with `stroke-dasharray`/`-dashoffset` on a circle rotated a
// quarter turn back, so 0% starts at twelve o'clock. An unmeasured pull — the
// first second of every one of them, and the whole of a venv build — spins a
// fixed quarter-arc instead: a ring frozen at 0 reads as a download that has
// stalled, which is the one thing it is not.
import { cn } from "@platform/lib/utils";

const RING_R = 6.5;
const RING_C = 2 * Math.PI * RING_R;

export function ProgressRing({
  /** 0–1, or null while nothing can divide — which is what spins the arc. */
  value,
  className,
}: {
  value: number | null;
  className?: string;
}) {
  const idle = value === null;
  return (
    <svg
      className={cn(
        "h-3.5 w-3.5 flex-none overflow-visible [transform:rotate(-90deg)]",
        idle && "animate-pg-ring-spin motion-reduce:animate-none",
        className,
      )}
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <circle className="fill-none stroke-[var(--border)] [stroke-width:2]" cx="8" cy="8" r={RING_R} />
      <circle
        // The poll lands once a second, so the arc walks between readings
        // rather than jumping.
        className="fill-none stroke-[var(--accent)] transition-[stroke-dashoffset] duration-200 ease-linear [stroke-linecap:round] [stroke-width:2]"
        cx="8"
        cy="8"
        r={RING_R}
        strokeDasharray={RING_C}
        strokeDashoffset={idle ? RING_C * 0.75 : RING_C * (1 - value)}
      />
    </svg>
  );
}
