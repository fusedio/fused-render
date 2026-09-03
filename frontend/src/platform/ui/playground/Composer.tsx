// The prompt box every stage shares: input on the left, a column of buttons
// (Clear above Run) in the bottom-right corner, and — where a stage takes a
// base image — a stacked variant with an attachment floor under the prompt.
//
// The focus ring is on the BOX, not the field: the textarea is borderless and
// transparent, so `:focus-within` is what tells a reader the whole composer is
// live. Accent border plus a 3px 12% wash, the same pair every focused control
// in this app wears.
//
// `pg-composer` and `pg-send` are KEPT as hooks: the AI tour
// (platform/lib/tours/ai.ts) drives `.pg-composer`, `.pg-composer textarea` and
// `.pg-composer .pg-send`. They carry no style of their own once
// ai-playground.css is gone.
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Kbd } from "@platform/shadcn/ui/kbd";

import { BARE_BUTTON, INHERIT_FONT_FACE, INHERIT_FONT_FAMILY } from "./classes";

/** The tour's landmarks. See above — hooks, not styles. */
export const TOUR_COMPOSER = "pg-composer";
export const TOUR_SEND = "pg-send";

export const composerVariants = cva(
  `${TOUR_COMPOSER} flex gap-2 rounded-[12px] border border-solid border-[var(--border)] bg-[var(--bg-alt)] p-2 ` +
    "focus-within:border-[var(--accent)] focus-within:shadow-[0_0_0_3px_rgba(var(--accent-rgb),0.12)]",
  {
    variants: {
      /** `stacked` is the image/edit shape (D467): attached photo, prompt, then
       *  a floor holding the attach controls on the left and the Clear/Run
       *  column on the right. `row` is the other three stages: one row of
       *  [prompt | button column], with the prompt's bottom line level with the
       *  Run button beside it. */
      layout: {
        row: "items-end",
        stacked: "relative flex-col items-stretch",
      },
    },
    defaultVariants: { layout: "row" },
  },
);

export function Composer({
  layout,
  className,
  ...props
}: ComponentProps<"div"> & VariantProps<typeof composerVariants>) {
  return <div className={cn(composerVariants({ layout, className }))} {...props} />;
}

/** The field. Borderless and transparent — the box around it is the control.
 *  The max-height is ten lines of the box's own text plus its 12px of padding,
 *  the same ceiling `COMPOSER_MAX_LINES` (platform/lib/autoGrow.ts) writes as
 *  an inline height: this is the backstop that keeps the box scrollable rather
 *  than unbounded if that inline height is ever stale. */
const composerFieldBase =
  `min-w-0 flex-1 resize-none border-none bg-transparent px-1 py-1.5 text-[13.5px] leading-[1.5] text-[var(--fg)] outline-none max-h-[calc(15em_+_12px)] ${INHERIT_FONT_FACE}`;

/** A three-line floor, because a prompt worth running is usually two or three
 *  lines and a box that starts at one makes it look like a search field. It has
 *  to be a min-height rather than `rows` alone: useAutoGrow writes an inline
 *  height from the text's own scrollHeight, and min-height is what stops that
 *  from collapsing the box back to one line. */
export const composerTextareaClass = `${composerFieldBase} min-h-[calc(4.5em_+_12px)]`;

/** The ONE-LINE input (the embed stage's search) starts at the TOP. The
 *  composer's bottom alignment is a rule about a GROWING box; an input does not
 *  grow, and flex-end left its text sitting on the floor with an empty third of
 *  a card above it. */
export const composerInputClass = `${composerFieldBase} self-start`;

/** The stacked composer's prompt keeps a lane clear for the corner Clear —
 *  ALWAYS, not only while Clear is on screen: the text wraps inside this
 *  padding, so a lane that appeared with the button would rewrap the whole
 *  prompt. `flex-none` undoes the row composers' `flex: 1`, which there sizes
 *  the box across the composer; here the main axis IS the height, and a
 *  `flex-basis: 0%` overrode the inline height useAutoGrow writes. */
export const composerStackTextareaClass = `${composerTextareaClass} flex-none pr-16`;

/** Every composer's buttons stack in a column of their own beside the input:
 *  Clear above Run, 8px apart, in the composer's bottom-right corner.
 *  Deliberately NOT stretched: stretched to a three-line prompt's height the
 *  column had ~20px of slack and all of it fell between the two buttons. The
 *  column's width is Run's, the wider of the two, so Clear appearing and
 *  disappearing never changes the input's width.
 *
 *  The 72px floor holds BOTH slots whether Clear is in one of them or not
 *  (2 × 32px + the gap). Without it the embed stage's one-line input sets the
 *  composer's height, and the box would jump taller the moment a result gave
 *  it a Clear. */
