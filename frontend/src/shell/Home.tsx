// The app's front door (/home): the explorer's search hero over three
// recency-sorted strips — Fused Apps, Claude Sessions, Recent files — each
// capped at one row with a "See all" that lands on the full surface it
// previews (/apps, or the explorer home pinned to the matching tab).
//
// Lives in the shell layer on purpose: it composes builder cards
// (AppPreviewCard) with explorer cards and libs, which only the shell may
// import together (scripts/check-boundaries.mjs).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { buttonVariants } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Muted, Page, PageBody, SectionTitle, Tiny } from "@platform/ui/flow/Typography";
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
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";
import { FdaStrip } from "@platform/ui/FdaStrip";
import { PLAYGROUND_GROUPS, type PlaygroundGroup } from "@apps/ai_models/playground/groups";
import { tabHref } from "@apps/ai_models/routes";

// One row per section: the page measures its own width and renders exactly
// as many full-size cards as fit — no wrapping, no clipping, no scrolling.
// The full lists live behind "See all".
// The card width the count is measured against (the rows themselves are equal
// grid columns — see Row — so this is the floor a card is allowed to shrink
// toward, not a pixel width drawn anywhere).
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
  id,
  title,
  seeAllHref,
  children,
}: {
  /** Stable anchor for the welcome tour (platform/lib/tours/home.ts) — the
      section's own classes are shared by all four strips. */
  id?: string;
  title: string;
  seeAllHref: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <SectionTitle>
          <a className="hover:text-muted-foreground" href={seeAllHref} onClick={(e) => softNavigate(e, seeAllHref)}>
            {title}
          </a>
        </SectionTitle>
        <a
          className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "text-muted-foreground hover:text-foreground")}
          href={seeAllHref}
          onClick={(e) => softNavigate(e, seeAllHref)}
        >
          See all
          <ChevronRight aria-hidden="true" />
        </a>
      </div>
      {children}
    </section>
  );
}

// One row of cards: as many equal columns as the page measured fit (see
// useStripCount), so nothing wraps, clips, or scrolls. Cards fill the width
// rather than sitting in a centred fixed-pixel column.
function Row({ count, children, ...rest }: { count: number; children: React.ReactNode } & React.ComponentProps<"div">) {
  return (
    <div
      className="grid gap-4"
      style={{ gridTemplateColumns: `repeat(${Math.max(1, count)}, minmax(0, 1fr))` }}
      {...rest}
    >
      {children}
    </div>
  );
}

// Skeleton for one card while a strip's fetch is in flight. Two variants,
// because Home's two async strips draw two DIFFERENT real cards:
//   - "app"    mirrors AppPreviewCard — title + a meta row (tag pill, timestamp)
//     OVER a full-bleed thumb. No icon: the real card has none.
//   - "folder" mirrors FolderPreviewCard — a head row (name over path) over an
//     inset thumb well. The real card's head DOES carry an icon, but it's a
//     static decorative folder glyph, identical on every card and independent
//     of the fetch — shimmering it would claim something is loading that isn't,
//     so neither variant renders an icon.
// Pure decoration — `aria-hidden`, with the row wrapper (below) carrying the
// one `role="status"` announcement for the whole strip.
function SkeletonCard({ variant }: { variant: "app" | "folder" }) {
  return (
    <span className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" aria-hidden="true">
      <span className="flex flex-col gap-1.5">
        <Skeleton className="h-3.5 w-[58%]" />
        {variant === "app" ? (
          <span className="flex gap-2">
            <Skeleton className="h-3 w-12" />
            <Skeleton className="h-3 w-16" />
          </span>
        ) : (
          <Skeleton className="h-3 w-[48%]" />
        )}
      </span>
      <Skeleton className="aspect-[16/10] w-full rounded-lg" />
    </span>
  );
}

// A skeleton row is sized by `shown`, not by a guess or the section's peak
// `limit` — the same number of cards the real row will draw once the fetch
// lands (Home.tsx slices every strip to `shown`), so the swap from skeleton to
// content never changes the row's card count or width. Floored at 1: `shown`
// is 0 before the wrapper has been measured (see useStripCount), and a row of
// zero skeleton cards would render as nothing at all rather than as "loading".
function SkeletonRow({
  count,
  label,
  variant,
}: {
  count: number;
  label: string;
  variant: "app" | "folder";
}) {
  return (
    <Row count={count} role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: Math.max(1, count) }, (_, i) => (
        <SkeletonCard key={i} variant={variant} />
      ))}
    </Row>
  );
}

