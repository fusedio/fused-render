// Home view — lives at "/" itself (old /view/_home sentinel redirects here)
// and is the app's launch landing. Structure, top to bottom:
//   1. Hero card — headline + blurb + the prompt composer (describe an app,
//      haiku names it, POST /api/apps/new scaffolds it). The structured
//      NewAppPanel lives in @apps/builder and opens from /apps.
//   2. Doorways — equal cards for the app's entry points: file explorer,
//      apps hub (the Fused workspace dir), templates manager, and — once
//      the bundled learn mount is ready — the Learn lessons.
//   3. Recent — the 10 most recently updated apps (GET /api/apps), sorted
//      once per fetch so the grid never reorders under interaction; keys
//      are stable paths.
import { useEffect, useState, type ReactNode } from "react";
import { appSourceLabel, getApps } from "@platform/lib/api";
import type { AppInfo, Config } from "@platform/lib/api";
import { entryOf, hrefFor, onAppCardClick } from "@platform/lib/appEntry";
import { navigate, navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { basename } from "@platform/lib/format";
import { useLearnMountReady } from "@platform/lib/hooks";
import { HomeHero } from "@apps/builder/HomeHero";
import { timeAgo } from "@apps/builder/AppPreviewCard";
import { openLearn } from "@apps/learn";
import { SkeletonLines } from "@platform/ui/Skeleton";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

// Returns the load state plus a reload: bumping the nonce refetches while the
// previous data stays on screen, so a refresh never flashes the loading state.
function useLoad<T>(fetcher: () => Promise<T>): [Loaded<T>, () => void] {
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    fetcher().then(
      (data) => alive && setState({ status: "ok", data }),
      (e: Error) => alive && setState({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce]);
  return [state, () => setNonce((n) => n + 1)];
}

// Section heading: mono eyebrow + count, hairline rule filling the middle,
// optional trailing action link. The mono face is the shell's existing code
// voice (listings, paths), so the labels read as part of the tool, not chrome.
function SectionRule({
  label,
  action,
}: {
  label: string;
  action?: ReactNode;
}) {
  return (
    <div className="home-rule">
      <span className="home-rule-label">{label}</span>
      <span className="home-rule-line" aria-hidden="true" />
      {action}
    </div>
  );
}

// One doorway card per top-level entry point. The glyph square borrows the
// listing's file-icon hues (set inline as `color`; fill/border derive from it
// via color-mix) so the three cards are distinguishable at a glance without
// inventing new palette.
function Doorway({
  hue,
  title,
  desc,
  onClick,
  glyph,
  titleAttr,
}: {
  hue: string;
  title: string;
  desc: string;
  onClick: () => void;
  glyph: ReactNode;
  titleAttr?: string;
}) {
  return (
    <button type="button" className="home-door" onClick={onClick} title={titleAttr}>
      <span className="home-door-glyph" aria-hidden="true" style={{ color: hue }}>
        {glyph}
      </span>
      <span className="home-door-title">{title}</span>
      <span className="home-door-desc">{desc}</span>
      <span className="home-door-arrow" aria-hidden="true">
        →
      </span>
    </button>
  );
}

// One row in the Recent list: name, tag, last-used — no icon. Opens through the
// same shared rule as the /apps cards (appEntry.openTargetFor), rather than the
// inline copy it used to carry: that copy had already drifted from the hub's,
// and a row and a card for the same app must not open different things.
function RecentRow({ app }: { app: AppInfo }) {
  const title = app.title || app.name;
  // openApp, not a local copy of the rule: this row had its own inline version
  // and it diverged — it sent a Claude Science figure to its own directory (a
  // folder named after a UUID) while the hub's card opened the file (D212).
  return (
    <a
      className="home-recent"
      href={hrefFor(app)}
      onClick={(e) => onAppCardClick(e, app)}
      title={entryOf(app) ?? app.path}
    >
      <span className="home-recent-name">{title}</span>
      {appSourceLabel(app.source) && (
        <span className="app-source">{appSourceLabel(app.source)}</span>
      )}
      <span className="home-app-tag">{app.tag}</span>
      <span className="home-recent-when">{timeAgo(app.updated_at) ?? "—"}</span>
    </a>
  );
}

export default function Home({ config }: { config: Config }) {
  const [apps, reloadApps] = useLoad(getApps);
  // The boot-time config snapshot's learn_mount_ready is stale in both
  // directions (see useLearnMountReady) — without the bounded re-poll the
  // Learn doorway would essentially never appear.
  const learnMountReady = useLearnMountReady(config.learn_mount_ready);


  return (
    <div className="home-page">
      <div className="home-inner">
        {/* Hero card: wordmark + headline + the prompt composer (shared with
            /apps via HomeHero). The structured NewAppPanel lives on /apps,
            and file browsing has its doorway card below. */}
        <HomeHero onCreated={reloadApps} />

        {/* Doorways: one card per entry point. */}
        <div className="home-doors">
          <Doorway
            hue="var(--icon-folder)"
            title="File explorer"
            desc="Navigate your workspace and open any file with its template."
            titleAttr={config.fused_dir}
            onClick={() => navigate(config.fused_dir, { isDir: true })}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
            }
          />
          <Doorway
            hue="var(--icon-code)"
            title="Apps"
            desc={`Every folder inside a tag folder in ${basename(config.fused_dir)} is a project with its own entry page — plus any found elsewhere on this machine.`}
            titleAttr={config.fused_dir}
            onClick={() => navigateUrl("/apps")}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="7" height="7" rx="1.5" />
                <rect x="14" y="3" width="7" height="7" rx="1.5" />
                <rect x="3" y="14" width="7" height="7" rx="1.5" />
                <rect x="14" y="14" width="7" height="7" rx="1.5" />
              </svg>
            }
          />
          <Doorway
            hue="var(--icon-json)"
            title="Templates"
            desc="Build a custom interactive view for any file extension."
            onClick={() => navigateUrl("/view/_templates")}
            glyph={
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M8 6l-5 6 5 6M16 6l5 6-5 6" />
              </svg>
            }
          />
          {learnMountReady && (
            <Doorway
              hue="var(--icon-data)"
              title="Learn"
              desc="Guided lessons that teach fused-render by example, right in the app."
              onClick={() => void openLearn(config)}
              glyph={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 7v13" />
                  <path d="M3 6a2 2 0 0 1 2-2h4a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H3Z" />
                  <path d="M21 6a2 2 0 0 0-2-2h-4a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H21Z" />
                </svg>
              }
            />
          )}
        </div>

        <section className="home-section">
          <SectionRule
            label="recent"
            action={
              apps.status === "ok" && apps.data.apps.length > 5 ? (
                <button
                  type="button"
                  className="home-rule-action"
                  onClick={() => navigateUrl("/apps")}
                >
                  View all →
                </button>
              ) : undefined
            }
          />
          {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
          {apps.status === "loading" && <SkeletonLines rows={3} label="Loading apps" />}
          {apps.status === "ok" && apps.data.apps.length === 0 && (
            <div className="home-empty">
              No apps yet. Describe one in the box above — it lands in{" "}
              {basename(config.fused_dir)}/local as a folder you own.
            </div>
          )}
          {apps.status === "ok" && apps.data.apps.length > 0 && (
            <div className="home-recents">
              {/* The 5 most recently updated apps. Sort is computed once per
                  fetch: recency (updated_at epoch seconds, missing → last;
                  name breaks ties) — stable under interaction since nothing
                  re-sorts after load. */}
              {apps.data.apps
                .slice()
                .sort(
                  (a, b) =>
                    (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.name.localeCompare(b.name),
                )
                .slice(0, 5)
                .map((app) => (
                  <RecentRow key={app.path} app={app} />
                ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
