// Crumb bar + "+ Bookmark" / "Update bookmark" / split right/down buttons.
// Rendered by every view: path crumbs for listing/preview, a static label for
// the layout modes (LM-11 / TM-9 — ★/update still operate on currentUrl()).
import React, { useEffect, useRef, useState } from "react";
import { navigate, navigateUrl, urlForFsPath, currentUrl, IS_EMBED } from "../lib/router";
import { basename } from "../lib/format";
import {
  addBookmark,
  allBookmarks,
  updateBookmarkUrl,
  armBookmark,
  disarmBookmark,
  getArmedBookmark,
  sameSearch,
  splitBookmarkUrl,
} from "../lib/bookmarks";
import { useUrlVersion, useBookmarksVersion, notifyBookmarksChanged } from "../lib/hooks";
import { encodePaneSegment, splitShellSearch } from "../lib/layout-codec";
import { panelUrl } from "../views/Panel";
import { SplitRightIcon, SplitDownIcon } from "./SplitIcons";
import { FinderIcon } from "./FinderIcon";

// "Update bookmark" visibility (D38). The check has side effects (a pathname
// change or a deleted bookmark disarms permanently), so it runs in an effect,
// re-evaluated on every URL or bookmark-store change — the React equivalent
// of the vanilla syncUpdateButton() wired to fused:urlchange.
function useUpdateButton(urlVersion: number, bookmarksVersion: number): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    // Embed pages (layout panes included) share the tab's sessionStorage.
    // Their breadcrumb is hidden chrome (D39) — if this ran there, the pane's
    // /embed pathname would never match the armed url and the pathname check
    // below would permanently disarm the bookmark for the whole tab.
    if (IS_EMBED) return;

    const armed = getArmedBookmark();
    if (!armed) return setVisible(false);

    // allBookmarks(), not loadBookmarks(): the armed bookmark may live inside
    // a folder (D44) — the top-level list alone would misread it as deleted.
    const bookmark = allBookmarks().find((b) => b.id === armed.id);
    if (!bookmark) {
      disarmBookmark(); // bookmark deleted out from under us
      return setVisible(false);
    }

    const { pathname: armedPathname, search: armedSearch } = splitBookmarkUrl(armed.url);

    if (location.pathname !== armedPathname) {
      disarmBookmark(); // page change = permanent disarm
      return setVisible(false);
    }
    setVisible(!sameSearch(location.search, armedSearch));
  }, [urlVersion, bookmarksVersion]);
  return visible;
}

// Shared action block (present on every view). `name` is the default bookmark
// name; `onSplit` present adds the panel-mode entry points (the layout modes
// themselves pass none).
interface CrumbActionsProps {
  name: string;
  onSplit?: (dir: "row" | "col") => void;
}

// Browsers block file:// navigation from http pages, so revealing in the OS
// file manager goes through the server (POST /api/fs/reveal). X-Fused forces
// a CORS preflight so a foreign page can't fire this blind (D3 guard).
function revealInFileManager(path: string): void {
  fetch("/api/fs/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify({ path }),
  });
}

const FILE_MANAGER = navigator.userAgent.includes("Windows") ? "File Explorer" : "Finder";

function RevealButton({ fsPath }: { fsPath: string }) {
  return (
    <button
      id="open-in-finder"
      className="reveal-btn"
      title={"Open in " + FILE_MANAGER}
      onClick={() => revealInFileManager(fsPath)}
    >
      <FinderIcon />
    </button>
  );
}

