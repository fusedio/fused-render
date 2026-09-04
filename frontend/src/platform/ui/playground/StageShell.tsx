// The tab's two-zone shell: model rail on the left, stage on the right, and
// inside the stage the one frame every box on it spans.
//
// On the playground the page's own <main> stops scrolling and this tab OWNS the
// viewport height — the rail and the stage scroll inside it, and the composer
// sits at the bottom of the window instead of wherever the content ran out.
//
// `pg-side` is KEPT on the rail as a hook: the AI tour
// (platform/lib/tours/ai.ts) points its first step at `.pg-side`. It carries no
// style of its own.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

/** The tour's landmark. See above — a hook, not a style. */
export const TOUR_MODEL_RAIL = "pg-side";

/** The page's `<main>` while this tab is showing (was `.pg-fill`): a flex
 *  column that owns the viewport height, `cc-main`'s 40px scroll runway cut to
 *  16px because a viewport-filling tab has nothing below the composer, and the
 *  22px gap under the head row that every other tab gets from its caption. */
export const playgroundFillClass =
  "flex flex-col overflow-y-hidden pb-4! [&>.cc-page-head]:mb-[22px]";

export function PlaygroundBody({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 items-stretch gap-[22px] pg-narrow:flex-col",
        className,
      )}
      {...props}
    />
  );
}

/** 300px of rail, and below 760px a full-width band across the top of the tab
 *  with a height cap instead — a sidebar is not a phone layout. */
export function ModelRail({ className, ...props }: ComponentProps<"aside">) {
  return (
    <aside
      className={cn(
        TOUR_MODEL_RAIL,
        "flex min-h-0 flex-[0_0_300px] flex-col gap-[14px] overflow-y-auto border-r border-[var(--border)] pr-[18px]",
        "pg-narrow:max-h-[38vh] pg-narrow:w-full pg-narrow:flex-none pg-narrow:border-r-0 pg-narrow:border-b pg-narrow:pr-0 pg-narrow:pb-3",
        className,
      )}
      {...props}
    />
  );
}

/** The stage. It is the SCROLLER (a one-shot column has no inner log, so the
 *  hero rides along with the content) and the CONTAINER the settings fold
 *  queries: the card's placement is a question about this box's width — how
 *  much gutter is left beside the column — and the viewport is a poor proxy
 *  for it, since the rail and the page chrome take ~630px before the stage gets
 *  any.
 *
 *  The four `--pg-*` numbers are declared HERE, on the container itself: a
 *  `@container` rule can never target its own container, so a var declared
 *  inside the query would simply never land and every calc() reading it would
 *  fall over. They are also the only place the fold's two beats are written
 *  down — `--pg-fade` is mirrored by `CONFIG_EXIT_MS` in the stage controls,
 *  and the two have to agree or the fade is cut off or an invisible card stays
 *  mounted.
 *
 *  `overflow-x-clip` and not `auto`: `overflow-y: auto` alone leaves x at
 *  `visible`, which the cascade then computes to `auto` — and the fold made
 *  that flash a scrollbar, because the work column's box grows by the card's
 *  footprint in one step while the margin is still gliding. Nothing is being
 *  clipped (through that whole glide the card is at opacity 0 and its rail is
 *  an empty box); this is only what keeps an empty box from growing a
 *  scrollbar. */
export function StageScroller({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "@container flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-x-clip overflow-y-auto",
        "[--pg-gap:16px] [--pg-card:240px] [--pg-glide:240ms] [--pg-fade:160ms]",
        className,
      )}
      {...props}
    />
  );
}

/** THE width story, in one place: every box on the stage — hero, work column —
 *  lives inside this frame and simply spans it. 840px cap, centered, a 32px
 *  gutter below that, and no gutter at all on a phone. */
export function StageFrame({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "mx-auto flex w-[min(840px,calc(100%_-_32px))] flex-[1_0_auto] flex-col gap-3 pg-narrow:w-full",
        className,
      )}
      {...props}
    />
  );
}

/** The hero card's PLACEMENT in the stage (the box itself is the shadcn
 *  Card's). Extra room below, because the stage's own gap reads too tight under
 *  a card this heavy and the work column needs to start as its own thing. The
 *  1px top margin keeps the Card's ring — a box-shadow drawn just OUTSIDE the
 *  box — from being clipped by the stage scroller's top edge. */
export const heroCardClass = "mx-auto mt-px mb-4 w-full";
