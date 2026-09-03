// A row of exclusive choices — aspect ratios, speed presets, embed modes.
//
// ONE ROW, never a ragged second line: the settings card's column is narrow, so
// overflow scrolls sideways instead of wrapping. The scrollbar is hidden — the
// cut-off chip at the edge is the affordance — and the row bleeds through the
// card's own padding and hands it back as scroll padding, so a clipped chip is
// clipped by the card's edge rather than left floating mid-padding.
//
// Plain buttons, not shadcn's `ToggleGroup`/`Toggle`: those bring a roving
// tabindex (one chip tabbable, arrow keys move focus) which is a real keyboard
// behaviour change from `main`, where every chip was its own tabbable
// `<button>` with `aria-pressed` in a `role="group"` container. Behaviour here
// must match `main` exactly; shadcn/base-ui is chrome only, not present at all.
//
// `active` may match NONE of the options (a hand-edited URL, a custom size),
// and then no chip lights: the chips are a VIEW over the underlying params,
// not the params.
import { cva } from "class-variance-authority";

import { cn } from "@platform/lib/utils";

import { INHERIT_FONT } from "./classes";

export const chipVariants = cva(
  "flex-none cursor-pointer rounded-[999px] border border-solid bg-transparent px-2.5 py-1 text-xs tabular-nums transition-none " +
    INHERIT_FONT + " " +
    "hover:border-[var(--ctl-quiet-border-hover)] hover:bg-transparent hover:text-[var(--fg)]",
  {
    variants: {
      active: {
        // Each state owns the border-color utility outright (rather than a
        // base color a variant tries to override) so the two never collide
        // in the generated stylesheet regardless of rule order.
        true: "border-[var(--accent)] text-[var(--fg)]",
        false: "border-[var(--border)] text-[var(--fg-muted)]",
      },
    },
    defaultVariants: { active: false },
  },
);

export function Chips<T extends string>({
  options,
  active,
  onPick,
  className,
}: {
  options: { value: T; label: string; title?: string }[];
  active: T | null;
  onPick: (value: T) => void;
  className?: string;
}) {
  return (
    <div
      role="group"
      className={cn(
        "flex w-auto flex-nowrap gap-1.5 overflow-x-auto rounded-none [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        "mx-[calc(var(--card-spacing,16px)*-1)] px-[var(--card-spacing,16px)] [scroll-padding-inline:var(--card-spacing,16px)]",
        className,
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === active}
          title={option.title}
          className={chipVariants({ active: option.value === active })}
          onClick={() => onPick(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