// The terminal empty state for a strip: one muted line, deliberately NOT a
// card-height well — this state is permanent, not a loading flicker.
// `data-empty` is a bare HOOK, not a style: the Home tour gates itself on a
// section having settled — either a real card or this line — and would
// otherwise retry forever for someone with no apps yet
// (platform/lib/tours/home.ts). It replaces the `.fh-empty` class the old
// files-home stylesheet supplied.
function EmptyLine({ children }: { children: React.ReactNode }) {
  return (
    <Muted data-empty="" className="py-5 text-center">
      {children}
    </Muted>
  );
}

// The AI Playground strip's glyph vocabulary — plain strokes on the current
// color, so the tinted body well colours them for free. Keyed by the THING a
// task reads or writes rather than by the capability, because the card body
// draws each task as the pair it maps between (see PLAYGROUND_FLOWS).
const MEDIA_GLYPHS = {
  // Material's `message` — a squared bubble with a corner tail and three lines
  // of writing — and the SAME geometry the playground rail's Text generation
  // section wears (apps/ai_models/playground/capabilityIcons.tsx). These two
  // surfaces are one click apart and the card is a picture of where the click
  // lands, so a different bubble on each end reads as two different features.
  // It replaces a rounded balloon whose only marks were two short lines: at
  // the 18px this draws at, that read as a speech balloon — someone talking —
  // where every use of this glyph here is about WRITTEN text.
  chat: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 4h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H7l-4 4V6a2 2 0 0 1 2-2Z" />
      <path d="M7 7.5h10M7 10.5h10M7 13.5h6" />
    </svg>
  ),
  image: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2.5" />
      <circle cx="9" cy="10" r="2" />
      <path d="M3 17.5 8.5 13l4 3.5 3.5-3 5 4.5" />
    </svg>
  ),
  speech: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </svg>
  ),
  meaning: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="10.5" cy="10.5" r="7" />
      <path d="M20.5 20.5 15.6 15.6" />
      <circle cx="8" cy="9" r="0.4" />
      <circle cx="12.8" cy="8.2" r="0.4" />
      <circle cx="10.2" cy="13" r="0.4" />
    </svg>
  ),
  video: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="5.5" width="13" height="13" rx="2.5" />
      <path d="M16 10.5 21 7.5v9L16 13.5z" />
    </svg>
  ),
} satisfies Record<string, ReactNode>;

type PlaygroundMedia = keyof typeof MEDIA_GLYPHS;

// What each task takes in and hands back. The body renders it literally —
// in-glyph, arrow, out-glyph — so the card shows "speech becomes text" without
// leaning on the blurb, and chat → chat still reads as a mapping (rewriting)
// rather than a doubled icon.
const PLAYGROUND_FLOWS: Record<string, [PlaygroundMedia, PlaygroundMedia]> = {
  "text-generation": ["chat", "chat"],
  "text-to-image": ["chat", "image"],
  "text-to-video": ["chat", "video"],
  "automatic-speech-recognition": ["speech", "chat"],
  embeddings: ["chat", "meaning"],
};

// The header's single glyph names the task itself, which is not always the
// flow's output — Transcription is filed under the mic, not under text.
const PLAYGROUND_HEADS: Record<string, PlaygroundMedia> = {
  "text-generation": "chat",
  "text-to-image": "image",
  "text-to-video": "video",
  "automatic-speech-recognition": "speech",
  embeddings: "meaning",
};

const FLOW_ARROW = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 12h15M13.5 6.5 20 12l-6.5 5.5" />
  </svg>
);

// One card per playground task: the header names it, the body says what it
// does. Static on purpose — the strip advertises the SURFACE, not this
// machine's downloads, so it costs Home no catalog fetch. The link carries
// only the capability (`?cap=`); the playground itself resolves that to its
// vetted default model, so the choice lives in one place.
function PlaygroundPreviewCard({ group }: { group: PlaygroundGroup }) {
  const href = tabHref("playground", `?cap=${encodeURIComponent(group.capability)}`);
  // A group added without a flow still renders (Home must not crash on a
  // vocabulary edit): it falls back to the header glyph on both sides.
  const head = PLAYGROUND_HEADS[group.capability] ?? "chat";
  const flow = PLAYGROUND_FLOWS[group.capability] ?? [head, head];
  return (
    <a
      className="group flex flex-col gap-2 rounded-lg border border-border bg-card p-3 text-sm shadow-sm transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none"
      href={href}
      onClick={(e) => softNavigate(e, href)}
    >
      <span className="flex items-center gap-2.5">
        <span className="flex size-4 shrink-0 items-center text-muted-foreground [&_svg]:size-4" aria-hidden="true">
          {MEDIA_GLYPHS[head]}
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{group.label}</span>
          <Tiny className="truncate">Runs on this machine</Tiny>
        </span>
      </span>
      {/* in-glyph → out-glyph: the card draws the task as the mapping it
          performs (speech becomes text) rather than as one decorative icon.
          Same tile twice is intended for chat → chat — the pair is what reads
          as "text in, text out". */}
      <span
        className="flex aspect-[16/10] flex-col items-center justify-center gap-2.5 overflow-hidden rounded-lg border border-border bg-muted"
        aria-hidden="true"
      >
        <span className="flex items-center gap-3">
          <FlowGlyph>{MEDIA_GLYPHS[flow[0]]}</FlowGlyph>
          <span className="flex size-4 text-muted-foreground [&_svg]:size-4">{FLOW_ARROW}</span>
          <FlowGlyph>{MEDIA_GLYPHS[flow[1]]}</FlowGlyph>
        </span>
        <Tiny className="max-w-[85%] text-center group-hover:text-foreground">{group.blurb}</Tiny>
      </span>
    </a>
  );
}

