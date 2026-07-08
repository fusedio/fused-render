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
      title="Show in the system file manager"
      onClick={() => revealInFileManager(fsPath)}
    >
      <svg width="18" height="18" viewBox="0 -0.5 25 25" fill="currentColor">
        <path d="M12.5 6.25C12.9142 6.25 13.25 5.91421 13.25 5.5C13.25 5.08579 12.9142 4.75 12.5 4.75V6.25ZM20.25 12.5C20.25 12.0858 19.9142 11.75 19.5 11.75C19.0858 11.75 18.75 12.0858 18.75 12.5H20.25ZM19.5 6.25C19.9142 6.25 20.25 5.91421 20.25 5.5C20.25 5.08579 19.9142 4.75 19.5 4.75V6.25ZM15.412 4.75C14.9978 4.75 14.662 5.08579 14.662 5.5C14.662 5.91421 14.9978 6.25 15.412 6.25V4.75ZM20.25 5.5C20.25 5.08579 19.9142 4.75 19.5 4.75C19.0858 4.75 18.75 5.08579 18.75 5.5H20.25ZM18.75 9.641C18.75 10.0552 19.0858 10.391 19.5 10.391C19.9142 10.391 20.25 10.0552 20.25 9.641H18.75ZM20.0303 6.03033C20.3232 5.73744 20.3232 5.26256 20.0303 4.96967C19.7374 4.67678 19.2626 4.67678 18.9697 4.96967L20.0303 6.03033ZM11.9697 11.9697C11.6768 12.2626 11.6768 12.7374 11.9697 13.0303C12.2626 13.3232 12.7374 13.3232 13.0303 13.0303L11.9697 11.9697ZM12.5 4.75H9.5V6.25H12.5V4.75ZM9.5 4.75C6.87665 4.75 4.75 6.87665 4.75 9.5H6.25C6.25 7.70507 7.70507 6.25 9.5 6.25V4.75ZM4.75 9.5V15.5H6.25V9.5H4.75ZM4.75 15.5C4.75 18.1234 6.87665 20.25 9.5 20.25V18.75C7.70507 18.75 6.25 17.2949 6.25 15.5H4.75ZM9.5 20.25H15.5V18.75H9.5V20.25ZM15.5 20.25C18.1234 20.25 20.25 18.1234 20.25 15.5H18.75C18.75 17.2949 17.2949 18.75 15.5 18.75V20.25ZM20.25 15.5V12.5H18.75V15.5H20.25ZM19.5 4.75H15.412V6.25H19.5V4.75ZM18.75 5.5V9.641H20.25V5.5H18.75ZM18.9697 4.96967L11.9697 11.9697L13.0303 13.0303L20.0303 6.03033L18.9697 4.96967Z" />
      </svg>
    </button>
  );
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
// pane carries the `_`-prefixed params and the shell-owned view state —
// listing sort/order and the `listing` directory-override (PT-13/D65) —
// pane-local (inside its `_layout` segment); every other param joins the
// merged top-level pool shared by all panes (LM-3). Pane iframes only read
// segment-local query, so shell state left in the merged pool would be
// silently dropped. Read via splitShellSearch, not raw URLSearchParams (D51):
// a stray `_layout=(…)` span carries literal `&` that would parse as junk
// keys; the codec read excludes the span, so it is dropped — the strict-read
// semantics.
function enterPanel(fsPath: string): void {
  const { params } = splitShellSearch(location.search);
  const paneLocal = new URLSearchParams();
  const merged: [string, string][] = [];
  for (const [k, v] of params) {
    if (k.startsWith("_") || k === "sort" || k === "order" || k === "listing") paneLocal.set(k, v);
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
        <RevealButton fsPath={fsPath} />
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
