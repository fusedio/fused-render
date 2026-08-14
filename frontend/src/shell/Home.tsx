// The app's front door (/home): the explorer's search hero over three
// recency-sorted strips — Fused Apps, Claude Sessions, Recent files — each
// capped at one row with a "See all" that lands on the full surface it
// previews (/apps, or the explorer home pinned to the matching tab).
//
// Lives in the shell layer on purpose: it composes builder cards
// (AppPreviewCard) with explorer cards and libs, which only the shell may
// import together (scripts/check-boundaries.mjs).
import { useEffect, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import {
  getApps,
  getClaudeSessionFolders,
  type AppInfo,
  type ClaudeSessionFolder,
  type Config,
} from "@platform/lib/api";
import { useIndexStatus } from "@platform/lib/index-status";
import { hydrateRecents, loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { FilesSearch } from "@apps/explorer/FilesHome";
import { FolderPreviewCard, RecentPreviewCard } from "@apps/explorer/BookmarkCards";
import { AppPreviewCard } from "@apps/builder/AppPreviewCard";

// One row per section: the page measures its own width and renders exactly
// as many full-size cards as fit — no wrapping, no clipping, no scrolling.
// The full lists live behind "See all".
// Card width + gap must match the .home-row CSS.
const CARD_W = 330;
const CARD_GAP = 16;
// How many entries each section fetches/keeps — enough for a very wide window.
const MAX_ROW = 12;

// How many cards fit across the sections' shared container right now.
// One ResizeObserver on the wrapper, one count for all three strips.
function useStripCount() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [count, setCount] = useState(3);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setCount(Math.max(1, Math.floor((el.clientWidth + CARD_GAP) / (CARD_W + CARD_GAP))));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return { ref, count };
}

// Same order the /apps hub uses (Apps.tsx sortApps): recently-modified desc,
// apps without a timestamp sink to the end, name breaks ties.
function sortApps(apps: AppInfo[]): AppInfo[] {
  const byName = (a: AppInfo, b: AppInfo) =>
    (a.title || a.name).localeCompare(b.title || b.name) || a.name.localeCompare(b.name);
  return apps.slice().sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || byName(a, b));
}

// Section chrome: title row with the "See all" action on the right edge.
// An <a> so cmd/ctrl-click opens the full page in a new tab like any link.
function Section({
  title,
  seeAllHref,
  children,
}: {
  title: string;
  seeAllHref: string;
  children: React.ReactNode;
}) {
  return (
    <section className="fh-section home-section">
      <div className="home-sec-head">
        <h2 className="home-sec-title">{title}</h2>
        <a
          className="home-sec-more"
          href={seeAllHref}
          onClick={(e) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
              return;
            e.preventDefault();
            navigateUrl(seeAllHref);
          }}
        >
          See all
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M9 6l6 6-6 6" />
          </svg>
        </a>
      </div>
      {children}
    </section>
  );
}

export default function Home({ config }: { config: Config }) {
  // Same normalization every other config.home consumer applies.
  const home = config.home.replace(/\\/g, "/");
  useRecentsVersion();

  // Fused apps — one cheap GET on mount, newest first.
  const [apps, setApps] = useState<AppInfo[] | null>(null);
  const [appsError, setAppsError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    getApps().then(
      (r) => alive && setApps(sortApps(r.apps).slice(0, MAX_ROW)),
      (e: Error) => {
        if (!alive) return;
        setApps([]);
        setAppsError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  // Claude session folders — the server already answers newest-session-first.
  const [sessions, setSessions] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    let alive = true;
    getClaudeSessionFolders().then(
      (r) => alive && setSessions(r.folders.slice(0, MAX_ROW)),
      () => alive && setSessions([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  // Recents come from the same client cache the explorer home reads (raw MRU).
  useEffect(() => {
    void hydrateRecents();
  }, []);
  const recents = loadRecents().entries.slice(0, MAX_ROW);

  // Search takes over the page body while a query is live — the same posture
  // as the explorer home. The index poll only runs while the box needs its
  // "indexing…" caveat.
  const [searching, setSearching] = useState(false);
  const indexScan = useIndexStatus(searching);
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;

  const { ref: stripRef, count } = useStripCount();

  return (
    <div className="files-home">
      <div className="files-home-inner home-wide">
        <header className="home-hero files-hero">
          <FilesSearch
            home={home}
            initialQuery={initialQuery}
            indexScan={indexScan}
            onActiveChange={setSearching}
          />
        </header>

        {searching ? null : (
          // Outer div is the full-width measuring element; the inner column is
          // exactly as wide as the cards that fit and centered, so the section
          // titles and "See all" stay flush with the cards' edges.
          <div ref={stripRef}>
            <div className="home-strips" style={{ width: count * (CARD_W + CARD_GAP) - CARD_GAP }}>
            <Section title="Fused Apps" seeAllHref="/apps">
              {apps === null ? (
                <p className="fh-empty">Loading apps…</p>
              ) : apps.length ? (
                <div className="home-row">
                  {apps.slice(0, count).map((app) => (
                    <AppPreviewCard key={app.path} app={app} />
                  ))}
                </div>
              ) : (
                <p className="fh-empty">
                  {appsError ?? "No apps yet. Build one and it'll show up here."}
                </p>
              )}
            </Section>

            <Section title="Claude Sessions" seeAllHref="/explorer?tab=sessions">
              {sessions === null ? (
                <p className="fh-empty">Looking for sessions…</p>
              ) : sessions.length ? (
                <div className="home-row">
                  {sessions.slice(0, count).map((f) => (
                    <FolderPreviewCard key={f.path} path={f.path} />
                  ))}
                </div>
              ) : (
                <p className="fh-empty">No Claude Code sessions found on this machine.</p>
              )}
            </Section>

            <Section title="Recent files" seeAllHref="/explorer?tab=recents">
              {recents.length ? (
                <div className="home-row">
                  {recents.slice(0, count).map((r) => {
                    const fsPath = recentFsPath(r.url);
                    return (
                      <RecentPreviewCard
                        key={fsPath}
                        url={r.url}
                        path={fsPath}
                        name={r.title || basename(fsPath)}
                      />
                    );
                  })}
                </div>
              ) : (
                <p className="fh-empty">Nothing opened yet. Files you view will show up here.</p>
              )}
            </Section>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