// One side of the flow pair: a squared-corner tile on the card ground.
function FlowGlyph({ children }: { children: ReactNode }) {
  return (
    <span className="flex size-14 items-center justify-center rounded-md border border-border bg-background text-foreground [&_svg]:size-7">
      {children}
    </span>
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
          // A "no-cache" status means the clone is still missing — wait for
          // it. But the clone can just as easily land in the gap between the
          // first empty getHomeApps and this very check, which reports it
          // "ok" already: that walk never re-ran, so its emptiness is just as
          // stale. Either way, one more walk is needed before the row really
          // is empty — retry unconditionally, only waiting on refresh first
          // when the clone genuinely hasn't landed yet.
          if (local.status === "no-cache") {
            await runCommunity({ action: "refresh" });
            if (!alive) return;
          }
          const retry = await getHomeApps(Math.min(limit, MAX_ROW));
          if (!alive) return;
          setApps(retry.apps.slice(0, MAX_ROW));
          return;
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
    <Page>
      <PageBody className="px-6 pt-2 pb-16 md:px-8">
        {/* `home-hero` is a DOM hook for the welcome tour only
            (platform/lib/tours/home.ts); nothing styles it. */}
        <header className="home-hero mx-auto w-full max-w-5xl pt-4 pb-2 text-left">
          <FilesSearch
            home={home}
            initialQuery={initialQuery}
            indexScan={indexScan}
            onActiveChange={setSearching}
          />
        </header>

        {searching ? null : (
          // The measuring element for useStripCount: the rows fill its width
          // with as many equal columns as whole cards fit.
          <div ref={stripRef} className="space-y-8">
            {/* Above the strips, because on a machine where Claude Code is not
                set up this is the only thing on the page the user can act on —
                and it renders nothing at all once there is nothing to say.
                Hidden while a search is live for the same reason the strips
                are: the search result IS the page then. */}
            <ClaudeHealthStrip />
            <FdaStrip />
            <Section id="home-sec-apps" title="Fused Apps" seeAllHref="/apps">
              {apps === null ? (
                <SkeletonRow count={shown} label="Loading apps" variant="app" />
              ) : apps.length ? (
                <Row count={shown}>
                  {apps.slice(0, shown).map((app) => (
                    <AppPreviewCard key={app.path} app={app} />
                  ))}
                </Row>
              ) : (
                <EmptyLine>
                  {appsError ?? "No apps yet. Build one and it'll show up here."}
                </EmptyLine>
              )}
            </Section>

            <Section
              id="home-sec-playground"
              title="AI Playground"
              seeAllHref={tabHref("playground", "")}
            >
              <Row count={shown}>
                {PLAYGROUND_GROUPS.slice(0, shown).map((group) => (
                  <PlaygroundPreviewCard key={group.capability} group={group} />
                ))}
              </Row>
            </Section>

            <Section
              id="home-sec-sessions"
              title="Claude Sessions"
              seeAllHref="/explorer?tab=sessions"
            >
              {sessions === null ? (
                <SkeletonRow count={shown} label="Loading Claude sessions" variant="folder" />
              ) : sessions.length ? (
                <Row count={shown}>
                  {sessions.slice(0, shown).map((f) => (
                    <FolderPreviewCard key={f.path} path={f.path} />
                  ))}
                </Row>
              ) : (
                <EmptyLine>No Claude Code sessions found on this machine.</EmptyLine>
              )}
            </Section>

            <Section title="Recent files" seeAllHref="/explorer?tab=recents">
              {recents.length ? (
                <Row count={shown}>
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
                </Row>
              ) : (
                <EmptyLine>Nothing opened yet. Files you view will show up here.</EmptyLine>
              )}
            </Section>
          </div>
        )}
      </PageBody>
    </Page>
  );
}
