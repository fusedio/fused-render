// A row of exclusive choices — aspect ratios, speed presets, embed modes.
//
// ONE ROW, never a ragged second line: the settings card's column is narrow, so
// overflow scrolls sideways instead of wrapping. The scrollbar is hidden — the
// cut-off chip at the edge is the affordance — and the row bleeds through the
// card's own padding and hands it back as scroll padding, so a clipped chip is
// clipped by the card's edge rather than left floating mid-padding.
//
// The GROUP is shadcn's `ToggleGroup`: it is what carries `role="group"`, the
// single-selection bookkeeping and each item's `aria-pressed`. The ITEMS are
// base UI's `Toggle` rather than shadcn's `ToggleGroupItem`, on purpose: that
// wrapper's `toggleVariants` bring a pressed fill, a `transition-all` and a 3px
// focus ring the chip would then have to fight declaration by declaration, and
// a chip that has to cancel six things to look like itself is not a chip built
// on the primitive. The chip's own look is one `cva` below.
//
// `active` may match NONE of the options (a hand-edited URL, a custom size),
// and then no chip lights: the chips are a VIEW over the underlying params,
// not the params.
import { Toggle } from "@base-ui/react/toggle";
import { cva } from "class-variance-authority";

import { cn } from "@platform/lib/utils";
import { ToggleGroup } from "@platform/shadcn/ui/toggle-group";

import { INHERIT_FONT } from "./classes";

export const chipVariants = cva(
  "flex-none cursor-pointer rounded-[999px] border border-solid border-[var(--border)] bg-transparent px-2.5 py-1 text-xs tabular-nums transition-none " +
    INHERIT_FONT + " " +
    "hover:border-[var(--ctl-quiet-border-hover)] hover:bg-transparent hover:text-[var(--fg)]",
  {
    variants: {
      active: {
        true: "border-[var(--accent)] text-[var(--fg)]",
        false: "text-[var(--fg-muted)]",
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
    <ToggleGroup
      // Controlled by the caller's params, so a click on the lit chip re-picks
      // the same value (as it always has) instead of un-pressing it.
      value={active === null ? [] : [active]}
      spacing={1.5}
      className={cn(
        "w-auto flex-nowrap overflow-x-auto rounded-none [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        "mx-[calc(var(--card-spacing,16px)*-1)] px-[var(--card-spacing,16px)] [scroll-padding-inline:var(--card-spacing,16px)]",
        className,
      )}
    >
      {options.map((option) => (
        <Toggle
          key={option.value}
          value={option.value}
          className={chipVariants({ active: option.value === active })}
          title={option.title}
          onClick={() => onPick(option.value)}
        >
          {option.label}
        </Toggle>
      ))}
    </ToggleGroup>
  );
}