function CrumbActions({ name, onSplit }: CrumbActionsProps) {
  const urlVersion = useUrlVersion();
  const bookmarksVersion = useBookmarksVersion();
  const showUpdate = useUpdateButton(urlVersion, bookmarksVersion);
  const starred = allBookmarks().some((b) => b.url === currentUrl());

  const onBookmark = async () => {
    await addBookmark(name, currentUrl());
    notifyBookmarksChanged();
  };
  const onUpdate = async () => {
    const armed = getArmedBookmark();
    if (!armed) return;
    const url = currentUrl();
    await updateBookmarkUrl(armed.id, url);
    armBookmark(armed.id, url); // re-arm against the newly saved url
    notifyBookmarksChanged();
  };

  return (
    <div className="crumb-actions">
      {showUpdate && (
        <button
          id="update-bookmark-btn"
          className="star-btn starred"
          title="Update bookmark to current params"
          onClick={onUpdate}
        >
          Update bookmark
        </button>
      )}
      {onSplit && (
        <>
          <button
            id="split-right-btn"
            className="star-btn split-dir"
            title="Open this view in panel mode, split right"
            onClick={() => onSplit("row")}
          >
            <SplitRightIcon />
          </button>
          <button
            id="split-down-btn"
            className="star-btn split-dir"
            title="Open this view in panel mode, split down"
            onClick={() => onSplit("col")}
          >
            <SplitDownIcon />
          </button>
        </>
      )}
      <button
        id="bookmark-btn"
        className={"star-btn" + (starred ? " starred" : "")}
        title={starred ? "View is bookmarked (★ adds another)" : "Bookmark this view"}
        onClick={onBookmark}
      >
        + Bookmark
      </button>
    </div>
  );
}

// Split entry (LM-10): two panes side by side (`dir` "row", `,` in the codec)
// or stacked ("col", `;`), both showing the current view — entering split mode
// with a single pane looked like nothing happened. The current view's WHOLE
// query goes pane-local, inside each `_layout` segment (LM-3/D72): nothing is
// promoted to the top-level pool — global params exist only when the user
// hand-types them on the shell URL. Read via splitShellSearch, not raw
// URLSearchParams (D51): a stray `_layout=(…)` span carries literal `&` that
// would parse as junk keys; the codec read excludes the span, so it is
// dropped — the strict-read semantics.
function enterPanel(fsPath: string, dir: "row" | "col"): void {
  const { params } = splitShellSearch(location.search);
  const paneQ = params.toString();
  const seg = encodePaneSegment(fsPath, paneQ ? "?" + paneQ : "");
  navigateUrl(panelUrl(seg + (dir === "row" ? "," : ";") + seg, null));
}

// Carry the active `_mode` (e.g. a folder viewed in "preview") across top-bar
// navigation so moving between folders preserves the chosen view. Other query
// params are dropped — a fresh path is a fresh view — and an unknown `_mode`
// on the target silently falls back to its default (Preview.activeTemplate).
function navigatePreservingMode(target: string): void {
  const mode = new URLSearchParams(location.search).get("_mode");
  if (mode) navigateUrl(urlForFsPath(target, "?_mode=" + encodeURIComponent(mode)));
  else navigate(target, { isDir: true }); // breadcrumb targets are always dirs
}

