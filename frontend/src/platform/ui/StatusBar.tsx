// The bottom status bar (SPEC §36, D563/D565): a thin strip that is the LAST
// child of `#main`, so it reserves layout height and page content ends above
// it. Three always-present sections, right-aligned, left to right: Models
// (persistent state), Activity (jobs + engines) and Notifications. Each is a
// `StatusBarSection` chip whose panel is a popover above the bar.
//
// `models`/`activity`/`repoUpdates` are handed in rather than imported because
// platform may not import shell or apps (frontend/scripts/check-boundaries.mjs);
// the shell composes the three docks. Omitted, the bare download manager stands
// in `activity`'s place so this component never depends on a shell being there.
import type { ReactNode } from "react";
import DownloadManager from "@platform/ui/DownloadManager";
import { Separator } from "@platform/shadcn/ui/separator";

export default function StatusBar({
  models,
  activity,
  repoUpdates,
}: {
  models?: ReactNode;
  activity?: ReactNode;
  repoUpdates?: ReactNode;
}) {
  const items = [models, activity ?? <DownloadManager />, repoUpdates].filter((n) => n != null);
  return (
    <div
      data-slot="status-bar"
      className="flex h-6 shrink-0 items-center justify-end border-t border-border bg-background px-2 text-xs text-muted-foreground tabular-nums"
    >
      {items.map((node, i) => (
        <div key={i} className="flex h-full min-w-0 items-center">
          {node}
          {i < items.length - 1 && <Separator orientation="vertical" className="mx-1 h-2.5 self-center" />}
        </div>
      ))}
    </div>
  );
}
