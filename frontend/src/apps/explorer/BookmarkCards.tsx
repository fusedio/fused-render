// Rich bookmark cards for the /explorer homepage. A bookmark on a FILE shows
// the file itself, rendered live in a scaled, display-only embed iframe (the
// same trick as the /apps AppPreviewCard). A bookmark on a FOLDER shows a
// "stack": the folder's first entries as fanned chips that spread on hover,
// with its best file child's view peeking out underneath — an authored
// `preview.png` as an image if there is one, else a live embed. A stat on the
// bookmark's target decides which body the card gets; saved layout sentinels
// (/_tab, /_panel) skip the stat and embed the whole saved view. Every iframe
// carries a pointer-events shield, so a click anywhere on the card opens the
// bookmark exactly like the sidebar row would.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  navigate,
  navigateUrl,
  urlForFsPath,
  embedUrlForFsPath,
  EMBED_PREFIX,
  VIEW_PREFIX,
} from "@platform/lib/router";
import { thumbFrame } from "@platform/lib/thumb-frame";
import { listDir, rawUrl, statPath } from "@platform/lib/api";
import type { FsEntry } from "@platform/lib/api";
import { basename } from "@platform/lib/format";
import { bestPeekFile, foldProbePick, isPreviewImage, peekRank } from "@apps/explorer/lib/folder-peek";
import type { ProbePick } from "@apps/explorer/lib/folder-peek";
import { iconForEntry } from "@platform/ui/FileIcons";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { armBookmark, isBookmarkMissing, splitBookmarkUrl } from "@platform/lib/bookmarks";
import type { Bookmark } from "@platform/lib/bookmarks";
import { bookmarkFsPath } from "@apps/explorer/sidebar/BookmarksSection";
import { useNearViewport, usePreviewStart } from "@platform/lib/preview-start";
import { cn } from "@platform/lib/utils";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Star, TriangleAlert } from "lucide-react";

// The card grid every home tab (and the shell Home page) lays these out in.
export const CARD_GRID = "grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]";

// The card: flat, square (rounded-lg is 0px), one hairline, no lift on hover.
const CARD_CLASS =
  "flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card p-1.5 text-foreground no-underline shadow-sm transition-colors hover:border-ring focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50";
// The 16/10 body well every card body (thumb, stack, note) fills.
const WELL_CLASS = "relative block aspect-[16/10] overflow-hidden rounded-lg border border-border bg-background";
// The absolute-fill preview box. `[&_iframe]` carries what used to be
// `.fhb-preview iframe`: pointer-events:none is what retargets every press
// onto the card so a click anywhere opens the bookmark.
const PREVIEW_CLASS =
  "block overflow-hidden [&_iframe]:absolute [&_iframe]:top-0 [&_iframe]:left-0 [&_iframe]:border-0 [&_iframe]:bg-background [&_iframe]:origin-top-left [&_iframe]:pointer-events-none";
// Two placements: absolute-fill inside a CardThumb well, or in-flow as the
// front sheet's flex-grown body (right under its title row — one card, no
// seam; the sheet's overflow clip rounds the top corners).
const PREVIEW_FILL = "absolute inset-0";
const PREVIEW_INLINE = "relative z-0 min-h-[60px] w-full flex-1 bg-background";
const previewClass = (inline?: boolean) => cn(PREVIEW_CLASS, inline ? PREVIEW_INLINE : PREVIEW_FILL);
const SKEL_CLASS = "absolute inset-0 rounded-none pointer-events-none motion-reduce:animate-none";

// The iframe renders at desktop width and is scaled into the preview box —
// same pure-CSS trick as AppPreviewCard.
const PREVIEW_SCALE = 0.25;

// How many stacked sheets a folder card fans out (the rest stays behind the count).
const MAX_CHIPS = 3;

