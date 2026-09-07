// Secondary/explanatory copy — replaces the ad-hoc `<p className="deploy-muted">`
// littered through every section (explanatory paragraphs, `describeRetention`,
// LAN device timestamps). `.deploy-muted` (fields.css:105) is color-only,
// inheriting whatever size its context sets — so is this; `SettingsPage`
// already sets `text-dense` on the page, matching the old rule's actual
// rendered size. Pass `text-meta` explicitly for the one call site
// (`.lan-devices li .deploy-muted`) that sized itself down. `as` picks the
// element (`p` by default; `span`/`div` for a context with its own wrapper).
import type { ComponentProps, ElementType } from "react";

import { cn } from "@platform/lib/utils";

export function MutedText<T extends ElementType = "p">({
  as,
  className,
  ...props
}: { as?: T } & Omit<ComponentProps<T>, "as">) {
  const Comp = as ?? "p";
  return <Comp className={cn("text-muted-foreground", className)} {...props} />;
}
