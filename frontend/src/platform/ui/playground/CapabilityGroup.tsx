// A capability section in the Playground's model rail: a <details>, its summary
// the icon + task-word header, with the default disclosure triangle replaced by
// a caret on the right so the icon owns the left edge.
//
// It stays a NATIVE <details>: the toggle semantics, the keyboard handling and
// the `open` attribute are the browser's, and a Collapsible would be a
// re-implementation of all three for the sake of a class name.
//
// Spacing between sections is a MARGIN, not flex gap: Chrome keeps <details>
// block-level whatever display it is handed, so a gap on the section itself
// silently does nothing.
import type { ReactNode } from "react";

import { cn } from "@platform/lib/utils";

/** The caret, the marker suppression and the muted→fg hover, on the summary.
 *  Exported so a caller that needs its own <summary> markup still gets the
 *  exact head. The caret's open state is driven by `group-open`, so the
 *  <details> above it must carry `group`. */
export const capabilityGroupHeadClass =
  "flex cursor-pointer list-none select-none items-center gap-2 text-[var(--fg-muted)] hover:text-[var(--fg)] " +
  "[&::-webkit-details-marker]:hidden " +
  // Immediately right of the title, not flushed to the rail's far edge: the
  // caret belongs to the word it opens.
  "after:-ml-0.5 after:h-1.5 after:w-1.5 after:flex-none after:border-r-[1.25px] after:border-b-[1.25px] " +
  "after:border-[var(--fg-muted)] after:content-[''] after:[transform:rotate(-45deg)] " +
  "after:transition-[transform] after:duration-150 after:ease-[ease] " +
  "group-open:after:[transform:rotate(45deg)]";

export function CapabilityGroup({
  open,
  icon,
  title,
  className,
  children,
}: {
  /** Native `<details open>`; omitted means collapsed, as the browser has it. */
  open?: boolean;
  icon: ReactNode;
  title: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <details
      open={open}
      className={cn(
        "group",
        // 28px between sections — wider than the 8px between a heading and its
        // own cards by enough to read as a different order of gap.
        "[&:not(:first-child)]:mt-7",
        "[&>*:not(summary)]:mx-0 [&>*:not(summary)]:mt-2 [&>*:not(summary)]:mb-0",
        className,
      )}
    >
      <summary className={capabilityGroupHeadClass}>
        {/* Bigger than capabilityIcon's own 16px: these are the rail's only
            headings, so the glyph carries part of the weight the old
            tracked-out caps did. */}
        <span className="inline-flex flex-none [&_svg]:h-[18px] [&_svg]:w-[18px]">{icon}</span>
        {/* The label as written — "Text generation", "Not supported" — instead
            of tracked-out caps. */}
        <span className="text-[14px] font-semibold">{title}</span>
      </summary>
      {children}
    </details>
  );
}

/** A section's reason for being empty or ruled out — visible with its reason,
 *  never hidden. */
export function CapabilityGroupNote({
  className,
  children,
}: {
  className?: string;
  children?: ReactNode;
}) {
  return (
    <p className={cn("m-0 text-xs leading-[1.45] text-[var(--fg-muted)]", className)}>
      {children}
    </p>
  );
}