// A view url (bookmark or recent) re-prefixed onto the chrome-free embed
// route so it can render inside a card thumbnail. The bare legacy "/view/"
// prefix still converts (urls recorded before the /explorer rename, same
// rule as bookmarkFsPath); layout sentinels (_tab/_panel) embed fine as-is.
export function embedUrlForBookmark(url: string): string {
  const { pathname, search } = splitBookmarkUrl(url);
  const prefix = [VIEW_PREFIX, "/view/"].find((p) => pathname.startsWith(p));
  return prefix ? EMBED_PREFIX + pathname.slice(prefix.length) + search : pathname + search;
}

function joinPath(dir: string, name: string): string {
  return (dir.endsWith("/") ? dir : dir + "/") + name;
}

// Display-only live preview: scaled iframe + a shield keeping clicks on the
// card. The `_preview=1` stamp is what keeps a peek from counting as an OPEN:
// the embed shell reads it once at load (router.IS_PREVIEW) and forwards it
// onto every /render it builds, so a card peeking at an app's entry page never
// records the app open (GET /render records by default, D301) — without it,
// scrolling a folder card into view reshuffles the /apps hub's recency order.
export function LivePreview({ src, inline }: { src: string; inline?: boolean }) {
  const [previewRef, nearViewport] = useNearViewport<HTMLSpanElement>();
  const { started, settled } = usePreviewStart(nearViewport);
  // Separate from `started`/`settled`: those track the SCHEDULER's slot (freed
  // on load OR error OR timeout, so a stuck preview doesn't starve the other
  // one), while `loaded` tracks whether the iframe has actually PAINTED
  // something — the two only coincide on the happy path. Gating the fade on
  // `loaded` (not `started`) is what keeps the crossfade from handing the
  // shimmer off to a raw white/blank frame mid-boot.
  const [loaded, setLoaded] = useState(false);
  // Reset whenever the CURRENT iframe goes away, not just on mount: `started`
  // flips back to false when the card scrolls far enough out of view
  // (usePreviewStart's effect unmounts the iframe), and scrolling back in
  // mounts a brand-new, unloaded one. Without this, `loaded` from the
  // previous mount survived the round trip and the new iframe rendered at
  // full opacity before it had painted anything — a blank/booting frame shown
  // as if it were done. `src` too, in case the same component is ever handed
  // a different preview target without a full unmount.
  useEffect(() => {
    setLoaded(false);
  }, [started, src]);
  if (!started) {
    return (
      <span ref={previewRef} className={previewClass(inline)} aria-hidden="true">
        <Skeleton className={SKEL_CLASS} />
      </span>
    );
  }
  return (
    <span ref={previewRef} className={previewClass(inline)} aria-hidden="true">
      {/* The near-viewport observer is the lazy gate; the shared scheduler is
          the concurrency gate. Keeping them separate means a browser-delayed
          iframe never holds one of the scheduler's two permits. */}
      {!loaded && <Skeleton className={SKEL_CLASS} />}
      <iframe
        {...thumbFrame(src)}
        style={{
          width: `${100 / PREVIEW_SCALE}%`,
          height: `${100 / PREVIEW_SCALE}%`,
          transform: `scale(${PREVIEW_SCALE})`,
          opacity: loaded ? 1 : 0,
          transition: "opacity 0.15s ease",
        }}
        onLoad={() => {
          setLoaded(true);
          settled();
        }}
        // An error is still a PAINTED result — the frame shows the embedded
        // page's own error state — and before this shimmer existed that error
        // page was exactly what a card peek showed. `onError={settled}` alone
        // freed the scheduler slot but left `loaded` false forever, so the
        // shimmer covered the error page permanently instead of revealing it.
        onError={() => {
          setLoaded(true);
          settled();
        }}
      />
      {/* No shield span: the iframe is already `pointer-events: none`
          (PREVIEW_CLASS), so every press retargets onto the card itself. The
          image peek below keeps its shield — an <img> carries the browser's
          native drag gesture, which pointer-events does not stop. */}
    </span>
  );
}

