// The app's front door (/home): the explorer's search hero over three
// recency-sorted strips — Fused Apps, Claude Sessions, Recent files — each
// capped at one row with a "See all" that lands on the full surface it
// previews (/apps, or the explorer home pinned to the matching tab).
//
// Lives in the shell layer on purpose: it composes builder cards
// (AppPreviewCard) with explorer cards and libs, which only the shell may
// import together (scripts/check-boundaries.mjs).
import { useCallback, useEffect, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import {
  getHomeApps,
  getHomeClaudeSessionFolders,
  type AppInfo,
  type ClaudeSessionFolder,
  type Config,
} from "@platform/lib/api";
import { useIndexStatus } from "@platform/lib/index-status";
import { runCommunity } from "@platform/lib/community";
import { loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { FilesSearch } from "@apps/explorer/FilesHome";
import { FolderPreviewCard, RecentPreviewCard } from "@apps/explorer/BookmarkCards";
import { AppPreviewCard } from "@apps/builder/AppPreviewCard";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";

// One row per section: the page measures its own width and renders exactly
// as many full-size cards as fit — no wrapping, no clipping, no scrolling.
// The full lists live behind "See all".
// Card width + gap must match the .home-row CSS.
const CARD_W = 330;
const CARD_GAP = 16;
// The ceiling on what a section may fetch/keep — enough for a very wide
// window, and the same cap the two endpoints apply to `limit` themselves
// (HOME_APPS_LIMIT / HOME_SESSION_LIMIT). NOT the number either one asks for:
// see useStripCount.
const MAX_ROW = 12;

// How many cards fit across the sections' shared container right now, plus the
// most that have ever fit — the number the fetches ask for.
// One ResizeObserver on the wrapper, one count for all three strips.
//
// The two numbers are not the same, and the difference is load-bearing. Asking
// for MAX_ROW when the row draws three cards is what made the server's
// recents-first fast path unreachable: /api/apps/home skips its exhaustive
// workspace walk only once the recents FILL the request (routers/apps.py), so a
// request for twelve walked the whole workspace on every visit for anyone with
// fewer than twelve opened apps — which is nearly everyone. A row that asks for
// what it can draw puts that walk back to being the fallback it is documented
// as, and the session row's per-directory transcript reads (its endpoint stops
// as soon as `limit` folders land) shrink with it.
//
// `count` is null until the wrapper has actually been MEASURED, and both
// fetches wait for it. A guess would be a request for cards the row cannot
// show, which is the same bug in smaller print. Nothing flashes for it: a
// callback ref runs in the commit phase, so the measured value is in before the
// browser paints.
//
// `limit` is the PEAK count, never the current one — widening the window needs
// cards the first fetch did not ask for, while narrowing it already holds
// enough, so a drag that shrinks the row refetches nothing.
//
// A CALLBACK ref, not useRef+useEffect: the measured wrapper UNMOUNTS while a
// search is live (`searching ? null : <div ref=…>`), and a mount-once effect
// only ever saw the first element — on unmount the observer fired against the
// detached node (clientWidth 0 → count 1) and the remounted wrapper was never
// observed again, so clearing a search left every strip at one card per row.
// The callback re-runs on each mount/unmount: it tears the old observer down
// and measures the element actually on screen.
function useStripCount() {
  // One state, not two: `limit` is derived from the same measurement as
  // `count`, and splitting them would let a render see a count the limit had
  // not accounted for yet.
  const [size, setSize] = useState<{ count: number | null; limit: number | null }>({
    count: null,
    limit: null,
  });
  const roRef = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const measure = () => {
      const fits = Math.max(
        1,
        Math.floor((el.clientWidth + CARD_GAP) / (CARD_W + CARD_GAP)),
      );
      // Same object back when the count is unchanged: a ResizeObserver fires
      // for every pixel of a window drag, and only a changed card count is a
      // reason to re-render (or, via `limit`, to fetch).
      setSize((prev) =>
        prev.count === fits
          ? prev
          : { count: fits, limit: Math.max(prev.limit ?? 0, fits) },
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return { ref, count: size.count, limit: size.limit };
}

// Section chrome: title row with the "See all" action on the right edge.
// Both title and "See all" are <a>s so cmd/ctrl-click opens the full page in
// a new tab like any link.
function softNavigate(e: React.MouseEvent, href: string) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigateUrl(href);
}

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
        <h2 className="home-sec-title">
          <a className="home-sec-title-link" href={seeAllHref} onClick={(e) => softNavigate(e, seeAllHref)}>
            {title}
          </a>
        </h2>
        <a className="home-sec-more" href={seeAllHref} onClick={(e) => softNavigate(e, seeAllHref)}>
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

  // Ahead of the two fetches below, which size their request by it.
  const { ref: stripRef, count, limit } = useStripCount();
  // Slice width while the wrapper is still unmeasured. Pre-paint only — see
  // useStripCount — so an empty row is never actually seen.
  const shown = count ?? 0;

  // Fused apps — hydrate the recent row first. The server only scans the full
  // workspace when valid recents do not fill it, preserving discovery and the
  // showcase fallback without charging returning visits for an exhaustive walk.
  const [apps, setApps] = useState<AppInfo[] | null>(null);
  const [appsError, setAppsError] = useState<string | null>(null);
  useEffect(() => {
    if (limit === null) return;
    let alive = true;
    getHomeApps(Math.min(limit, MAX_ROW)).then(
      async (r) => {
        if (!alive) return;
        if (r.apps.length > 0) {
          setApps(r.apps.slice(0, MAX_ROW));
          return;
        }
        // Empty on a brand-new install usually isn't "no apps" — it's this
        // fetch landing before the startup showcase clone (into
        // <workspace>/showcase, kicked off in the background at server start)
        // has finished. Apps.tsx already escalates the same "no-cache" catalog
        // status into a wait-for-clone-then-refetch; Home is the first page a
        // new user sees, so it needs the same escalation instead of settling
        // on "No apps yet" forever.
        try {
          const local = await runCommunity<{ status?: string }>({ action: "catalog" });
          if (!alive) return;
          if (local.status === "no-cache") {
            await runCommunity({ action: "refresh" });
            if (!alive) return;
            const retry = await getHomeApps(Math.min(limit, MAX_ROW));
            if (!alive) return;
            setApps(retry.apps.slice(0, MAX_ROW));
            return;
          }
        } catch {
          // Community backend unreachable — fall through to the empty state.
        }
        setApps([]);
      },
      (e: Error) => {
        if (!alive) return;
        setApps([]);
        setAppsError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, [limit]);

  // Claude session folders — Home's endpoint orders transcript mtimes first,
  // then opens only enough newest JSONL files to fill this one row.
  const [sessions, setSessions] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    if (limit === null) return;
    let alive = true;
    getHomeClaudeSessionFolders(Math.min(limit, MAX_ROW)).then(
      (r) => alive && setSessions(r.folders.slice(0, MAX_ROW)),
      () => alive && setSessions([]),
    );
    return () => {
      alive = false;
    };
  }, [limit]);

  // Recents come from the same client cache the explorer home reads (raw MRU).
  const recents = loadRecents().entries.slice(0, MAX_ROW);

  // Search takes over the page body while a query is live — the same posture
  // as the explorer home. The index poll only runs while the box needs its
  // "indexing…" caveat.
  const [searching, setSearching] = useState(false);
  const indexScan = useIndexStatus(searching);
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;

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
            <div
              className="home-strips"
              style={
                count === null
                  ? undefined
                  : { width: count * (CARD_W + CARD_GAP) - CARD_GAP }
              }
            >
            {/* Above the strips, because on a machine where Claude Code is not
                set up this is the only thing on the page the user can act on —
                and it renders nothing at all once there is nothing to say.
                Inside the measured column so it lines up with the cards rather
                than spanning the window. Hidden while a search is live for the
                same reason the strips are: the search result IS the page then. */}
            <ClaudeHealthStrip />
            <Section title="Fused Apps" seeAllHref="/apps">
              {apps === null ? (
                <p className="fh-empty">Loading apps…</p>
              ) : apps.length ? (
                <div className="home-row">
                  {apps.slice(0, shown).map((app) => (
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
                  {sessions.slice(0, shown).map((f) => (
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
                  {recents.slice(0, shown).map((r) => {
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
