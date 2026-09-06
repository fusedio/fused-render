// The tab strip. Deliberately plain `<button>`s, NOT base-ui's `Tabs` —
// every tab stays independently tabbable (no roving tabindex swallowing three
// of them into one stop), and the active tab is whatever the caller's own
// state says, synced to `?tab=` the way `Preferences.tsx` already does.
// Replaces `.prefs-tabs` / `.prefs-tab` (+`.active`) / `.prefs-tabpanel`.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function PageTabs({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex gap-1 border-b border-border", className)} {...props} />
  );
}

export function PageTab({
  active,
  className,
  ...props
}: ComponentProps<"button"> & { active?: boolean }) {
  return (
    <button
      type="button"
      className={cn(
        "mr-5 border-b-2 border-transparent px-1 py-2.5 text-dense text-muted-foreground transition-colors hover:text-foreground",
        active && "border-[var(--accent)] text-foreground",
        className,
      )}
      {...props}
    />
  );
}

// Replaces `.prefs-tabpanel`: the column of sections under the active tab.
export function PageTabPanel({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-6", className)} {...props} />;
}