// The card is a teaser, not a listing: hidden and gitignored entries would
// waste its three chip slots (and .DS_Store as the peeking preview says
// nothing about the folder), so they're dropped rather than sorted last.
function teaserEntries(entries: FsEntry[]): FsEntry[] {
  return entries
    .filter((e) => !e.name.startsWith(".") && !e.ignored)
    .sort(
      (a, b) =>
        a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) ||
        (a.name < b.name ? -1 : a.name > b.name ? 1 : 0),
    );
}

// How many subfolders a file-less folder probes for something to peek at.
const APP_PROBE_LIMIT = 3;

// Display-only image thumbnail, in the same box (and with the same shield) the
// scaled iframe gets.
//
// `onError` is not defensive noise: a `preview.png` the server reported can
// still be a corrupt or half-written PNG, and an <img> that fails renders as
// nothing at all — a permanently blank card, with no path back to the live
// render because THIS component was chosen instead of it. Falling back to the
// caller's iframe on the error keeps the old guarantee that a card is never
// blank.
function ImagePreview({ src, fallback, inline }: { src: string; fallback: ReactNode; inline?: boolean }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <>{fallback}</>;
  return (
    <span className={previewClass(inline)} aria-hidden="true">
      <img
        className="absolute inset-0 h-full w-full object-cover bg-background"
        src={src}
        alt=""
        loading="lazy"
        onError={() => setFailed(true)}
      />
      <span className="absolute inset-0" />
    </span>
  );
}

