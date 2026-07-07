// Crumb bar + "+ Bookmark" / "Update bookmark" / "Split" buttons.
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
// name; `onSplit` present adds the panel-mode entry point (the layout modes
// themselves pass none).
interface CrumbActionsProps {
  name: string;
  onSplit?: () => void;
}

function CrumbActions({ name, onSplit }: CrumbActionsProps) {
  const urlVersion = useUrlVersion();
  const bookmarksVersion = useBookmarksVersion();
  const showUpdate = useUpdateButton(urlVersion, bookmarksVersion);
  const starred = allBookmarks().some((b) => b.url === currentUrl());

  const onBookmark = () => {
    addBookmark(name, currentUrl());
    notifyBookmarksChanged();
  };
  const onUpdate = () => {
    const armed = getArmedBookmark();
    if (!armed) return;
    const url = currentUrl();
    updateBookmarkUrl(armed.id, url);
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
        <button id="split-btn" className="star-btn" title="Open this view in panel mode" onClick={onSplit}>
          Split
        </button>
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

// Split entry (LM-10): two side-by-side panes, both showing the current view —
// entering split mode with a single pane looked like nothing happened. Each
// pane carries the `_`-prefixed params and the listing sort/order pane-local
// (inside its `_layout` segment); every other param joins the merged top-level
// pool shared by all panes (LM-3). Read via splitShellSearch, not raw
// URLSearchParams (D51): a stray `_layout=(…)` span carries literal `&` that
// would parse as junk keys; the codec read excludes the span, so it is
// dropped — the strict-read semantics.
function enterPanel(fsPath: string): void {
  const { params } = splitShellSearch(location.search);
  const paneLocal = new URLSearchParams();
  const merged: [string, string][] = [];
  for (const [k, v] of params) {
    if (k.startsWith("_") || k === "sort" || k === "order") paneLocal.set(k, v);
    else merged.push([k, v]);
  }
  const paneQ = paneLocal.toString();
  const seg = encodePaneSegment(fsPath, paneQ ? "?" + paneQ : "");
  navigateUrl(panelUrl(seg + "," + seg, merged));
}

export function Breadcrumb({ fsPath }: { fsPath: string }) {
  const parts = fsPath.split("/").filter((s) => s.length > 0);
  const pieces: React.ReactNode[] = [];
  let acc = "";
  parts.forEach((part, i) => {
    acc += "/" + part;
    const target = acc;
    const isLast = i === parts.length - 1;
    pieces.push(<span key={"sep" + i} className="sep">/</span>);
    if (isLast) {
      pieces.push(
        <span key={target} className="current">
          {part}
        </span>
      );
    } else {
      pieces.push(
        <a
          key={target}
          href="#"
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
        <a
          href="#"
          onClick={(e) => {
            e.preventDefault();
            navigate("/");
          }}
        >
          /
        </a>
        {pieces}
      </div>
      <CrumbActions name={basename(fsPath)} onSplit={() => enterPanel(fsPath)} />
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
