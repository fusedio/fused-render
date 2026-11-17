// The S3 folder view's search box (RemoteView.tsx's RemoteListing): searches
// the folder's whole subtree in the `fused-asset` bucket index
// (/api/remote-index/rank with `under`, platform/lib/remote.ts), the same
// engine the Home search's "Fused assets" group ranks with — so a query means
// the same thing in both places, only scoped here.
//
// Deliberately NOT the local folder's merged field (SearchField.tsx): that one
// is a location bar, a completion dropdown and an index/walk escalation ladder
// over the user's own disk, and claims the crumb bar to do it. None of that
// applies to a read-only bucket prefix. This is the row the merged field
// renders as when it has no crumb bar to move into (the app builder's), with
// the same classes, so it looks like the folder search people already know:
// the box as the column's first strip, results in the listing's own table.
//
// Request discipline follows RemoteHitsGroup's `useRemoteHits`: a trailing
// INSTANT_DEBOUNCE_MS, abort-never-queue, a per-folder backspace memo, and the
// previous answer HELD while the next is in flight, so rows do not blank out
// on every keystroke.
import { useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { INSTANT_DEBOUNCE_MS, QueryMemo } from "@platform/lib/instant-search";
import { RANK_FETCH_LIMIT } from "@apps/explorer/lib/home-search";
import { remoteRank, type RemoteRankHit } from "@platform/lib/remote";

export type RemoteFolderSearch =
  /** No query: the plain listing. */
  | { status: "idle" }
  /** A query, no answer for it (or any earlier one) yet. */
  | { status: "searching" }
  /** The index is not loaded yet (or the integration is off). */
  | { status: "not_ready" }
  | { status: "error"; message: string }
  | {
      status: "done";
      hits: RemoteRankHit[];
      truncated: boolean;
      /** These rows answer an EARLIER query; this one is in flight. */
      held: boolean;
    };

interface Answer {
  query: string;
  ranked: boolean;
  ready: boolean;
  hits: RemoteRankHit[];
  truncated: boolean;
}

/** `folderUrl` is the folder's `s3://` prefix address (ending in "/"). */
export function useRemoteFolderSearch(folderUrl: string, query: string, ranked: boolean): RemoteFolderSearch {
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<{ query: string; message: string } | null>(null);
  const memo = useRef(new QueryMemo<Omit<Answer, "query" | "ranked">>());
  const inflight = useRef<AbortController | null>(null);

  // Another folder, or the other order: nothing remembered applies.
  useEffect(() => {
    memo.current.clear();
    setAnswer(null);
  }, [folderUrl, ranked]);
  useEffect(() => () => inflight.current?.abort(), []);

  const text = query.trim();
  useEffect(() => {
    inflight.current?.abort();
    setError(null);
    if (!text) {
      setAnswer(null);
      return;
    }
    const remembered = memo.current.get(query);
    if (remembered) {
      setAnswer({ query, ranked, ...remembered });
      return;
    }
    const run = () => {
      const ctl = new AbortController();
      inflight.current = ctl;
      remoteRank(query, { signal: ctl.signal, limit: RANK_FETCH_LIMIT, ranked, under: folderUrl }).then(
        (res) => {
          if (ctl.signal.aborted) return;
          const got = { ready: res.ready, hits: res.ready ? res.hits : [], truncated: res.truncated };
          // `ready:false` is not remembered: ask again once the index lands.
          if (res.ready) memo.current.put(query, got);
          setAnswer({ query, ranked, ...got });
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          setAnswer(null);
          setError({ query, message: err.message || "Search failed" });
        },
      );
    };
    const timer = window.setTimeout(run, INSTANT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // `text` is derived from `query`; listed so a whitespace-only edit that
    // empties it still clears.
  }, [query, text, ranked, folderUrl]);

  if (!text) return { status: "idle" };
  if (error && error.query === query) return { status: "error", message: error.message };
  if (!answer) return { status: "searching" };
  if (!answer.ready) return { status: "not_ready" };
  return {
    status: "done",
    hits: answer.hits,
    truncated: answer.truncated,
    held: answer.query !== query || answer.ranked !== ranked,
  };
}

/** The pinned count: "12 matches", "200+ matches", or null while there is
 *  nothing settled to count. */
export function remoteSearchCount(search: RemoteFolderSearch): string | null {
  if (search.status !== "done" || search.held) return null;
  const n = search.hits.length;
  if (search.truncated) return `${n.toLocaleString()}+ matches`;
  return n === 1 ? "1 match" : `${n.toLocaleString()} matches`;
}

export function RemoteSearchRow({
  query,
  setQuery,
  inputRef,
  count,
  searching,
  onKeyDown,
}: {
  query: string;
  setQuery: (q: string) => void;
  inputRef: RefObject<HTMLInputElement>;
  count: string | null;
  /** A request for the current query is outstanding. */
  searching: boolean;
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
}) {
  const hasClear = query !== "";
  const hasPin = count !== null || searching;
  return (
    <div className={"listing-search" + (query.trim() ? " searching" : "")}>
      <div className={"listing-search-box" + (hasPin ? " has-pin" : "") + (hasClear ? " has-clear" : "")}>
        <span className="listing-search-mode search" aria-hidden="true">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="7" />
            <line x1="16.5" y1="16.5" x2="21" y2="21" />
          </svg>
        </span>
        <input
          ref={inputRef}
          type="search"
          className="listing-search-input"
          placeholder="Search this folder…"
          aria-label="Search this S3 folder"
          spellCheck={false}
          autoComplete="off"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {searching && count === null && <span className="listing-search-spinner" aria-hidden="true" />}
        {count !== null && (
          <span className="listing-search-count" title={count} aria-label={count}>
            <span className="listing-search-count-base">{count}</span>
          </span>
        )}
        {hasClear && (
          <button
            type="button"
            className="listing-search-clear"
            aria-label="Clear search"
            onMouseDown={(e) => {
              // Keep the caret in the box: the arrow keys live there.
              e.preventDefault();
              setQuery("");
              inputRef.current?.focus();
            }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