// The stack body of a folder card: back-to-front fanned chips over an
// optional preview peeking out underneath the front chip. The peek is the
// folder's best direct file child (peekRank: `preview.png`, `index.html`,
// `readme.md`, then by extension tier); a folder holding only subfolders (a
// deploy dir of apps)
// probes its first few subfolders and peeks the best-ranked file found there —
// an authored preview.png anywhere wins the probe outright.
function FolderStack({ path }: { path: string }) {
  const [entries, setEntries] = useState<FsEntry[] | null>(null);
  // A probed subfolder's best file — the fallback peek: the file's absolute
  // fs path plus the name of the subfolder it came from (that subfolder
  // becomes the front sheet, so the sheet's title owns the page it shows).
  // undefined = probe not settled yet, null = settled with nothing to peek;
  // the note body waits for settled so it doesn't flash a count and then
  // swap to a preview mid-probe.
  const [subPeek, setSubPeek] = useState<{ path: string; dir: string } | null | undefined>(
    undefined,
  );
  useEffect(() => {
    let alive = true;
    setEntries(null);
    setSubPeek(undefined);
    (async () => {
      let list: FsEntry[] = [];
      try {
        list = (await listDir(path)).entries;
      } catch {
        /* unreadable folder — the note body says what the card knows */
      }
      if (!alive) return;
      setEntries(list);
      const teaser = teaserEntries(list);
      if (teaser.some((e) => !e.is_dir)) {
        setSubPeek(null); // a direct file child wins — no probe needed
        return;
      }
      let best: ProbePick | null = null;
      for (const d of teaser.filter((e) => e.is_dir).slice(0, APP_PROBE_LIMIT)) {
        const subPath = joinPath(path, d.name);
        let sub: FsEntry[];
        try {
          sub = (await listDir(subPath)).entries;
        } catch {
          continue;
        }
        if (!alive) return;
        const f = bestPeekFile(teaserEntries(sub));
        if (!f) continue;
        // Stop as soon as nothing later can beat what we HOLD — an authored
        // image, or a page, which is what "there is a fused app in here" looks
        // like. One listDir instead of three per card, which on an rclone/NFS
        // target is the difference between a card and a stalled mount listing.
        // foldProbePick owns both halves of that decision; see it for why the
        // stop test must be on the winner and not on this candidate.
        const folded = foldProbePick(best, {
          path: joinPath(subPath, f.name),
          rank: peekRank(f.name),
          dir: d.name,
        });
        best = folded.best;
        if (folded.done) break;
      }
      setSubPeek(best ? { path: best.path, dir: best.dir } : null);
    })();
    return () => {
      alive = false;
    };
  }, [path]);

  const teaser = teaserEntries(entries ?? []);
  const firstFile = bestPeekFile(teaser);
  const peekPath = firstFile ? joinPath(path, firstFile.name) : (subPeek?.path ?? null);
  // Settled: listDir landed AND the subfolder probe reached a verdict.
  const settled = entries !== null && subPeek !== undefined;
  // The front sheet must be the entry whose page it shows: the peeked file
  // itself, or the probed subfolder the fallback peek came from — never an
  // alphabetical bystander wearing another entry's preview.
  const frontName = firstFile?.name ?? subPeek?.dir ?? null;
  const front = frontName ? teaser.find((e) => e.name === frontName) : undefined;
  let shown = teaser.slice(0, MAX_CHIPS);
  if (front) {
    shown = shown.filter((e) => e.name !== front.name).slice(0, MAX_CHIPS - 1);
    shown.push(front);
  }
  // The front sheet's body, under its title row: the peeked view, or (once
  // settled with nothing to peek) a count so the card isn't a void. Either way
  // it is ONE preview per card — the back sheets load nothing (see below).
  const livePeek = peekPath ? <LivePreview src={embedUrlForFsPath(peekPath)} inline /> : null;
  const body = peekPath ? (
    isPreviewImage(peekPath) ? (
      // A broken image falls back to the embed of that same path, which renders
      // the file through the shell — worst case its own error state, never a
      // blank card.
      <ImagePreview src={rawUrl(peekPath)} fallback={livePeek} inline />
    ) : (
      livePeek
    )
  ) : settled ? (
    <span className="flex min-h-[60px] flex-1 items-center justify-center p-3 text-xs text-muted-foreground">
      {`${teaser.length} item${teaser.length === 1 ? "" : "s"}`}
    </span>
  ) : null;
  return (
    <span className={cn(WELL_CLASS, "group/stack flex flex-col items-stretch px-2.5 pt-2.5")}>
      {/* Back sheet first: natural stacking keeps the front sheet on top.
          Each entry is ONE sheet — a title row whose card body slides down
          behind the sheet in front of it; the front sheet carries the
          preview inside itself (the ref design's titlebar-plus-page card,
          not a bar floating over a separate panel). The whole stack waits
          for settled: painting an alphabetical stack mid-probe would let
          the front sheet reorder under the user once subPeek names it. */}
      {/* Before settled: three placeholder sheets at the same depths the real
          stack uses, so the silhouette (how many pages, which one is in
          front) is on screen from first paint and only the ink — names,
          the peeked body — arrives once listDir and the subfolder probe both
          land. */}
      {!settled &&
        [2, 1, 0].map((depth) => (
          <Sheet key={depth} depth={depth}>
            <SheetRow>
              <Skeleton className="size-3.5 shrink-0 motion-reduce:animate-none" />
              <Skeleton className="h-2.5 motion-reduce:animate-none" style={{ width: depth === 0 ? "55%" : "70%" }} />
            </SheetRow>
            {depth === 0 ? (
              <Skeleton className="min-h-[60px] w-full flex-1 rounded-none motion-reduce:animate-none" />
            ) : (
              <SheetPeek />
            )}
          </Sheet>
        ))}
      {settled &&
        shown.map((e, i) => {
          const depth = shown.length - 1 - i;
          return (
            <Sheet key={e.name} depth={depth}>
              <SheetRow>
                <span className="flex shrink-0 items-center text-muted-foreground [&_svg]:size-3.5">
                  {iconForEntry(e.name, e.is_dir)}
                </span>
                <span className="min-w-0 truncate">{e.name}</span>
              </SheetRow>
              {depth === 0 ? (
                body
              ) : (
                // Back sheets keep a blank page-body strip (revealed by the
                // hover fan) but load NO iframe: one live preview per card —
                // the front sheet's — is the whole embed budget; a strip per
                // back sheet tripled the page loads a card grid spawns.
                <SheetPeek />
              )}
            </Sheet>
          );
        })}
      {/* Nothing to stack: the mark stands in the card's place. Both theme
          renders are in the DOM; the dark variant shows the one matching
          data-theme. */}
      {shown.length === 0 && settled && (
        <span className="flex flex-1 flex-col items-center justify-center gap-2.5 p-3 text-center text-xs text-muted-foreground">
          <img className="size-10 opacity-35 hidden dark:block" src={logoMarkDark} alt="" aria-hidden="true" />
          <img className="size-10 opacity-35 dark:hidden" src={logoMarkLight} alt="" aria-hidden="true" />
          Empty folder
        </span>
      )}
    </span>
  );
}

