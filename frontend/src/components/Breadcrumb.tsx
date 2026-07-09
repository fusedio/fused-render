// Crumb bar + "+ Bookmark" / "Update bookmark" / split right/down buttons.
// Rendered by every view: path crumbs for listing/preview, a static label for
// the layout modes (LM-11 / TM-9 — ★/update still operate on currentUrl()).
import React, { useEffect, useState } from "react";
import { navigate, navigateUrl, currentUrl, IS_EMBED } from "../lib/router";
import { basename } from "../lib/format";
import {
  addBookmark,
  allBookmarks,
  updateBookmarkUrl,
  armBookmark,
  disarmBookmark,
  getArmedBookmark,
} from "../lib/bookmarks";
import { useUrlVersion, useBookmarksVersion, notifyBookmarksChanged } from "../lib/hooks";
import { encodePaneSegment, splitShellSearch } from "../lib/layout-codec";
import { panelUrl } from "../views/Panel";
import { ShareIcon } from "./ShareIcon";
import { SplitRightIcon, SplitDownIcon } from "./SplitIcons";

// True when two query strings carry the same decoded `_layout` and the same
// key/value multiset of remaining params, ignoring encoding and ordering
// differences. `_layout` may contain literal `&` (D51), so both sides go
// through the codec's splitShellSearch, never raw URLSearchParams.
function sameSearch(a: string, b: string): boolean {
  const norm = (s: string) => {
    const { layout, params } = splitShellSearch(s);
    return JSON.stringify([layout, [...params].sort()]);
  };
  return norm(a) === norm(b);
}

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

    const qIdx = armed.url.indexOf("?");
    const armedPathname = qIdx === -1 ? armed.url : armed.url.slice(0, qIdx);
    const armedSearch = qIdx === -1 ? "" : armed.url.slice(qIdx);

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

// Share glyph (arrow leaving a rounded box), sits at the end of the crumb
// trail and reveals the current path in the OS file manager.
function RevealButton({ fsPath }: { fsPath: string }) {
  return (
    <button
      id="open-in-finder"
      className="reveal-btn"
      title="Open in Finder"
      onClick={() => revealInFileManager(fsPath)}
    >
      <ShareIcon />
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

export function Breadcrumb({ fsPath }: { fsPath: string }) {
  const parts = fsPath.split("/").filter((s) => s.length > 0);
  const pieces: React.ReactNode[] = [
    <a
      key="root"
      href="#"
      className={"path-crumb" + (parts.length === 0 ? " last" : "")}
      onClick={(e) => {
        e.preventDefault();
        navigate("/");
      }}
    >
      /
    </a>,
  ];
  // A Windows path's first segment is the drive ("C:"); its crumb must target
  // "C:/" (bare "C:" is cwd-relative to os.stat) and later segments append
  // without re-rooting at "/".
  const isDrive = /^[A-Za-z]:$/.test(parts[0] || "");
  let acc = "";
  parts.forEach((part, i) => {
    if (i === 0 && isDrive) acc = part + "/";
    else acc = acc + (acc.endsWith("/") ? "" : "/") + part;
    const target = acc;
    const isLast = i === parts.length - 1;
    // Separator only between segments (root already carries the leading
    // slash) — matches the panel path bar's tight `/Users/name/...` format.
    if (i > 0) pieces.push(<span key={"sep" + i} className="path-crumb-sep">/</span>);
    if (isLast) {
      pieces.push(
        <span key={target} className="path-crumb last">
          {part}
        </span>
      );
    } else {
      pieces.push(
        <a
          key={target}
          href="#"
          className="path-crumb"
          onClick={(e) => {
            e.preventDefault();
            navigate(target);
          }}
        >
          {part}
        </a>
      );
    }
  });

  return (
    <>
      <div className="crumbs">
        {pieces}
        <RevealButton fsPath={fsPath} />
      </div>
      <CrumbActions name={basename(fsPath)} onSplit={(dir) => enterPanel(fsPath, dir)} />
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
