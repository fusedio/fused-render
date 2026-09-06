// The page shell every settings-shaped surface (Preferences, and eventually
// Claude Config/Mounts/setup-modal/new-task) wants: a full-width scroller
// with a 760px content column, so the scrollbar stays at the window edge
// while the reading measure stays capped. Replaces `.prefs-page` /
// `.prefs-page > *` (preferences.css) — that selector pair is left in place
// for Mounts.tsx/Scheduled.tsx/AddMount.tsx, which still use it directly;
// this component is the shadcn-era equivalent for pages built from the kit.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function SettingsPage({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex flex-col gap-6 overflow-y-auto p-5 px-6 pb-8 text-dense [&>*]:mx-auto [&>*]:w-full [&>*]:max-w-[760px]",
        className,
      )}
      {...props}
    />
  );
}

// Replaces `.prefs-title`: the page names itself — settings pages render
// chrome-free, no topbar.
export function SettingsTitle({ className, ...props }: ComponentProps<"h1">) {
  return (
    <h1
      className={cn("mb-3.5 text-heading tracking-[-0.01em]", className)}
      {...props}
    />
  );
}
