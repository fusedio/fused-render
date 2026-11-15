// The home search's "Fused assets" group: hits from the public `fused-asset`
// bucket's index (/api/remote-index/rank, platform/lib/remote.ts), rendered as
// their own block BELOW the local file rows and above the sticky AI row, inside
// the same listbox — so ↑/↓ walks local rows and bucket rows as one list.
// FilesSearch owns the keyboard model; bucket rows are reached only by arrowing
// or clicking, never pre-selected (a bare Enter never opens one).
//
// Kept out of FilesSearch's own request machinery on purpose: the bucket
// request runs in PARALLEL with the local `indexRank`, never waits for it and
// never touches `pending`/`failure`/`settled` — those arm the AI row and drive
// the result note, which stay about the local index alone. A failed or
// `ready:false` answer renders nothing: the group is a bonus, not a verdict.
//
// Gated on the canvases flag (platform/lib/canvases-flag, read by FilesSearch
// and passed in): off → no request, no rows.
import { useEffect, useRef, useState } from "react";
import { Cloud } from "lucide-react";
import { spaLinkProps } from "@platform/lib/router";
import { formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { substringMatch } from "@platform/lib/fuzzy";
import { INSTANT_DEBOUNCE_MS, QueryMemo } from "@platform/lib/instant-search";
import { REMOTE_WARN_BYTES, remoteRank, type RemoteRankHit } from "@platform/lib/remote";
import { iconForEntry } from "@platform/ui/FileIcons";
import {
  HOME_RESULT_CAP,
  RANK_FETCH_LIMIT,
  nameStart,
  positionsWithin,
} from "@apps/explorer/lib/home-search";
import { renderHighlight, renderHighlightPath } from "@apps/explorer/listing/bits";

/** Bucket rows shown before "Show more" — the same cap the local rows use. */
export const REMOTE_GROUP_CAP = HOME_RESULT_CAP;

const REMOTE_BUCKET_ADDR = "s3://fused-asset";
const HEAD_ID = "fh-remote-head";
const NO_HITS: RemoteRankHit[] = [];

export interface RemoteHits {
  /** Every hit fetched for the last answered query. */
  hits: RemoteRankHit[];
  /** The rows rendered — and the rows the keyboard walks: the first
   *  REMOTE_GROUP_CAP, or all of `hits` once "Show more" was pressed. */
  shown: RemoteRankHit[];
  /** `hits` answer an EARLIER query; the answer for this one is in flight.
   *  Held on screen (dimmed) rather than blanked, the way local rows are. */
  held: boolean;
  showMore: () => void;
}

/**
 * The bucket answer for `query`, with the local search's discipline: a trailing
 * INSTANT_DEBOUNCE_MS, abort-never-queue, a backspace memo, and the previous
 * answer HELD (flagged `held`) while the next one is in flight, so the group
 * does not vanish and reappear on every keystroke. A failure or `ready:false`
 * replaces the held rows with nothing.
 */
export function useRemoteHits(
  query: string,
  searchable: boolean,
  ranked: boolean,
  /** The canvases flag (platform/lib/canvases-flag), read by the caller. */
  enabled: boolean,
): RemoteHits {
  // `ranked` too: an answer in the other order is held (dimmed, never a
  // default) until the re-asked one lands.
  const [answer, setAnswer] = useState<{
    query: string;
    ranked: boolean;
    hits: RemoteRankHit[];
  } | null>(null);
  // Which answered query "Show more" was pressed for — the next answer
  // collapses again without an effect to reset it.
  const [expandedFor, setExpandedFor] = useState<string | null>(null);
  const memo = useRef(new QueryMemo<RemoteRankHit[]>());
  const inflight = useRef<AbortController | null>(null);

  // A remembered answer from before the preference flipped is in the wrong
  // order. Declared before the fetch effect so it clears first.
  useEffect(() => {
    memo.current.clear();
  }, [ranked, enabled]);
  useEffect(() => () => inflight.current?.abort(), []);

  useEffect(() => {
    inflight.current?.abort();
    if (!enabled || !searchable) {
      // Nothing held across a Clear or a too-short query: the next query must
      // not open on the previous one's rows.
      setAnswer(null);
      return;
    }
    const remembered = memo.current.get(query);
    if (remembered) {
      setAnswer({ query, ranked, hits: remembered });
      return;
    }
    const run = () => {
      inflight.current?.abort();
      const ctl = new AbortController();
      inflight.current = ctl;
      remoteRank(query, { signal: ctl.signal, limit: RANK_FETCH_LIMIT, ranked }).then(
        (res) => {
          if (ctl.signal.aborted) return;
          // `ready:false` (no store yet, still ingesting) is not remembered:
          // the next keystroke should ask again once the index lands.
          if (res.ready) memo.current.put(query, res.hits);
          setAnswer({ query, ranked, hits: res.ready ? res.hits : NO_HITS });
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          // Silent: a bucket outage must never put an error over local results.
          setAnswer({ query, ranked, hits: NO_HITS });
        },
      );
    };
    const timer = window.setTimeout(run, INSTANT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, searchable, ranked, enabled]);

  const live = enabled && searchable && answer !== null;
  const hits = live ? answer.hits : NO_HITS;
  const answered = answer?.query ?? null;
  const shown = expandedFor !== null && expandedFor === answered ? hits : hits.slice(0, REMOTE_GROUP_CAP);
  return {
    hits,
    shown,
    held: live && (answer.query !== query || answer.ranked !== ranked),
    showMore: () => setExpandedFor(answered),
  };
}

function RemoteRow({
  hit,
  query,
  active,
  held,
  id,
  onHover,
}: {
  hit: RemoteRankHit;
  query: string;
  active: boolean;
  held: boolean;
  id: string;
  onHover: () => void;
}) {
  // The key plays the local row's `rel`: name cell = its last segment, path
  // cell = the folder above it, each with the match rebased into it. A query
  // that is not a plain substring of the key (multi-word, a glob) renders
  // unhighlighted, the same honest-over-lucky posture as the local rows.
  // A folder hit's key ends in "/": split (and highlight) the key WITHOUT it,
  // so the name cell is the folder's own last segment.
  const isDir = hit.is_dir;
  const key = isDir ? hit.key.replace(/\/+$/, "") : hit.key;
  const from = nameStart(key);
  const name = key.slice(from) || key;
  const folder = from > 0 ? key.slice(0, from - 1) : "";
  const marks = substringMatch(query, key)?.positions ?? [];
  const namePos = positionsWithin(marks, from, key.length - from);
  const folderPos = positionsWithin(marks, 0, folder.length);
  const big = !isDir && hit.size !== null && hit.size > REMOTE_WARN_BYTES;
  return (
    <li
      role="option"
      id={id}
      aria-selected={active}
      aria-describedby={HEAD_ID}
      className={"fh-remote-item" + (held ? " is-held" : "")}
    >
      <a
        className={"fh-result fh-remote-row" + (active ? " is-active" : "")}
        title={hit.url}
        onMouseMove={onHover}
        {...spaLinkProps(hit.url, { isDir })}
      >
        <span className="fh-result-icon" aria-hidden="true">
          {iconForEntry(name, isDir)}
        </span>
        <span className="fh-result-name">{renderHighlight(name, namePos)}</span>
        <span className="fh-result-path">{renderHighlightPath(folder, folderPos)}</span>
        <span className="fh-result-meta">
          <span className="fh-remote-cloud" title="In the fused-asset bucket">
            <Cloud size={13} strokeWidth={2} aria-hidden="true" />
          </span>
          {!isDir && (
            <span
              className={"fh-remote-size" + (big ? " is-big" : "")}
              title={big ? "Large file: it is downloaded in full to view" : undefined}
            >
              {formatSize(hit.size)}
            </span>
          )}
          {!isDir && hit.mtime !== null && (
            <span className="fh-result-date" title={formatMtimeFull(hit.mtime)}>
              {formatMtime(hit.mtime)}
            </span>
          )}
        </span>
      </a>
    </li>
  );
}

/**
 * The group's <li>s, for the inside of FilesSearch's listbox: a heading, the
 * shown rows, and a "Show more" that reveals the rest already fetched. The
 * heading and the button are presentation, not rows — the keyboard model
 * counts `shown` only. `rowOffset` is the unified index of the first bucket
 * row (after the open row and the local rows).
 */
export function RemoteHitsGroup({
  remote,
  query,
  rowOffset,
  current,
  onHover,
}: {
  remote: RemoteHits;
  query: string;
  rowOffset: number;
  current: number | null;
  onHover: (row: number) => void;
}) {
  if (remote.hits.length === 0) return null;
  const hidden = remote.hits.length - remote.shown.length;
  const held = remote.held ? " is-held" : "";
  return (
    <>
      <li role="presentation" className={"fh-remote-item fh-remote-head" + held} id={HEAD_ID}>
        <span className="fh-remote-title">Fused assets</span>
        <span className="fh-remote-addr">{REMOTE_BUCKET_ADDR}</span>
      </li>
      {remote.shown.map((hit, i) => {
        const row = rowOffset + i;
        return (
          <RemoteRow
            key={hit.url}
            hit={hit}
            query={query}
            active={current === row}
            held={remote.held}
            id={"fh-row-" + row}
            onHover={() => onHover(row)}
          />
        );
      })}
      {hidden > 0 && (
        <li role="presentation" className={"fh-remote-item fh-remote-more" + held}>
          <button
            type="button"
            className="fh-link-button"
            // Keep the caret in the search box: the arrow keys live there.
            onMouseDown={(e) => e.preventDefault()}
            onClick={remote.showMore}
          >
            Show {hidden.toLocaleString()} more
          </button>
        </li>
      )}
    </>
  );
}
