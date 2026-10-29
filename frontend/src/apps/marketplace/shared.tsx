// Small pieces every Marketplace page shares: the AppEntry → presentational
// bindings (meta line, trust rows, the card), the notice banner and the empty
// state. The presentational parts themselves live in @platform/ui/appstore.
import type { ReactNode } from "react";
import {
  AlertTriangle,
  Globe2,
  Download,
  LayoutGrid,
  MessageSquareText,
  Repeat2,
  Rocket,
  Sparkles,
  Users,
  Wrench,
} from "lucide-react";
import { releaseDate } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { ActionPill, AppIcon, StoreCard, VerifiedMark, type ActionPillSize } from "@platform/ui/appstore";
import {
  actionFor,
  appDetailHref,
  categoryLabel,
  isMarketCopy,
  isOwnCopy,
  useChecking,
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

/** The listed app `app` remixes, by its slug: its name when the catalog
 *  has it, else the slug. Null for an app that is not a remix. */
export function remixOf(app: AppEntry, catalog: Catalog | null): { slug: string; name: string } | null {
  const slug = app.remix_of;
  if (!slug) return null;
  return { slug, name: catalog?.apps.find((a) => a.slug === slug)?.name ?? slug };
}

/** 1234 → "1.2k": a count that fits a card. */
export function compactCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}

/** The card's proof line (handoff B6): installs, lineage, remixes,
 *  contributors and the last release, each only when there is something to
 *  say. */
export function ProofLine({ app, catalog }: { app: AppEntry; catalog: Catalog | null }) {
  const remix = remixOf(app, catalog);
  const remixes = app.remix_count ?? 0;
  const people = app.contributor_count ?? 0;
  const installs = app.install_count ?? 0;
  const when = releaseDate(app.last_release_at ?? app.updated_at);
  const bits: ReactNode[] = [];
  if (remix) bits.push(<span key="of">Remix of {remix.name}</span>);
  if (installs > 0) {
    bits.push(
      <span key="in" className="mp-proof-count" title={`${installs} ${installs === 1 ? "install" : "installs"}`}>
        <Download size={12} strokeWidth={2.2} aria-hidden="true" />
        {compactCount(installs)}
      </span>,
    );
  }
  if (remixes > 0) {
    bits.push(
      <span key="rx" className="mp-proof-count" title={`${remixes} ${remixes === 1 ? "remix" : "remixes"}`}>
        <Repeat2 size={12} strokeWidth={2.2} aria-hidden="true" />
        {remixes}
      </span>,
    );
  }
  if (people > 1) {
    bits.push(
      <span key="pp" className="mp-proof-count" title={`${people} contributors`}>
        <Users size={12} strokeWidth={2.2} aria-hidden="true" />
        {people}
      </span>,
    );
  }
  if (when) bits.push(<span key="at">{when}</span>);
  if (bits.length === 0) return null;
  return (
    <span className="mp-proof">
      {bits.map((b, i) => (
        <span key={i} className="mp-proof-bit">
          {i > 0 && <span aria-hidden="true"> · </span>}
          {b}
        </span>
      ))}
    </span>
  );
}

export function pillPropsFor(app: AppEntry, action: AppAction) {
  switch (action.kind) {
    case "progress":
      return { state: "progress" as const, progress: action.progress };
    case "open":
      return { state: "open" as const };
    case "update":
      // A reviewed copy: a release is waiting and Update is the check that
      // brings it in (never the old package update).
      return isMarketCopy(app)
        ? { state: "update" as const, title: "A new reviewed release is waiting. Update brings it in." }
        : { state: "update" as const };
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

/** The AppEntry-bound pill: reads the job, derives the state, runs the action.
 *  `onAct` hears every click with the action it ran (the app page uses it to
 *  know an install was asked for from there). */
export function AppActionPill({
  app,
  size = "sm",
  emphasis,
  onAct,
}: {
  app: AppEntry;
  size?: ActionPillSize;
  emphasis?: "tinted" | "filled";
  onAct?: (action: AppAction) => void;
}) {
  const job = useJob(app.slug);
  const checking = useChecking(app.slug);
  const action = actionFor(app, job);
  if (checking) {
    return <ActionPill state="progress" progress={null} size={size} label="Checking" ariaLabel={`Checking ${app.name} for a new release`} />;
  }
  return (
    <ActionPill
      {...pillPropsFor(app, action)}
      size={size}
      emphasis={emphasis}
      onClick={() => {
        onAct?.(action);
        runPrimaryAction(app, job);
      }}
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
      return isMarketCopy(app) ? `Bring in the new release of ${app.name}` : `Update ${app.name}`;
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
  const block = app.installed?.block ?? null;
  // The card's meta: the author, then the proof line (lineage, remixes,
  // contributors, last release) when the row has any — else the old
  // category · version line.
  const proof = <ProofLine app={app} catalog={catalog} />;
  const hasProof = !!(
    app.install_count ||
    app.remix_of ||
    app.remix_count ||
    (app.contributor_count ?? 0) > 1 ||
    app.last_release_at
  );
  const meta = hasProof ? (
    <>
      {app.author?.name || "Unknown"}
      <span aria-hidden="true"> · </span>
      {proof}
    </>
  ) : (
    metaLine(app, catalog, { category: variant !== "compact" })
  );
  return (
    <StoreCard
      href={appDetailHref(app.slug)}
      onOpen={() => openDetail(app.slug)}
      icon={<AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={variant === "compact" ? 40 : 44} />}
      name={
        <>
          {app.name}
          {block?.level !== "hard" && <VerifiedMark />}
        </>
      }
      description={app.description}
      meta={meta}
      thumb={app.preview_url}
      badge={
        block
          ? block.level === "hard"
            ? "Blocked"
            : "Paused"
          : action.kind === "update"
            ? "Update"
            : action.kind === "open" && app.installed
              ? isOwnCopy(app)
                ? "Your app"
                : "Installed"
              : undefined
      }
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
