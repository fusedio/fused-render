// Small pieces every Marketplace page shares: the AppEntry → presentational
// bindings (meta line, trust rows, the card), the notice banner and the empty
// state. The presentational parts themselves live in @platform/ui/appstore.
import type { ReactNode } from "react";
import {
  AlertTriangle,
  Globe2,
  LayoutGrid,
  MessageSquareText,
  Rocket,
  Sparkles,
  Wrench,
} from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { ActionPill, AppIcon, StoreCard, type ActionPillSize } from "@platform/ui/appstore";
import {
  actionFor,
  appDetailHref,
  categoryLabel,
  useJob,
  type AppAction,
  type AppEntry,
  type Catalog,
} from "@platform/lib/marketplace";
import { runPrimaryAction } from "./install-flow";

/** A glyph per known category (chips, shelf titles). */
export function CategoryGlyph({ id, size = 14 }: { id: string; size?: number }) {
  const props = { size, strokeWidth: 2, "aria-hidden": true as const };
  switch (id) {
    case "all":
      return <LayoutGrid {...props} />;
    case "starters":
      return <Rocket {...props} />;
    case "local-ai":
      return <Sparkles {...props} />;
    case "productivity":
      return <Wrench {...props} />;
    case "geospatial":
      return <Globe2 {...props} />;
    default:
      return <LayoutGrid {...props} />;
  }
}

/** "Isaac · Local AI · v1.2.0" */
export function metaLine(app: AppEntry, catalog: Catalog | null, opts: { category?: boolean } = {}): string {
  const parts = [app.author?.name || "Unknown"];
  if (opts.category !== false) parts.push(categoryLabel(catalog, app.category));
  parts.push("v" + app.version);
  return parts.join(" · ");
}

export function pillPropsFor(app: AppEntry, action: AppAction) {
  switch (action.kind) {
    case "progress":
      return { state: "progress" as const, progress: action.progress };
    case "open":
      return { state: "open" as const };
    case "update":
      return { state: "update" as const };
    case "retry":
      return { state: "retry" as const, title: action.error ?? undefined };
    case "incompatible":
      return {
        state: "disabled" as const,
        title: app.min_fused_render ? `Needs Fused Render ${app.min_fused_render} or newer` : "Not compatible with this version",
      };
    default:
      return { state: "get" as const };
  }
}

/** The AppEntry-bound pill: reads the job, derives the state, runs the action. */
export function AppActionPill({ app, size = "sm", emphasis }: { app: AppEntry; size?: ActionPillSize; emphasis?: "tinted" | "filled" }) {
  const job = useJob(app.slug);
  const action = actionFor(app, job);
  return (
    <ActionPill
      {...pillPropsFor(app, action)}
      size={size}
      emphasis={emphasis}
      onClick={() => runPrimaryAction(app, job)}
      ariaLabel={actionAria(app, action)}
    />
  );
}

function actionAria(app: AppEntry, action: AppAction): string | undefined {
  switch (action.kind) {
    case "get":
      return `Get ${app.name}`;
    case "open":
      return `Open ${app.name}`;
    case "update":
      return `Update ${app.name}`;
    case "retry":
      return `Retry installing ${app.name}`;
    default:
      return undefined;
  }
}

export function openDetail(slug: string) {
  navigateUrl(appDetailHref(slug));
}

export function MarketCard({ app, catalog, variant = "default" }: { app: AppEntry; catalog: Catalog | null; variant?: "default" | "compact" }) {
  const job = useJob(app.slug);
  const action = actionFor(app, job);
  return (
    <StoreCard
      href={appDetailHref(app.slug)}
      onOpen={() => openDetail(app.slug)}
      icon={<AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={variant === "compact" ? 40 : 44} />}
      name={app.name}
      description={app.description}
      meta={metaLine(app, catalog, { category: variant !== "compact" })}
      thumb={app.preview_url}
      badge={action.kind === "update" ? "Update" : action.kind === "open" && app.installed ? "Installed" : undefined}
      variant={variant}
      action={<AppActionPill app={app} />}
    />
  );
}

export function Notice({ tone = "info", children, action }: { tone?: "info" | "warn" | "error"; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={"mp-notice is-" + tone} role={tone === "error" ? "alert" : "status"}>
      <AlertTriangle size={15} strokeWidth={2} aria-hidden="true" className="mp-notice-glyph" />
      <span className="mp-notice-text">{children}</span>
      {action}
    </div>
  );
}

export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: ReactNode; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="mp-empty">
      <div className="mp-empty-glyph" aria-hidden="true">
        {icon ?? <MessageSquareText size={22} strokeWidth={1.8} />}
      </div>
      <div className="mp-empty-title">{title}</div>
      {body && <div className="mp-empty-body">{body}</div>}
      {action && <div className="mp-empty-action">{action}</div>}
    </div>
  );
}

/** "2 hours ago" for an ISO timestamp (the offline banner). */
export function ago(iso: string | undefined): string {
  if (!iso) return "earlier";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "earlier";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.round(m)} minutes ago`;
  const h = m / 60;
  if (h < 36) return `${Math.round(h)} hour${Math.round(h) === 1 ? "" : "s"} ago`;
  const d = h / 24;
  return `${Math.round(d)} days ago`;
}
