// One model in the Playground's rail — a CARD carried by a fill, not an
// outline: unselected draws no visible border (a column of them was a stack of
// boxes), selected gains an accent one, and because every card already carries
// a transparent border of the same width, gaining it moves nothing.
//
// It stays a div with `role="button"`, not a <button>: the Download CTA lives
// INSIDE the card, and a button inside a button is markup browsers are free to
// mangle. The composite therefore never renders the element itself as a
// button — the caller keeps its own `role`, `tabIndex`, `onClick`, `onKeyDown`
// and `aria-pressed`.
import type { ComponentProps, ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

export const modelRowVariants = cva(
  "flex flex-col gap-0.5 rounded-[10px] border border-solid p-[15px] text-left text-[13px]",
  {
    variants: {
      /** `off` is a downloaded model nothing here runs: no pointer (there is
       *  nothing to select), a dimmed body, and a dashed border — the shape
       *  this app already reads as "present but not active". Its hover is
       *  deliberately INERT: feedback on something that cannot be clicked is
       *  the false promise the dashed border is there to withdraw. */
      state: {
        idle:
          "cursor-pointer border-transparent bg-[var(--pg-card-bg)] text-[var(--fg)] " +
          "hover:bg-[var(--pg-card-bg-hover)] " +
          "focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:outline-offset-[-2px]",
        off:
          "cursor-default border-dashed border-[var(--border)] bg-transparent text-[var(--fg-muted)] " +
          "hover:bg-transparent hover:border-[var(--border)]",
      },
      /** Selected = an accent OUTLINE and nothing else. */
      active: { true: "border-[var(--accent)]", false: "" },
    },
    defaultVariants: { state: "idle", active: false },
  },
);

export function ModelRow({
  state,
  active,
  className,
  ...props
}: ComponentProps<"div"> & VariantProps<typeof modelRowVariants>) {
  return <div className={cn(modelRowVariants({ state, active, className }))} {...props} />;
}

/** The card, on one line: nickname, then the figures and the CTA pushed right.
 *  Centred rather than baseline-aligned — one of the three is a padded button,
 *  and a shared baseline would hang it below the text beside it. */
export function ModelRowHead({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("flex min-w-0 items-center gap-2", className)} {...props} />;
}

/** The nickname. It is the part that gives way — it truncates; neither of the
 *  other two ever does. `muted` is NOT ON THIS DISK: the name greys to the
 *  weight of the figures beside it, which is what makes "you have this one"
 *  legible at a glance down the rail. */
export function ModelName({
  muted,
  className,
  ...props
}: ComponentProps<"span"> & { muted?: boolean }) {
  return (
    <span
      className={cn(
        "min-w-0 flex-1 truncate font-semibold",
        muted && "text-[var(--fg-muted)]",
        className,
      )}
      {...props}
    />
  );
}

/** Loaded right now: a green dot ahead of the name. */
export function ModelLive({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "mr-1.5 inline-block h-[7px] w-[7px] rounded-[50%] bg-[var(--success-bright)] [vertical-align:1px]",
        className,
      )}
      {...props}
    />
  );
}

/** `flex-none` is the whole point of the slot: the size is the one thing on the
 *  card that must not shrink. */
export function ModelSize({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("flex-none text-[11.5px] text-[var(--fg-muted)] tabular-nums", className)}
      {...props}
    />
  );
}

/** The full repo id, quiet under the nickname. */
export function ModelFull({ className, ...props }: ComponentProps<"span">) {
  return (
    <span className={cn("truncate text-[11.5px] text-[var(--fg-muted)]", className)} {...props} />
  );
}

/** The unsupported card's foot — its task word, and nothing else. */
export function ModelFoot({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("mt-1.5 flex items-center gap-2 text-[11.5px] text-[var(--fg-muted)]", className)}
      {...props}
    />
  );
}

/** What the model DOES, beside its size — the Hub's own task words. */
export function ModelTask({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("truncate", className)} {...props} />;
}

/** Why there is no button. WRAPS, unlike every other line on a card: it is a
 *  sentence, and truncating the one line whose whole job is to explain would
 *  leave the reader exactly where the missing card did. */
export function ModelWhy({ className, ...props }: ComponentProps<"p">) {
  return (
    <p
      className={cn("mt-1.5 mr-0 mb-0 ml-0 text-[11.5px] leading-[1.45] text-[var(--fg-muted)]", className)}
      {...props}
    />
  );
}

/** The CTA: a round, borderless hit area that only paints on hover, last on the
 *  name line. Muted and not white — it shares a line with the model's name, and
 *  at full contrast it read as the louder of the two.
 *
 *  Mid-download it stops responding but does NOT dim: the ring the caller puts
 *  in the slot is a different drawing altogether, and fading the one part of
 *  the row that is actually moving is the opposite of what it is for. */
export function ModelDownloadButton({
  className,
  children,
  ...props
}: ComponentProps<"button"> & { children?: ReactNode }) {
  return (
    <button
      className={cn(
        "inline-flex flex-none cursor-pointer items-center justify-center rounded-[999px] border-none bg-transparent p-[3px] text-[var(--fg-muted)]",
        // 14px, not MenuIcons' own 16: the glyph sits beside 13px text, and at
        // 16 it was the biggest thing on the row.
        "[&_svg]:h-3.5 [&_svg]:w-3.5",
        "[&:hover:not(:disabled)]:bg-[rgba(var(--tint),0.1)] [&:hover:not(:disabled)]:text-[var(--fg)]",
        "disabled:cursor-default",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}