// One sheet of the folder stack. Depth 0 is the front sheet (flex-grown, its
// body is the peek); deeper sheets tuck behind it with a negative bottom
// margin that the card's hover relaxes — the "fan". Margin animates only
// under motion-safe.
function Sheet({ depth, children }: { depth: number; children: ReactNode }) {
  return (
    <span
      className={cn(
        "flex min-w-0 flex-col rounded-t-md border border-b-0 border-border bg-card text-[10px] shadow-sm motion-safe:transition-[margin] motion-safe:duration-200",
        depth === 0 && "z-30 min-h-0 flex-1 overflow-hidden",
        depth === 1 && "z-20 mx-2.5 -mb-[33px] group-hover/stack:-mb-[18px]",
        depth === 2 && "z-10 mx-5 -mb-[33px] group-hover/stack:-mb-[18px]",
      )}
    >
      {children}
    </span>
  );
}

function SheetRow({ children }: { children: ReactNode }) {
  return <span className="flex min-w-0 items-center gap-2 p-2.5">{children}</span>;
}

// A back sheet's blank page-body strip — a step lighter than the sheet so the
// fan reads as pages, not bars.
function SheetPeek() {
  return <span className="relative h-5 shrink-0 overflow-hidden bg-muted" />;
}

// Card shell: header row (icon tile + name over path) above the body. An
// anchor so middle-click / Cmd-click open a new tab (same as LaunchCard).
// Exported so the shell Home page can build its skeleton and its own card
// variants on the real shell instead of mimicking it.
export function CardShell({
  href,
  title,
  icon,
  name,
  path,
  badge,
  onClick,
  children,
}: {
  href: string;
  title: string;
  icon: ReactNode;
  name: ReactNode;
  path: ReactNode;
  badge?: ReactNode;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <a
      className={CARD_CLASS}
      href={href}
      title={title}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        onClick();
      }}
    >
      <span className="flex min-w-0 items-center gap-2.5 px-2 pb-2.5 pt-1.5">
        <span
          className="flex size-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted text-foreground"
          aria-hidden="true"
        >
          {icon}
        </span>
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-sm font-medium">{name}</span>
          <span className="truncate text-xs text-muted-foreground">{path}</span>
        </span>
        {badge != null && <span className="ml-auto shrink-0">{badge}</span>}
      </span>
      {children}
    </a>
  );
}

// The 16/10 body well a card's thumbnail (or skeleton) sits in.
export function CardThumb({ className, children }: { className?: string; children?: ReactNode }) {
  return <span className={cn(WELL_CLASS, className)}>{children}</span>;
}

// Placeholder body: missing target, stat in flight.
function CardNote({ children, ...rest }: { children?: ReactNode; "aria-hidden"?: boolean }) {
  return (
    <span
      className="flex aspect-[16/10] items-center justify-center rounded-lg bg-muted/50 p-3 text-center text-xs text-muted-foreground"
      {...rest}
    >
      {children}
    </span>
  );
}

