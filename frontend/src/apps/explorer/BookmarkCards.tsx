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
  withPreviewFlag,
  EMBED_PREFIX,
  VIEW_PREFIX,
} from "@platform/lib/router";
import { withNoFocus } from "@platform/lib/frame-focus";
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
export function LivePreview({ src }: { src: string }) {
  // `_nofocus=1` beside the thumbnail stamp: a card peek must not take the
  // keyboard, because focusing an element inside a frame scrolls that frame
  // into view and the scroll propagates out to the page's own scroller — a
  // peeked page that focuses an input on boot yanked the card grid down to
  // itself mid-scroll (D341, platform/lib/frame-focus.ts).
  src = withNoFocus(withPreviewFlag(src));
  return (
    <span className="fhb-preview" aria-hidden="true">
      <iframe
        src={src}
        style={{
          width: `${100 / PREVIEW_SCALE}%`,
          height: `${100 / PREVIEW_SCALE}%`,
          transform: `scale(${PREVIEW_SCALE})`,
        }}
        loading="lazy"
        tabIndex={-1}
        scrolling="no"
        title=""
      />
      <span className="fhb-shield" />
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
function ImagePreview({ src, fallback }: { src: string; fallback: ReactNode }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <>{fallback}</>;
  return (
    <span className="fhb-preview" aria-hidden="true">
      <img
        className="fhb-shot"
        src={src}
        alt=""
        loading="lazy"
        onError={() => setFailed(true)}
      />
      <span className="fhb-shield" />
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
  const livePeek = peekPath ? <LivePreview src={embedUrlForFsPath(peekPath)} /> : null;
  const body = peekPath ? (
    isPreviewImage(peekPath) ? (
      // A broken image falls back to the embed of that same path, which renders
      // the file through the shell — worst case its own error state, never a
      // blank card.
      <ImagePreview src={rawUrl(peekPath)} fallback={livePeek} />
    ) : (
      livePeek
    )
  ) : settled ? (
    <span className="fhb-sheet-note">
      {`${teaser.length} item${teaser.length === 1 ? "" : "s"}`}
    </span>
  ) : null;
  return (
    <span className="fhb-stack">
      {/* Back sheet first: natural stacking keeps the front sheet on top.
          Each entry is ONE sheet — a title row whose card body slides down
          behind the sheet in front of it; the front sheet carries the
          preview inside itself (the ref design's titlebar-plus-page card,
          not a bar floating over a separate panel). The whole stack waits
          for settled: painting an alphabetical stack mid-probe would let
          the front sheet reorder under the user once subPeek names it. */}
      {settled &&
        shown.map((e, i) => {
          const depth = shown.length - 1 - i;
          return (
            <span key={e.name} className={`fhb-sheet fhb-sheet-d${depth}`}>
              <span className="fhb-sheet-row">
                <span className="fhb-sheet-icon">{iconForEntry(e.name, e.is_dir)}</span>
                <span className="fhb-sheet-label">{e.name}</span>
              </span>
              {depth === 0 ? (
                body
              ) : (
                // Back sheets keep a blank page-body strip (revealed by the
                // hover fan) but load NO iframe: one live preview per card —
                // the front sheet's — is the whole embed budget; a strip per
                // back sheet tripled the page loads a card grid spawns.
                <span className="fhb-sheet-peek" />
              )}
            </span>
          );
        })}
      {/* Nothing to stack: the mark stands in the card's place. Both theme
          renders are in the DOM; CSS shows the one matching data-theme. */}
      {shown.length === 0 && settled && (
        <span className="fhb-note fhb-empty">
          <img
            className="fhb-empty-mark fhb-empty-mark-dark"
            src={logoMarkDark}
            alt=""
            aria-hidden="true"
          />
          <img
            className="fhb-empty-mark fhb-empty-mark-light"
            src={logoMarkLight}
            alt=""
            aria-hidden="true"
          />
          Empty folder
        </span>
      )}
    </span>
  );
}

// Card shell: header row (icon tile + name over path) above the body. An
// anchor so middle-click / Cmd-click open a new tab (same as LaunchCard).
function CardShell({
  b,
  icon,
  isDir,
  children,
}: {
  b: Bookmark;
  icon: ReactNode;
  isDir: boolean | null;
  children: ReactNode;
}) {
  const fsPath = bookmarkFsPath(b.url);
  return (
    <a
      className="fhb-card"
      href={b.url}
      title={fsPath}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        // Open AND arm, like the sidebar row — the breadcrumb's
        // Update-bookmark tracking should work no matter where the
        // bookmark was opened from.
        armBookmark(b.id, b.url);
        navigateUrl(b.url, isDir === true ? { isDir: true } : undefined);
      }}
    >
      <span className="fhb-card-head">
        <span className="fh-card-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="fh-card-text">
          <span className="fh-card-name">{b.name}</span>
          <span className="fh-card-path">{fsPath}</span>
        </span>
        {isBookmarkMissing(b.id) && (
          <span className="bookmark-missing-badge" title={`File not found: ${fsPath}`}>
            ⚠
          </span>
        )}
      </span>
      {children}
    </a>
  );
}

const STAR_ICON = (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M8 1.6l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.4l-3.8 2 .7-4.3-3.1-3 4.3-.6L8 1.6z" />
  </svg>
);

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
    <span className="fh-card-emoji">{b.icon}</span>
  ) : isDir === null ? (
    STAR_ICON
  ) : (
    iconForEntry(basename(fsPath), isDir)
  );

  return (
    <CardShell b={b} icon={icon} isDir={isDir}>
      {missing ? (
        <span className="fhb-note">File not found — the target was moved or deleted</span>
      ) : isDir === null ? (
        // Stat in flight: hold the body's box so the grid doesn't reflow.
        <span className="fhb-note" aria-hidden="true" />
      ) : isDir ? (
        <FolderStack path={fsPath} />
      ) : (
        <span className="fhb-thumb">
          <LivePreview src={embedUrlForBookmark(b.url)} />
        </span>
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
    <a
      className="fhb-card"
      href={url}
      title={path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        navigateUrl(url);
      }}
    >
      <span className="fhb-card-head">
        <span className="fh-card-icon" aria-hidden="true">
          {iconForEntry(basename(path), false)}
        </span>
        <span className="fh-card-text">
          <span className="fh-card-name">{name}</span>
          <span className="fh-card-path">{path}</span>
        </span>
      </span>
      <span className="fhb-thumb">
        <LivePreview src={embedUrlForBookmark(url)} />
      </span>
    </a>
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
    <a
      className="fhb-card"
      href={urlForFsPath(path)}
      title={path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        navigate(path, { isDir: true });
      }}
    >
      <span className="fhb-card-head">
        <span className="fh-card-icon" aria-hidden="true">
          {iconForEntry(basename(path), true)}
        </span>
        <span className="fh-card-text">
          <span className="fh-card-name">{basename(path)}</span>
          <span className="fh-card-path">{path}</span>
        </span>
      </span>
      <FolderStack path={path} />
    </a>
  );
}
