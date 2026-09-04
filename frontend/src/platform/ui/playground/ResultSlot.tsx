// The result canvas, idle. Every stage draws one where its answer will land, so
// a stage that has not run yet reads as *waiting* rather than as half a page.
//
// It is a PLACEHOLDER, not a skeleton: no shimmer, no fake rows. A dashed frame
// with the capability's own sidebar glyph and one line naming what arrives
// here — the same grammar an empty folder or an empty inbox gets. Dashed and
// not solid, with no background: a solid card at this size reads as a real
// surface that failed to fill, while a dashed one reads as a slot, and it has
// to stay quieter than the two boxes that have something in them (the answer
// card and the image frame).
//
// ONE height for all five stages. An aspect-locked idle frame would avoid the
// first render's layout shift, but a 9:16 placeholder is ~1.8 column widths of
// empty dashed box — trading the void this fixes for a taller one.
//
// Built on shadcn's `Empty`, which is where the dashed centred box already
// lives; four of its defaults are cancelled here (`flex-1`, `gap-4`,
// `rounded-xl`, `text-balance`) because this slot's height, gap, corner and
// wrapping are the stage's, not the primitive's.
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@platform/lib/utils";
import { Empty } from "@platform/shadcn/ui/empty";

export function ResultSlot({
  icon,
  note,
  className,
  ...props
}: Omit<ComponentProps<"div">, "children"> & { icon: ReactNode; note: ReactNode }) {
  return (
    <Empty
      className={cn(
        "min-h-[200px] flex-none gap-2.5 rounded-[12px] border border-[var(--border)] p-6 text-wrap",
        className,
      )}
      {...props}
    >
      {/* The capability's own sidebar glyph, at a size that carries a 200px box
          — `capabilityIcon` draws at 16px, which is a section-header size, not
          a centrepiece. Dimmed, because an empty state should not be the
          loudest mark on the column. */}
      <span
        className="grid place-items-center text-[var(--fg-muted)] opacity-50 [&_svg]:h-[26px] [&_svg]:w-[26px]"
        aria-hidden="true"
      >
        {icon}
      </span>
      <p className="m-0 max-w-[34ch] text-[12.5px] leading-[1.5] text-[var(--fg-muted)]">{note}</p>
    </Empty>
  );
}