export function ComposerSide({
  /** On the stacked composer's floor there is nothing beside the column, so
   *  there is no width to reserve and no jump to prevent. */
  flat,
  className,
  ...props
}: ComponentProps<"div"> & { flat?: boolean }) {
  return (
    <div
      className={cn(
        "flex flex-none flex-col items-end justify-end gap-2",
        flat ? "min-h-0" : "min-h-[72px]",
        className,
      )}
      {...props}
    />
  );
}

/** Everything on the stacked composer's floor lives in the bottom-RIGHT
 *  corner: attach, webcam, then Clear/Generate. Bottom-aligned, so the 28px
 *  pills sit on the same baseline as the 32px button beside them. */
export function ComposerFoot({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex items-end justify-end gap-2", className)} {...props} />
  );
}

/** A bare text button — the quietest control in the family. */
export function GhostButton({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        BARE_BUTTON,
        "text-xs text-[var(--fg-muted)]",
        "[&:hover:not(:disabled)]:text-[var(--fg)]",
        "disabled:cursor-default disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export const clearVariants = cva("", {
  variants: {
    /** `inline` is Clear in the button column: 32px to match the Run below it,
     *  top-aligned by an auto margin so it sits at the pair's top wherever it
     *  appears, and held 8px off the edge Run's border sits on — flush, a bare
     *  label squared off against a filled button's corner read as a mis-set
     *  pair.
     *
     *  `corner` is Clear floating in a stacked composer's top-right, out of
     *  flow: in that shape the button column is one button tall, and putting
     *  Clear back into it would add its 32px plus the gap to the composer's
     *  height for good — a row of chrome for a control that only exists once
     *  there is a result. Padding rather than offset, so the hit area grows
     *  with the room the word needs.
     *
     *  `bare` is the transcribe stage's audio row, a horizontal toolbar rather
     *  than a composer: its Clear belongs on that line with the buttons beside
     *  it, not lifted off it. */
    placement: {
      inline: "mr-2 mb-auto inline-flex h-8 flex-none items-center px-1.5",
      bare: "mr-2 mb-0 inline-flex h-8 flex-none items-center px-1.5",
      corner:
        "absolute top-2.5 right-2.5 m-0 h-auto rounded-[8px] px-2.5 py-[5px] " +
        "[&:hover:not(:disabled)]:bg-[rgba(var(--tint),0.08)]",
    },
  },
  defaultVariants: { placement: "inline" },
});

/** Clear, in one of its three placements. Always a `GhostButton` underneath —
 *  the word is the control. */
export function ClearButton({
  placement,
  className,
  ...props
}: ComponentProps<"button"> & VariantProps<typeof clearVariants>) {
  return <GhostButton className={cn(clearVariants({ placement, className }))} {...props} />;
}

/** Run / Generate / Search / Stop — this app's `.btn` vocabulary on shadcn's
 *  Button. `primary` is the neutral FILLED button (fg on bg): it reads
 *  strongest through contrast, not accent, because accent is a signal here
 *  (focus, selection) and never a fill. `secondary` is the same box, outlined.
 *
 *  Five of shadcn Button's defaults are cancelled: its 10px radius (this app's
 *  buttons are 6px), its 14px type (13px), its 3px focus ring (this app draws a
 *  2px accent OUTLINE at a 2px offset), its `transition-all` (`.btn` has none)
 *  and its 1px press nudge (`.btn` does not move). */
export const stageButtonVariants = cva(
  `${TOUR_SEND} h-8 flex-none gap-[7px] rounded-[6px] border border-solid px-3.5 text-[13px] transition-none ` +
    INHERIT_FONT_FAMILY +
    " focus-visible:ring-0 focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:outline-offset-2 " +
    "active:not-aria-[haspopup]:translate-y-0 disabled:opacity-50",
  {
    variants: {
      variant: {
        primary:
          "border-transparent bg-[var(--fg)] font-semibold text-[var(--bg)] " +
          "hover:bg-[var(--on-fg)] focus-visible:border-transparent",
        secondary:
          "border-[var(--border)] bg-transparent font-normal text-[var(--fg)] " +
          "hover:border-[var(--fg-muted)] hover:bg-transparent hover:text-[var(--fg)] focus-visible:border-[var(--border)]",
      },
    },
    defaultVariants: { variant: "primary" },
  },
);

export function StageButton({
  variant,
  className,
  ...props
}: ComponentProps<typeof Button> & VariantProps<typeof stageButtonVariants>) {
  return <Button className={cn(stageButtonVariants({ variant, className }))} {...props} />;
}

/** The Enter hint inside the Run button: a faded glyph, not a boxed chip —
 *  shadcn's `Kbd` with its plate and its 20px box taken off, because inside a
 *  filled button a second filled box is one box too many. */
export function ComposerKbd({ className, ...props }: ComponentProps<typeof Kbd>) {
  return (
    <Kbd
      className={cn(
        "ml-1.5 h-auto min-w-0 rounded-none bg-transparent px-0 text-xs leading-none text-current opacity-55",
        INHERIT_FONT_FACE,
        className,
      )}
      {...props}
    />
  );
}
