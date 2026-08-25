// The /apps hub's card: a shadcn Card around the shared AppThumb. The whole
// card is one link that opens the app (hrefFor / onAppCardClick, the same open
// rule Home's cards follow); the export action is a hover-revealed icon button
// over the thumb, and right-click goes to the hub's context menu.
import { useState } from "react";
import { DownloadIcon } from "lucide-react";
import type { AppInfo } from "@platform/lib/api";
import { exportAppFile } from "@platform/lib/appShot";
import { pushToast } from "@platform/lib/toast";
import { AppThumb } from "@platform/ui/AppThumb";
import { appRecency, hrefFor, onAppCardClick, openTargetFor } from "@platform/lib/appEntry";
import { timeAgo } from "@platform/lib/format";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Card,
  CardAction,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@platform/shadcn/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@platform/shadcn/ui/tooltip";

export function AppsCard({
  app,
  onContextMenu,
  badge,
}: {
  app: AppInfo;
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
  // Extra pill after the folder badge (the "cloned" marker on showcase cards).
  badge?: string;
}) {
  const title = app.title || app.name;
  // The same timestamp the grid SORTS by (last opened, modified standing in),
  // so a card ranked first for being opened just now labels itself with that.
  const ago = timeAgo(appRecency(app));
  const [hovered, setHovered] = useState(false);
  // The thumb element once its body iframe has painted the app — the export
  // button's crop source (appShot). null until then, so the export stages the
  // app instead of cropping an empty box.
  const [liveThumb, setLiveThumb] = useState<HTMLSpanElement | null>(null);

  return (
    <a
      href={hrefFor(app)}
      onClick={(e) => onAppCardClick(e, app)}
      onContextMenu={onContextMenu && ((e) => onContextMenu(e, app))}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={openTargetFor(app).path}
      className="group/appcard block rounded-xl no-underline outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      <Card
        size="sm"
        className={cn(
          "relative h-full pt-0 transition-[box-shadow,transform] duration-150",
          "group-hover/appcard:ring-foreground/20 group-hover/appcard:shadow-md",
        )}
      >
        {/* The thumb keeps its own `.app-pcard-thumb` box (apps.css) for the
            iframe mechanics; `!` because that rule is unlayered app CSS and
            would otherwise beat these utilities. Sits flush at the card's top
            — the border it carries for Home's title-first card is a rule here. */}
        <AppThumb
          app={app}
          hovered={hovered}
          onBodyLive={setLiveThumb}
          className="border-t-0! border-b border-b-border bg-muted!"
        />
        <CardHeader>
          <CardTitle className="truncate">{title}</CardTitle>
          <CardDescription className="flex min-w-0 items-center gap-1.5 text-xs">
            {title !== app.name && <span className="truncate">{app.name}</span>}
            {title !== app.name && ago && <span aria-hidden="true">·</span>}
            {ago && <span className="shrink-0">{ago}</span>}
          </CardDescription>
          <CardAction className="flex items-center gap-1">
            <Badge variant="outline" className="font-mono text-[10px]">
              {app.tag}
            </Badge>
            {badge && <Badge variant="secondary">{badge}</Badge>}
          </CardAction>
        </CardHeader>
        {/* Export (SPEC §43 AF-4, D391): the same action as the context menu's
            "Export App File", one visible click. A <button> inside the card's
            <a>: it must both preventDefault (or the link opens the app) and
            stopPropagation (or the click also reaches onAppCardClick). Not on
            an exported .fused card (kind "appfile", D396) — its path is the
            file itself and the export route only takes app folders. */}
        {app.kind !== "appfile" && (
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Export app file"
                  className="absolute top-2 right-2 bg-background opacity-0 transition-opacity group-hover/appcard:opacity-100 focus-visible:opacity-100"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    exportAppFile(app, liveThumb).catch((err: Error) =>
                      pushToast({
                        msg: "Could not export " + app.name + ": " + err.message,
                        tone: "error",
                      }),
                    );
                  }}
                />
              }
            >
              <DownloadIcon />
            </TooltipTrigger>
            <TooltipContent>Export as .fused app file</TooltipContent>
          </Tooltip>
        )}
      </Card>
    </a>
  );
}
