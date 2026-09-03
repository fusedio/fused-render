// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useEffect, useRef, useState } from "react";
import { navigate, navigateUrl, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { basename, formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config, ClaudeSessionFolder, GitRepos, IndexStatus } from "@platform/lib/api";
import { searchCaveat } from "@apps/explorer/listing/index-caveat";
import {
  getClaudeSessionFolders,
  getGitRepos,
  indexRank,
  startIndexScan,
  statPath,
} from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import {
  fsMutationCount,
  indexLifecycleCount,
  indexRescanPending,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { hydrateRecents, loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { RecentPreviewCard, FolderPreviewCard } from "@apps/explorer/BookmarkCards";
import { describeSpec, runAiSearch, type AiSearchResult } from "@apps/explorer/lib/ai-search";
import {
  refreshIsPending,
  reposMessage,
  reposNeedsIndexPoll,
  reposStaleNote,
  reposView,
} from "@apps/explorer/lib/repos";
import { useIndexStatus } from "@platform/lib/index-status";
import {
  PENDING_INDICATOR_MS,
  QueryMemo,
  STALE_CLEAR_MS,
  searchDelay,
} from "@platform/lib/instant-search";
import {
  MIN_QUERY_CHARS,
  RANK_FETCH_LIMIT,
  activeRow,
  aiSearchUsable,
  answerFrom,
  formatElapsed,
  homeCountNote,
  indexGap,
  isAiRow,
  isOpenRow,
  nameStart,
  narrowAnswer,
  noteAnswer,
  pathShortcut,
  positionsWithin,
  rankingSettled,
  redirectsToSearch,
  scanStarting,
  stepHighlight,
  submitRow,
  type HomeAnswer,
  type HomeHit,
  type PendingScan,
  type RowModel,
} from "@apps/explorer/lib/home-search";
import { renderHighlight } from "@apps/explorer/listing/bits";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// How many cards a tab shows before "Show more" — flat count, not a row
// multiple, so it's the same rule for every tab regardless of how many columns
// the grid happens to lay out at the current width.
// 9 fills a 3×3 grid at the layout's usual three columns.
const MAX_CARDS = 9;

type LaunchTab = "recents" | "sessions" | "repos";

// -- The home search bar ------------------------------------------------------
//
// A plain file search that answers while you type, with AI search as one row
// at the bottom of the results rather than the only way in. Typing ranks the
// home root's index corpus locally (lib/home-search); picking the last row —
// "Search with AI" — spends the model call the old composer spent on every
// Enter, including on queries that were just a filename.
//
// The two modes never blur into each other: AI results REPLACE the instant
// list (with the spec echo that makes a wrong interpretation visible), and
// editing the query drops back to instant results. Only a committed AI search
// touches the URL — mirroring every keystroke into ?q= would fill the history
// with half-typed words and re-run a model call on reload.

// What the AI half of the box is doing. `off` is the normal state: the results
// are the index's, and the AI row is an offer.
type AiPhase =
  | { status: "off" }
  | { status: "running"; query: string }
  | { status: "done"; query: string; result: AiSearchResult }
  | { status: "failed"; query: string; message: string };

const AI_OFF: AiPhase = { status: "off" };

// What the on-mount idle warm asks for. Nothing renders it — it exists to pay
// the server's cold cost (the duckdb import, the ignore-root discovery, the
// gitignore verdict pool) before the first keystroke rather than on it.
//
// It matches NOTHING on purpose, and that is what makes it a real warm: the
// server escalates from a cheap substring pass to a subsequence regex only when
// the cheap pass cannot fill the limit, so a query WITH hits leaves the
// expensive plan cold. This one runs both passes and comes back empty.
const WARM_QUERY = "zqxjv";

function MagnifierIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3l1.8 4.9L18.7 9.7l-4.9 1.8L12 16.4l-1.8-4.9L5.3 9.7l4.9-1.8L12 3z" />
      <path d="M18 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8z" />
    </svg>
  );
}

// One file hit. An <a> (not a button) so cmd/ctrl-click opens it the way every
// other path in this app does.
function FileRow({
  hit,
  home,
  active,
  id,
  onHover,
}: {
  hit: HomeHit;
  home: string;
  active: boolean;
  id: string;
  onHover: () => void;
}) {
  const display = hit.path.startsWith(home + "/") ? "~/" + hit.rel : hit.path;
  // What matched, marked in both cells. Positions index the REL, so each cell
  // rebases them: the name cell to its start inside the rel, the path cell past
  // the "~/" the display adds (an absolute display path is not the rel at all,
  // so it gets none). AI hits carry no positions and simply render plain.
  const marks = hit.positions ?? [];
  const from = nameStart(hit.rel);
  const namePos = positionsWithin(marks, from, hit.rel.length - from);
  const pathPos = display.endsWith(hit.rel)
    ? positionsWithin(marks, 0, hit.rel.length).map((p) => p + display.length - hit.rel.length)
    : [];
  const name = basename(hit.path);
  return (
    <li role="option" id={id} aria-selected={active}>
      <a
        className={"fh-result" + (active ? " is-active" : "")}
        href={urlForFsPath(hit.path)}
        title={hit.path}
        onMouseMove={onHover}
        onClick={(e) => {
          if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
            return;
          e.preventDefault();
          navigate(hit.path, { isDir: hit.is_dir });
        }}
      >
        <span className="fh-result-icon" aria-hidden="true">
          {iconForEntry(name, hit.is_dir)}
        </span>
        <span className="fh-result-name">{renderHighlight(name, namePos)}</span>
        <span className="fh-result-path">{renderHighlight(display, pathPos)}</span>
        <span className="fh-result-meta">
          {hit.is_dir ? "" : formatSize(hit.size)}
          {hit.mtime !== null && (
            <span className="fh-result-date" title={formatMtimeFull(hit.mtime)}>
              {formatMtime(hit.mtime)}
            </span>
          )}
        </span>
      </a>
    </li>
  );
}

// Row 0, when the query is really an ADDRESS that resolved: an ACTION, like
// the AI row below, but at the OPPOSITE end of the list and for the opposite
// reason — this one is offered because ranking would have been noise on a
// literal path, not because ranking came up empty. No query echo (the
// resolved path already answers "what did this address mean"), and the icon
// follows `isDir` the same way a file row's does.
function OpenRow({
  path,
  isDir,
  active,
  id,
  onOpen,
}: {
  path: string;
  isDir: boolean;
  active: boolean;
  id: string;
  onOpen: () => void;
}) {
  return (
    <li role="option" id={id} aria-selected={active}>
      <button
        type="button"
        className={"fh-result fh-open-row" + (active ? " is-active" : "")}
        onClick={onOpen}
      >
        <span className="fh-result-icon" aria-hidden="true">
          {iconForEntry(basename(path), isDir)}
        </span>
        <span className="fh-result-name">Open</span>
        <span className="fh-result-path">{path}</span>
        <span className="fh-result-meta">
          <kbd>↵</kbd>
        </span>
      </button>
    </li>
  );
}

// The last row: an ACTION, not a hit. Deliberately unlike the file rows above
// it (accent glyph, no path, no size) because activating it costs a model call
// and a wait, and because on a zero-hit query it is the only thing on screen.
//
// It does NOT reuse `.fh-result-path` / `.fh-result-meta` for the query echo
// and the `↵` hint, even though it reuses `.fh-result`/`.fh-result-name` for
// layout: those two classes are the file rows' PATH and DATE/SIZE columns, and
// borrowing them here made the query — "parquet" — land in the same visual
// column as a file's `~/Work/...` path, and the `↵` align with a date. Nothing
// about this row is a file, so it gets its own two classes instead
// (`.fh-ai-query`, `.fh-ai-hint`; preferences.css).
//
// Deliberately NOT hoverable, unlike the file rows: setting the highlight on
// mousemove meant nudging the pointer across the list armed Enter to spend a
// model call. A pointer merely crossing a row must not arm a paid action —
// reaching this one takes an arrow key or a click.
function AiActionRow({
  query,
  active,
  running,
  id,
  onRun,
}: {
  query: string;
  active: boolean;
  running: boolean;
  id: string;
  onRun: () => void;
}) {
  return (
    // `fh-ai-row` (sticky/background/border-top — preferences.css) lives on
    // THIS <li>, not the button inside it: `position: sticky` is bound to its
    // element's own containing block, and a <li> that wraps exactly one
    // full-width button has no room of its own to offset within — sticky on
    // the button was a no-op that never actually stuck. `fh-ai-action`
    // carries what genuinely IS about the button (the accent name, the
    // disabled cursor).
    <li className="fh-ai-row" role="option" id={id} aria-selected={active}>
      <button
        type="button"
        className={"fh-result fh-ai-action" + (active ? " is-active" : "")}
        disabled={running}
        onClick={onRun}
      >
        <span className="fh-result-icon fh-ai-glyph" aria-hidden="true">
          <SparkIcon />
        </span>
        <span className="fh-result-name">Search with AI</span>
        <span className="fh-ai-query">“{query}”</span>
        {/* The badge only claims Enter when Enter actually runs THIS row.
            `activeRow` pre-selects the AI row exactly when it is the only
            content on screen (settled, zero file hits) — every other time
            Enter opens the top file hit instead, and showing `↵` here would
            be the strongest affordance in the row pointing the wrong way.
            The span stays in the layout (not conditionally rendered) even
            empty, so the row's columns do not reflow as the highlight moves
            onto or off it. */}
        <span className="fh-ai-hint">
          {running ? "Asking…" : active ? <kbd>↵</kbd> : null}
        </span>
      </button>
    </li>
  );
}

// The "Understood as: …" echo above AI results — the one place a wrong
// interpretation becomes visible instead of silently shaping the list.
function AiResults({ home, query, result }: { home: string; query: string; result: AiSearchResult }) {
  const summary = describeSpec(result.spec);
  return (
    <div className="fh-panel">
      <p className="fh-search-summary">
        <span className="fh-ai-badge">
          <SparkIcon /> AI
        </span>
        {summary ? `Understood as: ${summary}` : "No filters — showing closest matches."}
        {result.truncated && " · Broad query: showing the first slice of matches."}
      </p>
      {result.hits.length ? (
        <ul className="fh-results" role="listbox" aria-label="AI search results">
          {result.hits.map((h) => (
            <FileRow
              key={h.path}
              home={home}
              active={false}
              id={"fh-ai-hit-" + h.path}
              onHover={() => {}}
              hit={{
                path: h.path,
                rel: h.path.startsWith(home + "/") ? h.path.slice(home.length + 1) : h.path,
                is_dir: h.is_dir,
                size: h.size,
                mtime: h.mtime,
              }}
            />
          ))}
        </ul>
      ) : (
        <p className="fh-empty">
          AI search found nothing for “{query}”. Try different words, or fewer of them.
        </p>
      )}
    </div>
  );
}

// Exported for the shell Home page (/home), which reuses this box as its hero.
export function FilesSearch({
  home,
  initialQuery,
  indexScan,
  onActiveChange,
  onScanRequested,
}: {
  home: string;
  initialQuery: string;
  /** The shared index poll (see FilesHome) — this box adds no poller of its own. */
  indexScan: IndexStatus | null;
  onActiveChange: (active: boolean) => void;
  /**
   * A scan was just started from in here, so the shared poll should look
   * again NOW. Owning the poll is the parent's job (one poller, several
   * consumers), which also means only the parent can shorten its beat: while
   * idle it is on a ten-second timer, and a run that this box asked for should
   * not have to wait that out to be admitted to exist.
   */
  onScanRequested: () => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [ai, setAi] = useState<AiPhase>(AI_OFF);
  const [highlight, setHighlight] = useState<number | null>(null);
  const q = query.trim();
  const active = q !== "";
  // Below MIN_QUERY_CHARS the REQUEST is gated, not `active`: `active` is what
  // hides bookmarks/recents and hands the page body to this panel, and doing
  // that on the first character would bounce the whole page as the user types
  // their second one. `searchable` instead governs whether a rank request goes
  // out and whether the AI row can ever be armed for the current query.
  const searchable = q.length >= MIN_QUERY_CHARS;
  useEffect(() => onActiveChange(active), [active, onActiveChange]);

  // -- a query that is really an address --------------------------------------
  //
  // `pathShortcut` (lib/home-search) detects the SHAPE — `/…`, `~/…`, `C:\…` —
  // on every render, not just at submit: consulting it only on Enter left the
  // whole screen while typing lying about what Enter would do. A pasted
  // `/tmp/report.csv` used to rank nothing, render "No file name matched…",
  // and pre-arm the AI row — a paid model call offered on a filesystem path —
  // while Enter would in fact have navigated straight there.
  //
  // `address` is pure and cheap (a regex), computed fresh every render. What
  // it resolves TO takes a stat, so that part is debounced/abortable exactly
  // like the rank request below (leading-edge `searchDelay`, one
  // AbortController) — a query that merely LOOKS like a path is typed one
  // character at a time same as any other.
  const address = pathShortcut(q, home);
  const [addr, setAddr] = useState<
    | { status: "unknown" }
    | { status: "exists"; path: string; is_dir: boolean }
    | { status: "missing" }
  >({ status: "unknown" });
  const addrCtl = useRef<AbortController | null>(null);
  const addrIssuedAt = useRef(0);
  useEffect(() => {
    addrCtl.current?.abort();
    if (address === null) {
      setAddr({ status: "unknown" });
      return;
    }
    setAddr({ status: "unknown" });
    const run = () => {
      addrCtl.current?.abort();
      const ctl = new AbortController();
      addrCtl.current = ctl;
      addrIssuedAt.current = Date.now();
      statPath(address, ctl.signal).then(
        (st) => {
          if (ctl.signal.aborted) return;
          setAddr({ status: "exists", path: st.path, is_dir: st.is_dir });
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          // Not found, or not statable (permissions, a dead mount): either
          // way it is not a navigable address, so it falls back to a search
          // (7d) — a half-typed path is a legitimate query prefix.
          setAddr({ status: "missing" });
        },
      );
    };
    const delay = searchDelay(Date.now(), addrIssuedAt.current);
    if (delay === 0) {
      run();
      return;
    }
    const timer = window.setTimeout(run, delay);
    return () => window.clearTimeout(timer);
  }, [address]);
  useEffect(() => () => addrCtl.current?.abort(), []);

  // Enter pressed WHILE the stat above is still in flight (addr.status ===
  // "unknown") used to be a silent no-op: `suppressRank` holds the rank
  // request back, `showOpenRow` is false (nothing has resolved yet), and the
  // AI row is suppressed too (`address !== null`) — so `submitRow` has
  // nothing to commit. That drops exactly the paste-and-go gesture the
  // address feature exists for: paste a path, hit Enter immediately, expect
  // it to open the moment the stat lands.
  //
  // `awaitingCommit` remembers the SPECIFIC address Enter was pressed for.
  // The effect below fires once `addr` settles (either resolution) and
  // commits — navigating only on "exists", never on "missing" — but ONLY if
  // `address` still equals the address the commit was requested for: a newer
  // keystroke changes `address` before the stat answers (and aborts the old
  // stat's controller, so a still-in-flight reply for the OLD address is
  // also ignored on its own terms), and the mismatch here is what stops a
  // late-arriving answer for an address the user has since typed past from
  // committing anyway.
  const awaitingCommit = useRef<string | null>(null);
  useEffect(() => {
    if (awaitingCommit.current === null || addr.status === "unknown") return;
    const target = awaitingCommit.current;
    awaitingCommit.current = null;
    if (target !== address) return; // superseded by a newer keystroke
    if (addr.status === "exists") navigate(addr.path, { isDir: addr.is_dir });
    // "missing" resolves to nothing: Enter must not navigate to a path that
    // does not exist.
  }, [addr, address]);

  // There is no "Open" row until the stat is back and says the address is
  // real.
  const showOpenRow = address !== null && addr.status === "exists";
  // The rank request itself is suppressed more broadly than the open row: a
  // query shaped like a path is held back from ranking THE MOMENT it looks
  // like one, not only once it is confirmed to exist — firing a rank request
  // for something that is very likely about to resolve is a wasted round
  // trip, and a half-typed path is not yet a case anyone can rank sensibly.
  // Only once the stat comes back "missing" does 7d's fallback apply: this IS
  // a legitimate search query after all.
  const suppressRank = address !== null && addr.status !== "missing";

  // -- the ranked answer -----------------------------------------------------
  //
  // ONE REQUEST PER QUERY. This page used to fetch the whole home corpus (19.8
  // MB, 164k rows, silently capped so most of a large home was unfindable) and
  // rank it in the browser; the server filters and ranks now (/api/index/rank)
  // and answers a few KB from the WHOLE index.
  //
  // A round trip per keystroke can only be an improvement if it never feels
  // like one, and every piece below is that and nothing else: fire on the
  // leading edge, coalesce a burst, abort rather than queue, keep the previous
  // rows on screen while the next answer is in flight, and answer a backspace
  // from memory.
  const [answer, setAnswer] = useState<HomeAnswer | null>(null);
  const [failure, setFailure] = useState("");
  const [pending, setPending] = useState(false);
  // Pending for long enough that saying so is information rather than a
  // flicker. The common answer lands well inside PENDING_INDICATOR_MS.
  const [slow, setSlow] = useState(false);
  const memo = useRef(new QueryMemo<HomeAnswer>());
  const inflight = useRef<AbortController | null>(null);
  const issuedAt = useRef(0);
  // The last answer the result note settled on — see `noteAnswer` and where
  // it is read, below.
  const heldAnswerRef = useRef<HomeAnswer | null>(null);
  // Bumped by a real gesture (typing) to re-run a failed request. Without it a
  // failure was terminal: none of the other deps is something a user can move,
  // so search stayed dead until a reload.
  const [retryNonce, setRetryNonce] = useState(0);

  // A scan finishing or the index being deleted changes what a query ANSWERS
  // to, and no other signal reports it — the filesystem did not change (see
  // lib/index-freshness). An in-app rename or delete moves paths the index
  // still spells the old way, so a remembered hit would 404 on click.
  //
  // Both invalidate every remembered answer at once, so the memo is dropped
  // wholesale rather than reasoned about per entry, and the current query is
  // re-asked. Neither DISABLES search: this page has no live walk to fall back
  // on, so switching search off is the worst available outcome — an answer one
  // rename stale beats no answer at all. (useWalkSearch keeps its gate; it has
  // a walk that can answer the renamed folder correctly.)
  const [lifecycle, setLifecycle] = useState(indexLifecycleCount);
  useEffect(() => subscribeIndexLifecycle(() => setLifecycle(indexLifecycleCount())), []);
  const [mutations, setMutations] = useState(fsMutationCount);
  useEffect(() => subscribeFsMutations(() => setMutations(fsMutationCount())), []);
  useEffect(() => {
    memo.current.clear();
  }, [lifecycle, mutations, home]);

  // Warm at idle, once per mount. The first search of a fresh server process
  // pays for the duckdb import and the gitignore verdict pool, and the whole
  // point of this page is that the cost lands before the user types rather
  // than on their first keystroke. It is a few KB now, not 20 MB.
  useEffect(() => {
    let cancelled = false;
    const idle = window.requestIdleCallback ?? ((cb: () => void) => window.setTimeout(cb, 300));
    idle(() => {
      // Errors are not the user's problem: nothing is on screen, and a real
      // search will report a real failure.
      if (!cancelled) void indexRank(home, WARM_QUERY, { limit: 1 }).catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [home]);

  useEffect(() => () => inflight.current?.abort(), []);

  useEffect(() => {
    // `!searchable` is the same early-out as `!active`: nothing is asked, and
    // anything already in flight (from a longer query since backspaced away)
    // is abandoned rather than left to land over a query too short to have
    // earned an answer. `suppressRank` is a THIRD early-out (7c/7d): a query
    // shaped like a path is held back from ranking the moment it looks like
    // one — the noise that used to produce "No file name matched" over a
    // path that Enter would have navigated to — until the stat says it is
    // NOT one, at which point it is a legitimate search query again.
    if (!active || !searchable || suppressRank) {
      inflight.current?.abort();
      setPending(false);
      return;
    }
    const remembered = memo.current.get(q);
    if (remembered) {
      // Backspacing (or retyping) must not cost a round trip for rows the page
      // already had — and must not leave a superseded request able to land.
      inflight.current?.abort();
      setAnswer(remembered);
      setFailure("");
      setPending(false);
      return;
    }
    const run = () => {
      // Abort, never queue: the answer to a query the user has already edited
      // is worth nothing, and letting it land would repaint the list backwards.
      inflight.current?.abort();
      const ctl = new AbortController();
      inflight.current = ctl;
      issuedAt.current = Date.now();
      setPending(true);
      // The previous failure is not this request's verdict. Left standing it
      // kept the banner up over rows that were about to be replaced, and — via
      // rankingSettled — armed the AI row on every keystroke after one
      // transient error.
      setFailure("");
      indexRank(home, q, { signal: ctl.signal, limit: RANK_FETCH_LIMIT }).then(
        (res) => {
          if (ctl.signal.aborted) return;
          const next = answerFrom(res, q, home, Date.now() - issuedAt.current);
          memo.current.put(q, next);
          setAnswer(next);
          setFailure("");
          setPending(false);
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          // The rows in hand STAY. They are the best answer available on a page
          // with no live walk, and the banner below says the refresh failed.
          setFailure(err.message);
          setPending(false);
        },
      );
    };
    // Zero on the leading edge — the first keystroke after a pause must not sit
    // behind a timer (lib/instant-search, `searchDelay`).
    const delay = searchDelay(Date.now(), issuedAt.current);
    if (delay === 0) {
      run();
      return;
    }
    const timer = window.setTimeout(run, delay);
    return () => window.clearTimeout(timer);
  }, [home, q, active, searchable, suppressRank, lifecycle, mutations, retryNonce]);

  useEffect(() => {
    if (!pending) {
      setSlow(false);
      return;
    }
    const timer = window.setTimeout(() => setSlow(true), PENDING_INDICATOR_MS);
    return () => window.clearTimeout(timer);
  }, [pending]);

  // The rows on screen, whatever query they answer. Never blanked while the
  // next answer is in flight: results → nothing → results is the single most
  // visible way this can feel worse than ranking locally, which never had an
  // empty frame. `behind` is what dims them and what the caveat chip explains.
  //
  // While behind, render `narrowAnswer`'s re-filtered subset rather than the
  // held answer's raw hits: the overwhelmingly common case is the query
  // EXTENDING the held one ("read" -> "readme"), and re-running fuzzyMatch
  // over hits already in hand narrows the list with no round trip and no
  // blank frame — strictly better than dimming rows that cannot possibly
  // match. It can only ever remove rows, never add or reorder them, so this
  // is always a subset of the true (still in-flight) answer.
  const behind = answer !== null && answer.query !== q;
  const hits = behind && answer ? narrowAnswer(answer, q) : (answer?.hits ?? []);

  // The staleness deadline: past STALE_CLEAR_MS of a request outliving the
  // query it was asked for, "a little behind" stops being true — this used to
  // hold for a ~40ms local rank, not a multi-second round trip. Narrowing
  // (above) is tried FIRST and takes priority: if it leaves real rows, those
  // are a provable subset of the answer to THIS query, and the deadline must
  // not throw them away out from under the user. Only when narrowing leaves
  // nothing (an unrelated query, a paste, a select-all retype) does the
  // deadline drop to `answer = null`, which renders as the plain "Searching…"
  // note — no rows and an honest label beats rows for a query the user has
  // visibly moved past.
  //
  // `pending` was the only qualifying condition before `suppressRank`
  // existed — there was always a real request "outliving" the query to time
  // out on. An address-shaped query holds ranking back entirely (`suppressRank`
  // is a THIRD reason `behind` can be true, alongside `pending` and, briefly,
  // neither): `pending` is never true on that path (the rank effect
  // early-returns before setting it), so a guard reading only `pending` never
  // fires here at all — the held answer, and its `is-stale` dimming, stayed
  // behind indefinitely regardless of how long the stat took. `suppressRank`
  // is included in the gate for exactly the same reason `pending` is: it is
  // the other case where nothing is going to replace `answer` for THIS query
  // on its own — this one because the stat, not the ranking round trip, is
  // what has to resolve first (and if it resolves to "missing", 7d's fallback
  // fires a real rank request, which unsticks this the normal way, but until
  // then the deadline is what stops a slow stat from pinning a stale answer
  // in place).
  useEffect(() => {
    if (!behind || (!pending && !suppressRank)) return;
    const timer = window.setTimeout(() => {
      setAnswer((prev) => {
        if (prev === null || prev.query === q) return prev;
        return narrowAnswer(prev, q).length > 0 ? prev : null;
      });
    }, STALE_CLEAR_MS);
    return () => window.clearTimeout(timer);
  }, [pending, suppressRank, behind, q]);

  // -- the box is where typing goes ------------------------------------------
  //
  // This page exists to be typed into, so the caret starts here and STAYS
  // reachable: a stray click on the background, or arriving with the hands
  // already moving, must not cost a click on the input to recover. Any printable
  // keystroke aimed at nothing else focuses the box and lands in it.
  const inputEl = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputEl.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const claim = redirectsToSearch({
        key: e.key,
        ctrlKey: e.ctrlKey,
        altKey: e.altKey,
        metaKey: e.metaKey,
        tagName: el?.tagName,
        isContentEditable: el?.isContentEditable === true,
        isSearchInput: el === inputEl.current,
      });
      // Focus, then let the event through UNHANDLED: the browser delivers this
      // same keystroke to the newly focused input, so the character is neither
      // dropped nor inserted twice (preventDefault + appending by hand would
      // fight the controlled value and lose the caret).
      if (claim) inputEl.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // -- AI search -------------------------------------------------------------
  const aiCtl = useRef<AbortController | null>(null);
  useEffect(() => () => aiCtl.current?.abort(), []);
  const syncQueryParam = (value: string | null) => {
    const params = new URLSearchParams(location.search);
    if (value) params.set("q", value);
    else params.delete("q");
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };
  const runAi = (target: string) => {
    if (!target) return;
    // Re-entry guard, the keyboard's half of the `disabled` that already
    // protects the mouse path: Enter on the AI row while a search is in flight
    // aborted and re-issued it, so three impatient presses were three billed
    // model calls. Editing the query is the way to change what is running (it
    // resets the phase), so a running search is never for a stale query.
    if (ai.status === "running") return;
    aiCtl.current?.abort();
    const ctl = new AbortController();
    aiCtl.current = ctl;
    setAi({ status: "running", query: target });
    runAiSearch(home, target, ctl.signal).then(
      (result) => {
        if (ctl.signal.aborted) return;
        setAi({ status: "done", query: target, result });
        // Only a committed AI search rides the URL, so a reload reproduces the
        // search the user paid for — instant results need no restoring.
        syncQueryParam(target);
      },
      (err: Error) => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        // No substituted keyword search: the index results underneath are
        // still on screen, and they are the honest fallback (lib/ai-search).
        setAi({ status: "failed", query: target, message: err.message });
      },
    );
  };

  const clear = () => {
    aiCtl.current?.abort();
    setQuery("");
    setAi(AI_OFF);
    setHighlight(null);
    syncQueryParam(null);
  };

  const edit = (value: string) => {
    setQuery(value);
    setHighlight(null);
    // Typing is a user gesture, so it is also the retry for a failed request —
    // the same way useWalkSearch re-arms its stream from setQuery. (Editing to
    // a query that was never asked re-runs anyway; this covers editing BACK to
    // the one that failed.)
    if (failure !== "") setRetryNonce((n) => n + 1);
    // Editing the query is how the user gets back from AI results to instant
    // ones, so it drops the AI phase — and the ?q= that a committed search set.
    if (ai.status !== "off") {
      aiCtl.current?.abort();
      setAi(AI_OFF);
      syncQueryParam(null);
    }
  };

  // A ?q= restored from the URL was a committed AI search, so it re-runs one.
  const ranInitial = useRef(false);
  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (initialQuery.trim()) runAi(initialQuery.trim());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showingAi = ai.status === "done" && ai.query === q;
  // Whether the instant list is a finished answer FOR THIS QUERY. Gates the AI
  // row's pre-selection, so Enter while the request is in flight cannot spend a
  // model call on a query that was about to answer itself. `!searchable` short-
  // circuits it to false outright: a query under MIN_QUERY_CHARS never asked
  // anything, so there is nothing for it to be settled ON, and a model call on
  // "a" is never the intent.
  const settled = searchable && rankingSettled(answer, q, pending, failure !== "");
  // The result note's own source of truth — see `noteAnswer`. Mutated during
  // render (not in an effect) so the note that renders THIS frame, the one
  // settled turns true on, already reads from it: an effect would apply one
  // render late, which is exactly the kind of one-beat lag that reads as
  // flicker.
  const displayAnswer = noteAnswer(answer, settled, heldAnswerRef.current);
  // A settled `answer` of null (a later query's request failed while an
  // earlier, unrelated held answer got cleared by the stale-clear effect —
  // see `noteAnswer`, home-search.ts) is never something to hold: writing it
  // would clobber a real held value for every render after this one, not
  // just this one — the guard above only protects THIS frame's note.
  if (settled && answer !== null) heldAnswerRef.current = answer;
  // The indexing caveat, same helper and same two messages as the listing's
  // search chip (listing/index-caveat) so the two boxes make the same claim in
  // the same words. It is the piece that makes a lagging answer read as
  // intentional: with the previous query's rows still on screen and nothing
  // saying why, the box just looks wrong. The rows themselves dim while
  // `behind`, the same treatment the listing gives held results.
  // `behind` is the rows answering a DIFFERENT query, which is two different
  // situations: one where the next answer is 40ms away, and one where it is
  // never coming. Only the second is staleness, and only the second gets the
  // caveat's words for it ("clear the search and run it again") — saying that
  // on every keystroke also puts a word on screen exactly where the 200ms rule
  // deliberately withholds a spinner. `rescanPending` is the listing's third
  // message arriving here for the same reason it exists there: this app just
  // changed a file, the server is re-indexing that folder, and until a status
  // poll catches the run nothing else would say so.
  const caveat =
    active && !showingAi && searchable && !showOpenRow
      ? searchCaveat(indexScan, { behind, pending, rescanPending: indexRescanPending() })
      : null;

  // Why an uncovered answer is uncovered, and — for the one value of that the
  // user can act on — the action itself (lib/home-search's `indexGap`).
  //
  // The poll's scan state is fed in alongside the answer's own `reason`, as a
  // TRI-STATE: `null` while the poll has not answered, so that a definite
  // "nothing is running" can contradict a `reason` frozen at rank time in
  // either direction (see `indexGap`). `indexScan?.scanning === true` would
  // collapse "no answer yet" into "idle" and lose half of that.
  const liveScanning = indexScan === null ? null : indexScan.scanning;
  const gap =
    displayAnswer !== null && !displayAnswer.covered
      ? indexGap(displayAnswer.reason, liveScanning)
      : null;
  // Whether AI search has anything to answer WITH. It executes its spec
  // against the same file index (`routers/search._search_index`), so with no
  // index built it fails the same way the instant search did — see
  // `aiSearchUsable`'s doc comment.
  const aiUsable = aiSearchUsable(indexScan);
  const [pendingBuild, setPendingBuild] = useState<PendingScan | null>(null);
  // Scoped to the query it was raised on. The note is per-query real estate,
  // and this message REPLACES the "your files aren't indexed yet" diagnosis:
  // held across keystrokes, one failed click (a 409 from the pref being
  // flipped in another window, say) would prefix every later uncovered query
  // with a stale complaint and hide the actual state. Typing is the retry
  // gesture here, the same as `retryNonce`.
  const [buildError, setBuildError] = useState<{ query: string; message: string } | null>(
    null,
  );
  const buildFailure =
    buildError !== null && buildError.query === q ? buildError.message : "";
  const starting = scanStarting(
    pendingBuild,
    liveScanning,
    indexScan?.last_completed_at ?? null,
    Date.now(),
  );
  // POST /api/index/scan with no root — every configured root, and NOT
  // `requestFolderScan`, whose 15-minute debounce is right for a keystroke and
  // wrong for a button: the whole reason the user is looking at this button is
  // that a scan started minutes ago and left nothing behind, which is exactly
  // what that debounce would refuse.
  //
  // `pendingBuild` covers the request AND the wait for the poll to see the run
  // it started (`scanStarting`); after that the poll owns the state, because
  // the poll is what `gap` reads and what survives this component remounting
  // mid-scan. `onScanRequested` re-fires that poll immediately rather than
  // leaving the run unseen until the next idle beat — the latch is the honest
  // bridge across the round trip, not a substitute for asking.
  const startBuild = () => {
    if (starting) return;
    setPendingBuild({ at: Date.now(), completedAt: indexScan?.last_completed_at ?? null });
    setBuildError(null);
    startIndexScan().then(
      () => onScanRequested(),
      (err: Error) => {
        setPendingBuild(null);
        setBuildError({ query: q, message: err.message });
      },
    );
  };

  // The row-model descriptor (lib/home-search): at most one leading "Open"
  // row, then files, then at most one AI row. `showOpenRow` forces the other
  // two off — the request that would have produced file hits was never sent
  // (7c), and a paid model call is never the intent for something shaped like
  // a path (7e, independent of whether it actually resolved). `aiUsable`
  // gates it too: offering a paid model call that will fail on the same
  // missing index is a dead end, not an offer.
  const rowModel: RowModel = {
    openRow: showOpenRow,
    fileCount: showOpenRow ? 0 : hits.length,
    aiRow: searchable && !showOpenRow && address === null && aiUsable,
  };
  const current = activeRow(highlight, rowModel, settled);

  const activateRow = (row: number) => {
    if (isOpenRow(row, rowModel)) {
      if (addr.status === "exists") navigate(addr.path, { isDir: addr.is_dir });
      return;
    }
    if (isAiRow(row, rowModel)) {
      runAi(q);
      return;
    }
    const fi = row - (rowModel.openRow ? 1 : 0);
    navigate(hits[fi].path, { isDir: hits[fi].is_dir });
  };

  // Enter with no row chosen: a resolved address (the open row) or the top
  // hit commits, or — only once ranking has settled on nothing — the AI row.
  // The address itself is no longer resolved HERE: `addr` is kept live by the
  // effect above as the query is typed, so by the time Enter is pressed the
  // row model already reflects it — `activeRow`/`submitRow` do the rest.
  const submit = () => {
    if (address !== null && addr.status === "unknown") {
      // The paste-and-go gesture: Enter fired before the stat came back.
      // Await it instead of dropping the keystroke — the effect above
      // commits once `addr` settles.
      awaitingCommit.current = address;
      return;
    }
    const row = submitRow(highlight, rowModel, settled);
    if (row !== null) activateRow(row);
  };

  return (
    <div className="files-search-wrap">
      <div className="files-search">
        <span className="files-search-icon" aria-hidden="true">
          <MagnifierIcon />
        </span>
        <input
          ref={inputEl}
          type="search"
          className="files-search-input"
          placeholder="Search your files — or paste a path like ~/Downloads"
          aria-label="Search your files"
          role="combobox"
          aria-expanded={active && !showingAi && (searchable || showOpenRow)}
          aria-controls="fh-result-list"
          aria-activedescendant={
            active && !showingAi && (searchable || showOpenRow) && current !== null
              ? "fh-row-" + current
              : undefined
          }
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => edit(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              if (!active || showingAi || (!searchable && !showOpenRow)) return;
              e.preventDefault();
              // Step from `current` (the RESOLVED row, `activeRow` above), not
              // the raw `highlight` state: the top file row can be active with
              // `highlight` still null (activeRow's implicit pre-select), and
              // stepping from null would land the first ArrowDown back on row
              // 0 — the already-visible selection — instead of row 1.
              setHighlight(stepHighlight(current, rowModel, e.key === "ArrowDown" ? 1 : -1));
            } else if (e.key === "Enter") {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape" && active) {
              e.preventDefault();
              clear();
            }
          }}
        />
        {active && (
          <button type="button" className="files-search-clear" title="Clear search (esc)" onClick={clear}>
            Clear
          </button>
        )}
      </div>

      {ai.status === "failed" && <ErrorBanner>{ai.message}</ErrorBanner>}

      {/* A refetch failed while rows are still in hand. The rows stay — they
          are the best answer available on a page with no live walk — but the
          failure is said out loud, because typing is the retry gesture
          (`retryNonce`) and a retry that reports nothing leaves the user
          pressing keys into silence. The no-rows case is not this banner: it
          is the whole content of the result note below. */}
      {active && !showingAi && answer !== null && failure !== "" && (
        <ErrorBanner>
          Couldn't search the file index ({failure}) — showing the last results.
        </ErrorBanner>
      )}

      {!active ? null : showingAi ? (
        <AiResults home={home} query={ai.query} result={ai.result} />
      ) : (
        <div className="fh-panel">
          {/* The one action this screen asks of the user, promoted out of the
              muted footnote and into a real CTA — a link-button buried mid-
              sentence at 12px was invisible in testing. The note chain below
              still has a `gap === "buildable"` case, but it renders nothing:
              this block is the whole message for that state. */}
          {gap === "buildable" && (
            <div className="fh-index-cta">
              <span className="fh-index-cta-text">
                {buildFailure !== ""
                  ? `The scan could not be started: ${buildFailure}`
                  : "Your files aren’t indexed yet — one scan is all it takes."}
              </span>
              <button
                type="button"
                className="btn btn-primary fh-index-cta-btn"
                disabled={starting}
                onClick={startBuild}
              >
                {starting ? "Starting the scan…" : "Index my files"}
              </button>
            </div>
          )}
          <p className="fh-result-note">
            {/* Branch on the ANSWER IN HAND, not on the request state: while a
                new query is in flight the previous answer is what is on screen,
                and a note that denies having rows over rows that are visibly
                there is the mid-rescan "still building" bug in another costume.
                Only having never had an answer may say so.

                `!searchable` comes FIRST, ahead of every other branch: under
                MIN_QUERY_CHARS no request went out, so `answer` (if any) is
                whatever an earlier, longer query left behind, and reporting
                on it here would describe a search that didn't happen. */}
            {/* `showOpenRow` comes ahead of everything below it too: once an
                address resolves, the row IS the content, and reporting on
                `answer` (whatever a previous, non-address query left behind,
                or nothing at all) would describe a search that was never
                sent (7c skips it entirely). `suppressRank`, right below it,
                is the SAME protection one beat earlier in the address's
                lifecycle: the stat hasn't resolved yet (addr.status is
                "unknown", neither "exists" nor "missing"), so no rank
                request went out for this query either — `answer` is still
                whatever the PREVIOUS, non-address query left behind. This
                used to fall all the way through to the count-note branch
                below, reporting that stale answer's total over rows that
                (see `hits`, above) have likely narrowed to nothing. */}
            {showOpenRow ? (
              <>
                <kbd>↵</kbd> to open · <kbd>esc</kbd> to clear
              </>
            ) : !searchable ? (
              "Keep typing…"
            ) : suppressRank ? (
              "Checking…"
            ) : displayAnswer === null && failure !== "" ? (
              `The file index could not be searched: ${failure}`
            ) : displayAnswer === null ? (
              // Never settled once for this typing run yet — nothing held to
              // fall back on, so there is nothing else honest to say.
              "Searching…"
            ) : gap === "disabled" ? (
              // Distinct from "still building": nothing is coming, because
              // nothing is scanning, because the user turned it off. Saying
              // "still building" here would be a lie the user has no way to
              // resolve by waiting.
              <>
                File indexing is off —{" "}
                <button
                  type="button"
                  className="fh-link-button"
                  onClick={() => navigateUrl("/preferences?tab=indexing")}
                >
                  enable it in Preferences
                </button>
                .
              </>
            ) : gap === "scanning" ? (
              // Never "no matches" for an index that has not been built: that
              // would blame the user's files for the app's state. The file
              // count is the listing's chip in words — a claim that something
              // is coming should be able to show its progress, or it is
              // indistinguishable from the wedged case below.
              `The file index is still building${
                indexScan && indexScan.files > 0
                  ? ` (${indexScan.files.toLocaleString()} files so far)`
                  : ""
              }${aiUsable ? " — AI search can answer in the meantime." : ""}`
            ) : gap === "buildable" ? (
              // Owned by the `.fh-index-cta` block above, not this note — a
              // paragraph AND a callout saying the same thing would say it
              // twice. Kept as its own branch so the chain still accounts
              // for every `IndexGap` value.
              null
            ) : gap === "unavailable" ? (
              // mount / package / ignored: no scan will ever cover this root,
              // so offering one would be a button that cannot work. There is
              // no live walk on this page to fall back to either (that is the
              // listing's), which leaves AI search as the honest offer —
              // when the index backing it actually exists.
              aiUsable
                ? "This location can’t be indexed — AI search can still answer."
                : "This location can’t be indexed."
            ) : hits.length === 0 && settled && rowModel.aiRow ? (
              // `hits`, not `answer.hits`: `settled` already rules out
              // `behind` (see `hits`'s own comment — `rankingSettled` is
              // false whenever `answer.query !== q`), so the two agree here,
              // but reading the rendered array rather than the held answer's
              // is what keeps every branch below honest about what's on
              // screen instead of what a previous request found.
              `No file name matched “${q}” — AI search can look at dates, types and sizes.`
            ) : hits.length === 0 && settled ? (
              // Settled, zero hits, but the AI row is suppressed (address !==
              // null: this query is shaped like a path that did not resolve —
              // 7e). Offering AI search here would point at a row that is not
              // being rendered.
              `No file name matched “${q}”.`
            ) : displayAnswer === null ? (
              // Never settled once for this typing run yet — nothing to hold.
              "Searching…"
            ) : (
              <>
                {/* `displayAnswer` (`noteAnswer`, home-search.ts) is the LAST
                    SETTLED answer, not necessarily this one: while `behind`,
                    it is deliberately one query stale rather than recomputed
                    from `hits`/`narrowAnswer` on every keystroke — that used
                    to rewrite this line 2-3 times per keystroke (a lower
                    bound that shrinks toward zero, then a swap back to the
                    real total). Staleness is still honestly communicated —
                    the rows dim while `behind`, and the `slow`-gated
                    "· Searching…" below covers the in-flight case — so this
                    text is free to just hold still until a new answer
                    settles. */}
                {homeCountNote(displayAnswer.total, behind || displayAnswer.truncated)}
                {" · "}
                {formatElapsed(displayAnswer.elapsedMs)}
                {" · "}
                <kbd>↑</kbd>
                <kbd>↓</kbd> to pick · <kbd>esc</kbd> to clear
              </>
            )}
            {/* Only once the wait is long enough to be worth mentioning: under
                PENDING_INDICATOR_MS the answer beats the words onto the screen,
                and a note that appears and vanishes reads as slower than one
                that never appeared. Gated on `displayAnswer`, not `hits`: the
                held count note renders even when narrowing has emptied `hits`
                (see `displayAnswer`, above), and this suffix is what tells the
                user a fresh answer for `q` is still on the way. */}
            {searchable && !showOpenRow && slow && displayAnswer !== null && (
              <span className="fh-searching-note"> · Searching…</span>
            )}
            {caveat && (
              <span className="fh-index-chip" title={caveat.title}>
                {/* Only while a scan is actually running. The chip also carries
                    the "not refreshed" caveat, whose entire point is that NO
                    work is in flight — a spinner there asserts the opposite of
                    what the words next to it say. (Listing.tsx keeps its
                    spinner on walk status for the same reason: the spinner
                    tracks work, the caveat tracks trust.) */}
                {indexScan?.scanning && <span className="fh-index-spinner" aria-hidden="true" />}
                {caveat.note}
              </span>
            )}
          </p>
          {/* Nothing to show below MIN_QUERY_CHARS UNLESS an address resolved
              (an "Open" row needs no search — it isn't ranked, it's stat'd —
              so it is not gated on the same threshold): otherwise no request
              went out, `hits` is stale leftover from a longer query, and
              offering the AI row here would arm a model call on "a". The note
              above already says why the list is empty. */}
          {(searchable || showOpenRow) && (
            <ul
              className={"fh-results" + (behind ? " is-stale" : "")}
              id="fh-result-list"
              role="listbox"
              aria-label="Search results"
            >
              {showOpenRow && addr.status === "exists" && (
                <OpenRow
                  path={addr.path}
                  isDir={addr.is_dir}
                  active={current === 0}
                  id="fh-row-0"
                  onOpen={() => navigate(addr.path, { isDir: addr.is_dir })}
                />
              )}
              {!showOpenRow &&
                hits.map((hit, i) => (
                  <FileRow
                    key={hit.path}
                    hit={hit}
                    home={home}
                    active={current === i}
                    id={"fh-row-" + i}
                    onHover={() => setHighlight(i)}
                  />
                ))}
              {rowModel.aiRow && (
                <AiActionRow
                  query={q}
                  active={current === hits.length}
                  running={ai.status === "running"}
                  id={"fh-row-" + hits.length}
                  onRun={() => runAi(q)}
                />
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// The fold's "Show more" — a quiet pill under the grid, shared by both tabs.
function ShowMoreButton({ expanded, onClick }: { expanded: boolean; onClick: () => void }) {
  return (
    <button type="button" className="fhb-more" onClick={onClick}>
      {expanded ? "Show less" : "Show more"}
      <svg
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        style={expanded ? { transform: "rotate(180deg)" } : undefined}
      >
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>
  );
}

export default function FilesHome({ config }: { config: Config }) {
  // Same normalization every other config.home consumer applies (App.tsx,
  // Panel.tsx): backslashed on Windows, while search hits and path helpers
  // (basename, the "home + /" strip below) are forward-slash-only.
  const home = config.home.replace(/\\/g, "/");
  useRecentsVersion();
  // The active tab lives entirely in the URL (?tab=recents) — read fresh on
  // every render, re-triggered by any history write (typed url, back/forward,
  // or this file's own tab clicks below), so it's a true two-way binding
  // rather than state that's merely seeded from the url once at mount.
  useUrlVersion();
  const tabParam = new URLSearchParams(location.search).get("tab");
  const setTab = (next: LaunchTab) => {
    const params = new URLSearchParams(location.search);
    // Always written explicitly (even "bookmarks"): the no-param default is
    // content-dependent below, so a clicked tab must pin itself in the URL.
    params.set("tab", next);
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };
  // Each tab folds at a flat MAX_CARDS and keeps its own "Show more" state,
  // so switching tabs doesn't reset (or leak) the other tab's expansion.
  const [expandedRecents, setExpandedRecents] = useState(false);
  const [expandedSessions, setExpandedSessions] = useState(false);
  const [expandedRepos, setExpandedRepos] = useState(false);
  // Raw MRU (newest first) — not the sidebar's stable-slot top-3, since a
  // full page doesn't jump under the pointer the way an always-visible
  // sidebar section does.
  const recents = loadRecents().entries;
  const shownRecents = expandedRecents ? recents : recents.slice(0, MAX_CARDS);
  // Claude session folders have no client-side cache like bookmarks/recents —
  // one cheap GET on mount, independent of which tab is showing, so switching
  // to the tab never shows a fetch-in-flight blip.
  const [sessionFolders, setSessionFolders] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    let alive = true;
    getClaudeSessionFolders().then(
      (r) => alive && setSessionFolders(r.folders),
      () => alive && setSessionFolders([]),
    );
    return () => {
      alive = false;
    };
  }, []);
  const shownSessions =
    sessionFolders && (expandedSessions ? sessionFolders : sessionFolders.slice(0, MAX_CARDS));
  // Git repos, same deal as the session folders: one cheap GET on mount whatever
  // tab is showing. The whole response is kept, not just the list — the tab has
  // to tell "no repos on this machine" apart from "the first index scan hasn't
  // finished", and only `indexed`/`scanning` carry that. A failed request lands
  // on a null response, which the tab renders as its own message rather than as
  // an empty repo list.
  const [repos, setRepos] = useState<GitRepos | null>(null);
  const [reposFailed, setReposFailed] = useState(false);
  // Search takes over the page body (bookmarks/recents hide behind it) for as
  // long as there is a query — instant results appear while typing, so the
  // grids yield from the first keystroke rather than on a submit. Declared
  // here because the index poll below is shared with the search box.
  const [searching, setSearching] = useState(false);
  // ...but "one GET on mount" alone would strand the user, because the answer can
  // arrive LATER: this list is derived from the file index, and a homepage opened
  // while the first scan is running would sit on "Still building…" until something
  // remounted the page — which switching tabs does not do (the tab is a URL param,
  // not a route). That is not an edge case; it is every user's state right after an
  // upgrade that changes the index rules and forces a rescan.
  //
  // So the existing index poller drives the refetch (useIndexStatus, shared with
  // the listing's search indicator) rather than a poll of our own: it already
  // knows both scan rates.
  //
  // It runs until the answer is FINAL, not merely present (reposNeedsIndexPoll).
  // Gating on `!indexed` stopped the poll the instant a stale-but-served list
  // arrived — which froze `indexKey` below, so the cards and their "Reindexing"
  // note stayed on screen forever, even after the scan that would have cleared
  // them finished.
  //
  // `searching` widens the same poll rather than adding a second one: the
  // search box needs the scan state for its "indexing…" caveat, and the repos
  // gate goes quiet exactly when the index is healthy — which is when a scan
  // starting mid-session would otherwise go unnoticed by the box. One poller,
  // two consumers, the listing's rule (poll only while a search is open) still
  // honoured for the search half.
  // Bumped when the search box starts a scan: `useIndexStatus` restarts on a
  // new nonce, which turns the idle ten-second beat into an immediate look.
  const [indexNonce, setIndexNonce] = useState(0);
  const indexScan = useIndexStatus(reposNeedsIndexPoll(repos) || searching, indexNonce);
  // Refetch whenever the index's OBSERVABLE STATE changes — not when a scan
  // "completes". Completion was the previous trigger and it was subtly wrong:
  // `last_completed_at` is read off the manifest, and a cancelled, failed or
  // killed run stops without ever writing one (runner.derive_state sets running
  // false on any run_end; _with_liveness does the same for an abandoned worker).
  // So those runs moved `scanning` true -> false with `last_completed_at` frozen,
  // no refetch fired, and the tab sat on "Still building…" with nothing running.
  //
  // Keying on the pair covers every way a scan can end — completed, cancelled,
  // failed, killed — and also the start of one, with no transition bookkeeping to
  // get wrong. Extra refetches are harmless (the request is a cheap read and the
  // view is a pure function of its result), which is the point: this trigger is
  // deliberately over-eager rather than clever.
  const indexKey = indexScan
    ? `${indexScan.scanning}|${indexScan.last_completed_at ?? ""}`
    : null;
  // The key the response we are HOLDING was fetched under. `undefined` means
  // nothing has been fetched yet, which no real key can equal.
  const [fetchedKey, setFetchedKey] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    const key = indexKey;
    getGitRepos().then(
      (r) => {
        if (!alive) return;
        setReposFailed(false);
        setRepos(r);
        setFetchedKey(key);
      },
      () => {
        if (!alive) return;
        setReposFailed(true);
        // Stamped on failure too, or `refreshPending` would stay true forever and
        // the tab would claim something is coming when nothing is.
        setFetchedKey(key);
      },
    );
    return () => {
      alive = false;
    };
  }, [indexKey]);
  // DERIVED, not stored in an effect. The instant `indexKey` changes, this is
  // already true in the very same render — which is the whole point: a
  // `setRefreshPending(true)` in an effect lands one frame late, and that one frame
  // is precisely the scan-end flicker (the tab showing "go rebuild it" between the
  // poll going idle and the new list arriving). Triggered is not arrived, so the
  // view has to be able to see the gap rather than be told about it afterwards.
  //
  // The rule itself (notably: the first poll reading is a BASELINE, not a change)
  // lives in refreshIsPending so it is covered by the state-table walk.
  const refreshPending = refreshIsPending(fetchedKey, indexKey);
  // One total function over all four inputs, so no impossible in-between state can
  // be rendered — see the state table in lib/repos.ts.
  const reposTab = reposView({
    response: repos,
    failed: reposFailed,
    liveScanning: indexScan?.scanning ?? null,
    refreshPending,
  });
  const repoList = reposTab.kind === "ready" ? reposTab.repos : [];
  const shownRepos = expandedRepos ? repoList : repoList.slice(0, MAX_CARDS);
  // With no ?tab= in the URL, land on Claude sessions — the leading tab.
  // The recents cache still hydrates on mount so the tab is ready when
  // clicked. An explicit ?tab= always wins.
  useEffect(() => {
    void hydrateRecents();
  }, []);
  const tab: LaunchTab =
    tabParam === "recents" || tabParam === "sessions" || tabParam === "repos"
      ? tabParam
      : "sessions";
  // A ?q= present at load was a committed AI search; FilesSearch re-runs it.
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;

  return (
    <div className="files-home">
      <div className="files-home-inner">
        {/* No brand row or headline: the search prompt is the whole hero —
            the page title lives in the sidebar's Explorer entry, and a
            "Find and preview your files" restatement above the box only
            pushed the one thing you came to use further down. */}
        <header className="home-hero files-hero">
          <FilesSearch
            home={home}
            initialQuery={initialQuery}
            indexScan={indexScan}
            onActiveChange={setSearching}
            onScanRequested={() => setIndexNonce((n) => n + 1)}
          />
        </header>

        {searching ? null : (
          <>
          <section className="fh-section">
            <div className="fh-tabs">
              <div className="fh-tablist" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "sessions"}
                className={"fh-tab" + (tab === "sessions" ? " active" : "")}
                onClick={() => setTab("sessions")}
              >
                Claude Sessions
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "recents"}
                className={"fh-tab" + (tab === "recents" ? " active" : "")}
                onClick={() => setTab("recents")}
              >
                Recents
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "repos"}
                className={"fh-tab" + (tab === "repos" ? " active" : "")}
                onClick={() => setTab("repos")}
              >
                Repos
              </button>
              </div>
              {/* Browse rides the tab strip's right edge — the one action in a
                  row of filters, so it sits opposite them rather than above
                  them. Like the grids, it yields to search results. */}
              <button
                type="button"
                className="files-hero-cta"
                onClick={() => navigate(home, { isDir: true })}
              >
                Browse files
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </button>
            </div>

            {tab === "recents" ? (
              recents.length ? (
                <>
                  <div className="fhb-grid">
                    {shownRecents.map((r) => {
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
                  {recents.length > MAX_CARDS && (
                    <ShowMoreButton
                      expanded={expandedRecents}
                      onClick={() => setExpandedRecents((v) => !v)}
                    />
                  )}
                </>
              ) : (
                <p className="fh-empty">Nothing opened yet. Files you view will show up here.</p>
              )
            ) : tab === "repos" ? (
              repoList.length ? (
                <>
                  {/* A stale list still shows every card; this is a footnote, not a
                      warning. An index is always a little behind the filesystem,
                      so alarming copy here would cry wolf permanently. */}
                  {reposStaleNote(reposTab) && (
                    <p className="fh-search-summary">{reposStaleNote(reposTab)}</p>
                  )}
                  <div className="fhb-grid">
                    {shownRepos.map((r) => (
                      <FolderPreviewCard key={r.path} path={r.path} />
                    ))}
                  </div>
                  {repoList.length > MAX_CARDS && (
                    <ShowMoreButton
                      expanded={expandedRepos}
                      onClick={() => setExpandedRepos((v) => !v)}
                    />
                  )}
                </>
              ) : (
                // Every no-cards case routes through the same function, so "you
                // have no repos" can never be shown for "we haven't finished
                // looking" (or the reverse) — the distinction the index makes
                // necessary, and the one three earlier versions of this got wrong.
                <p className="fh-empty">{reposMessage(reposTab)}</p>
              )
            ) : shownSessions === null ? (
              <p className="fh-empty">Looking for artifacts…</p>
            ) : sessionFolders && sessionFolders.length ? (
              <>
                <div className="fhb-grid">
                  {shownSessions.map((f) => (
                    <FolderPreviewCard key={f.path} path={f.path} />
                  ))}
                </div>
                {sessionFolders.length > MAX_CARDS && (
                  <ShowMoreButton
                    expanded={expandedSessions}
                    onClick={() => setExpandedSessions((v) => !v)}
                  />
                )}
              </>
            ) : (
              <p className="fh-empty">
                No Claude Code sessions found on this machine.
              </p>
            )}
          </section>
          </>
        )}
      </div>
    </div>
  );
}