export function BookmarkPreviewCard({ b }: { b: Bookmark }) {
  const fsPath = bookmarkFsPath(b.url);
  // A saved layout (/_tab, /_panel — exact sentinel segments, not any
  // _-prefixed folder like _site) is never a directory — skip the stat.
  const sentinel = fsPath === "/_tab" || fsPath === "/_panel";
  const missing = isBookmarkMissing(b.id);
  const [isDir, setIsDir] = useState<boolean | null>(sentinel ? false : null);
  useEffect(() => {
    // Reset before the stat lands, so a card re-pointed at a new target
    // doesn't keep showing the previous target's body while the new stat
    // is in flight.
    setIsDir(sentinel ? false : null);
    if (sentinel || missing) return;
    let alive = true;
    statPath(fsPath).then(
      (s) => alive && setIsDir(s.is_dir),
      // A failed stat (target gone, mount down) falls back to the file body;
      // the embed route shows its own error state inside the thumbnail.
      () => alive && setIsDir(false),
    );
    return () => {
      alive = false;
    };
  }, [fsPath, sentinel, missing]);

  const icon = b.icon ? (
    <span className="text-[17px] leading-none">{b.icon}</span>
  ) : isDir === null ? (
    <Star className="size-4" />
  ) : (
    iconForEntry(basename(fsPath), isDir)
  );

  return (
    <CardShell
      href={b.url}
      title={fsPath}
      icon={icon}
      name={b.name}
      path={fsPath}
      badge={
        missing ? (
          <TriangleAlert className="size-3.5 text-destructive" aria-label={`File not found: ${fsPath}`} />
        ) : undefined
      }
      onClick={() => {
        // Open AND arm, like the sidebar row — the breadcrumb's
        // Update-bookmark tracking should work no matter where the
        // bookmark was opened from.
        armBookmark(b.id, b.url);
        navigateUrl(b.url, isDir === true ? { isDir: true } : undefined);
      }}
    >
      {missing ? (
        <CardNote>File not found — the target was moved or deleted</CardNote>
      ) : isDir === null ? (
        // Stat in flight: hold the body's box so the grid doesn't reflow.
        <CardNote aria-hidden />
      ) : isDir ? (
        <FolderStack path={fsPath} />
      ) : (
        <CardThumb>
          <LivePreview src={embedUrlForBookmark(b.url)} />
        </CardThumb>
      )}
    </CardShell>
  );
}

// A recent file, in the same card shell as a bookmark — header row (icon +
// name over path) over a live preview thumbnail. Simpler than
// BookmarkPreviewCard: recents are never recorded for a directory
// (useRecentsTracking opts folders out) and the server's GET already drops
// entries whose file is gone, so there's no isDir stat and no missing state
// to branch on.
export function RecentPreviewCard({
  url,
  path,
  name,
}: {
  url: string;
  path: string;
  name: string;
}) {
  return (
    <CardShell
      href={url}
      title={path}
      icon={iconForEntry(basename(path), false)}
      name={name}
      path={path}
      onClick={() => navigateUrl(url)}
    >
      <CardThumb>
        <LivePreview src={embedUrlForBookmark(url)} />
      </CardThumb>
    </CardShell>
  );
}

// A plain folder, in the same card shell as a bookmark — header (icon + name
// over path) over the same folder "stack" preview a directory bookmark gets
// (FolderStack, above). Clicking opens the folder itself in the Explorer.
//
// Deliberately knows nothing about WHY the folder is being listed: the homepage's
// Artifacts tab points it at Claude Code project folders and its Repos tab at
// git repo roots, and both want exactly "the folder preview the other tabs
// show". Any per-tab metadata (a branch, a session time) would belong in the
// card's header, so it would have to arrive as a prop — none does, on purpose.
export function FolderPreviewCard({ path }: { path: string }) {
  return (
    <CardShell
      href={urlForFsPath(path)}
      title={path}
      icon={iconForEntry(basename(path), true)}
      name={basename(path)}
      path={path}
      onClick={() => navigate(path, { isDir: true })}
    >
      <FolderStack path={path} />
    </CardShell>
  );
}