export function Breadcrumb({
  fsPath,
  home,
  renderedTitle,
}: {
  fsPath: string;
  home?: string;
  // The previewed page's own <title>, when known (see StatView) — preferred
  // over the file's basename for the default bookmark name (and, via
  // Recents, for its sidebar row) so "My DB app" beats "index.html".
  renderedTitle?: string | null;
}) {
  const crumbsRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);

  // Keep the tail of a long path in view on every path change (same as the
  // panel path bar, Panel.tsx). The strip hides its scrollbar (shell.css), so
  // without this the current folder could sit scrolled off the right edge.
  useEffect(() => {
    const el = crumbsRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [fsPath]);

  // Ctrl/Cmd+L jumps into the editable path (like a browser's location bar).
  // Skip when focus is already in a text field so it never hijacks typing.
  // NOTE: Chrome/Firefox route Ctrl/Cmd+L to their own address bar before the
  // page sees it, so this only lands in app-mode/standalone windows (D: see
  // plan). Registered document-level, cleaned up on unmount (Listing.tsx).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "l") return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      setEditing(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Map a plain mouse wheel's vertical delta onto horizontal scroll so the
  // scrollbar-less strip is still wheel-scrollable (touchpad horizontal pans
  // already work via the native overflow-x).
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY === 0) return;
    e.currentTarget.scrollLeft += e.deltaY;
  };

  // Strictly below home only — home itself shows its full path, not a lone "~".
  const underHome = home !== undefined && fsPath.startsWith(home + "/");
  const rest = underHome ? fsPath.slice(home.length) : fsPath;
  const parts = rest.split("/").filter((s) => s.length > 0);

  // Edit mode seeds the same "~"-contracted path the crumbs display; Enter
  // expands a leading "~" back to the real home before navigating.
  const displayPath = underHome ? "~" + rest : fsPath;
  const submitEdit = (raw: string) => {
    let path = raw.trim();
    if (home !== undefined) {
      if (path === "~") path = home;
      else if (path.startsWith("~/")) path = home + path.slice(1);
    }
    if (path.length > 1) path = path.replace(/\/+$/, ""); // drop trailing slash, keep lone "/"
    setEditing(false);
    // No isDir hint — a typed path's kind is unknown; the destination view's
    // stat/error handling covers a bad path (see plan: no pre-validation).
    if (path) navigate(path);
  };
  const pieces: React.ReactNode[] = [
    <a
      key="root"
      href="#"
      className={"path-crumb" + (parts.length === 0 ? " last" : "")}
      onClick={(e) => {
        e.preventDefault();
        navigatePreservingMode(underHome ? home : "/");
      }}
    >
      {underHome ? "~" : "/"}
    </a>,
  ];
  // A Windows path's first segment is the drive ("C:"); its crumb must target
  // "C:/" (bare "C:" is cwd-relative to os.stat) and later segments append
  // without re-rooting at "/".
  const isDrive = !underHome && /^[A-Za-z]:$/.test(parts[0] || "");
  let acc = underHome ? home : "";
  parts.forEach((part, i) => {
    if (i === 0 && isDrive) acc = part + "/";
    else acc = acc + (acc.endsWith("/") ? "" : "/") + part;
    const target = acc;
    const isLast = i === parts.length - 1;
    // Separator only between segments (root already carries the leading
    // slash) — matches the panel path bar's tight `/Users/name/...` format.
    // The "~" crumb carries no slash, so its first segment needs one too.
    if (i > 0 || underHome) pieces.push(<span key={"sep" + i} className="path-crumb-sep">/</span>);
    if (isLast) {
      pieces.push(
        <span key={target} className="path-crumb last" title={part}>
          {part}
        </span>
      );
    } else {
      pieces.push(
        <a
          key={target}
          href="#"
          className="path-crumb"
          title={part}
          onClick={(e) => {
            e.preventDefault();
            navigatePreservingMode(target);
          }}
        >
          {part}
        </a>
      );
    }
  });

  return (
    <>
      {editing ? (
        <input
          className="crumb-edit"
          defaultValue={displayPath}
          spellCheck={false}
          autoFocus
          onFocus={(e) => e.target.select()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submitEdit(e.currentTarget.value);
            } else if (e.key === "Escape") {
              e.preventDefault();
              setEditing(false); // discard, no navigation
            }
          }}
          onBlur={() => setEditing(false)} // a stray click cancels rather than commits
        />
      ) : (
        // A click on the strip itself (whitespace right of the crumbs), not on
        // a crumb or the reveal button, switches to the editable path.
        <div
          className="crumbs"
          ref={crumbsRef}
          onWheel={onWheel}
          onClick={(e) => {
            if (e.target === e.currentTarget) setEditing(true);
          }}
        >
          {pieces}
          <RevealButton fsPath={fsPath} />
        </div>
      )}
      <CrumbActions name={renderedTitle || basename(fsPath)} onSplit={(dir) => enterPanel(fsPath, dir)} />
    </>
  );
}

export function StaticBreadcrumb({ label }: { label: string }) {
  return (
    <>
      <div className="crumbs">
        <span className="current">{label}</span>
      </div>
      <CrumbActions name={label} />
    </>
  );
}
