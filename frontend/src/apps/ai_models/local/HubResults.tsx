// The one section a query puts on the Local page: what the Hub has that this
// machine can run.
//
// **It REPLACES the page rather than joining it** (`searchChrome`, the either/or
// in LocalTab). The capability carousels answer "what should I even get", which
// is the question somebody has BEFORE they know what to type; once they have
// typed they have a better one, and two grids on screen would ask the reader
// which of them is answering. So one section, named at the top in the slot the
// sections it replaced use, with the way back beside its own heading.
//
// **A wrapping grid, not a carousel.** A capability's row is one open-ended list
// per capability and reads a few cards deep (`Carousel`); a result SET is one
// flat answer to one question, and hiding half of it behind a horizontal scroll
// would be the page withholding the thing it was asked for.
//
// **One of the five orderings is this section's own work.** The Hub's list
// endpoint cannot rank a search by size — it refuses to expand `usedStorage` on
// a list at all, so a size is one request per repo — so "Size" asks the server
// for the most-downloaded candidates and reorders them here, once, by the figure
// each card is SHOWING and only when every one of those is known (`ResultSort`,
// `hubSizeBytes`, and the effect that does it below).
//
// This was the Discover tab (D426). What moved is the machinery — the search,
// the host disclosure, the gate chrome, the lazy sizes — onto a page that
// already had the cards, the download plumbing and, crucially, the LISTING: the
// join that makes an in-app Hub search worth having is "you already have this
// one", and the page's own walk is the only honest source for it.
import { useEffect, useState } from "react";
import { HubResultCard } from "./RecommendedCard";
import { type DiskCard, resultDisk, type SectionRunner } from "@apps/ai_models/lib/aiModelGroups";
import {
  bySizeAscending,
  needsHubLogin,
  resultsSummary,
  searchChrome,
  sortsOnPage,
  wireSort,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import { hubSizeBytes, knownTotalSize, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import {
  cancelHfLogin,
  getHfAuth,
  searchHubModels,
  startHfLogin,
  type HfAuth,
  type HubModel,
} from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

/** A settled query — what the debounce hands over, and the only thing this
 *  section is keyed on. */
export interface SettledQuery {
  q: string;
  task: string;
  /** The PAGE's sort, which may be one the Hub cannot perform. What goes on the
   *  wire is `wireSort(sort)`; see `ResultSort`. */
  sort: ResultSort;
}

/** How many rows one question is worth. `limit` means "rows you will be shown":
 *  the server over-fetches and truncates after its supported-tag pass (HS-0a). */
const LIMIT = 24;

/** How many size lookups the page will have outstanding at once while sorting by
 *  size. Each is one request to the Hub through our server, so this is the
 *  difference between a considerate burst and a self-inflicted rate limit — the
 *  same courtesy the lazy per-card lookup gets for free from the viewport only
 *  ever holding a few cards. */
const SIZE_LOOKUPS = 4;

/** Every repo's total, measured with at most `SIZE_LOOKUPS` in flight.
 *
 *  `alive` is asked between repos rather than once at the end: a query that
 *  changed mid-pass must stop SENDING, not just stop being listened to — the
 *  whole point of the cap is that this page is polite to a third party.
 */
async function measureSizes(
  ids: readonly string[],
  alive: () => boolean,
): Promise<Map<string, number | null>> {
  const out = new Map<string, number | null>();
  let next = 0;
  const worker = async () => {
    while (alive() && next < ids.length) {
      const id = ids[next++];
      out.set(id, await lookupTotalSize(id));
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(SIZE_LOOKUPS, ids.length) }, () => worker()),
  );
  return out;
}

/** Signing in to Hugging Face, offered where a gate makes it matter.
 *
 *  **The device-code flow, not a token box** (D402): the button starts
 *  `huggingface_hub`'s own login, the user authorizes on huggingface.co, hf
 *  stores what comes back, and no credential passes through this component in
 *  either direction. Preferences → AI has the same flow with the sign-out beside
 *  it; what this adds is that somebody who just hit a gated result does not have
 *  to leave the results to get past it.
 *
 *  It POLLS while a login is pending rather than holding a request open: the
 *  thing being waited for is a person in another tab, which takes as long as it
 *  takes, and hf's device code lives about fifteen minutes.
 */
function HubLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [auth, setAuth] = useState<HfAuth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Nothing on mount. The search reply already said this machine has no token,
  // and a settings-page GET fired by every gated search would be a request for
  // an answer the page is holding.
  const pending = auth?.pending ?? null;
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const id = setInterval(() => {
      // A blip mid-login is not worth a banner: the person is in another tab,
      // and the next tick two seconds later is the retry.
      getHfAuth().then(
        (a) => alive && setAuth(a),
        () => undefined,
      );
    }, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
    // Keyed on WHETHER a login is in flight, not on the object: the poll
    // rewrites `secondsLeft` every tick, and a dependency on the payload would
    // tear the interval down and build it again every two seconds.
  }, [pending !== null]);

  // The moment it lands, the results are asked again — the reply carries
  // `authenticated`, so the gates on the cards below are re-decided from the
  // same payload as the rows rather than from a second flag kept here.
  useEffect(() => {
    if (auth?.signedIn) onSignedIn();
    // `onSignedIn` is a fresh closure each render; keying on it would refetch
    // on every parent render.
  }, [auth?.signedIn]);

  const act = async (fn: () => Promise<HfAuth>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setAuth(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="am-hub-login">
      {auth?.pending ? (
        <>
          {/* The page's own button-shaped link — the same one a gated card
              offers for "Accept terms", because it is the same kind of act:
              one step, on the Hub, in a new tab. */}
          <a
            className="am-card-power am-card-gate-link"
            href={auth.pending.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Authorize on huggingface.co
          </a>
          {/* The code is shown as well as carried by that link: the link has
              it, but the Hub asks for confirmation, and somebody who opened
              the page in a different browser needs to type it. */}
          <span className="cc-caption">
            Waiting for you to authorize. If asked for a code, enter{" "}
            <code>{auth.pending.userCode}</code> — it expires in{" "}
            {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
          </span>
          <button
            type="button"
            className="am-hub-back"
            disabled={busy}
            onClick={() => void act(cancelHfLogin)}
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            className="am-card-power"
            disabled={busy}
            title="Start the Hugging Face device-code login. The token is stored by huggingface_hub, never by this app."
            onClick={() => void act(() => startHfLogin())}
          >
            Log in to Hugging Face
          </button>
          <span className="cc-caption">
            Some results below are gated. Downloading one needs a token on this machine.
          </span>
        </>
      )}
      {(error || auth?.error) && <ErrorBanner>{error ?? auth?.error}</ErrorBanner>}
    </div>
  );
}

export function HubResults({
  settled,
  cards,
  runners,
  jobByModel,
  pulling,
  onDownload,
  onCancel,
  onBack,
}: {
  settled: SettledQuery;
  /** What this machine already has, from the page's ONE walk — never the
   *  `local` field on the search reply, which is frozen at the moment of the
   *  search. Null while the walk has not answered. */
  cards: ReadonlyMap<string, DiskCard> | null;
  /** Which engine serves each capability here — the same table the recommended
   *  cards read. */
  runners: ReadonlyMap<string, SectionRunner>;
  jobByModel: Map<string, Job>;
  /** The page's three-way guard: reported, just clicked, or settling. */
  pulling: (id: string) => boolean;
  onDownload: (id: string, capability: string) => void;
  onCancel: (job: Job) => void;
  /** Clears query AND task filter — the same act as the ✕ in the box. */
  onBack: () => void;
}) {
  const [models, setModels] = useState<HubModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  // Bumped when a login lands, so the SAME query is asked again with the token
  // in force — the reply is where `authenticated` and the rows come from
  // together, and a flag flipped locally would be a second source for one fact.
  const [authEpoch, setAuthEpoch] = useState(0);
  // Sizes for the PAGE-LEVEL sort, and only once every row of the answer on
  // screen has one. Null means "not sorting by size, or not finished measuring",
  // and in both cases the grid is drawn in the server's order.
  const [sizes, setSizes] = useState<ReadonlyMap<string, number | null> | null>(null);
  const [measuring, setMeasuring] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    // `wireSort`, never `settled.sort`: "size" is a page-level ordering the
    // Hub's list endpoint cannot perform, and the server's own allowlist would
    // reject it (`_SORTS` in routers/hub_models.py). It asks for the
    // most-downloaded candidates and the pass below reorders them.
    searchHubModels({
      q: settled.q,
      task: settled.task,
      sort: wireSort(settled.sort),
      limit: LIMIT,
    }).then(
      (data) => {
        if (!alive) return;
        setLoading(false);
        // A reachable server that could not reach the Hub answers 200 with an
        // `error` — the request was fine, the far side was not, and the
        // difference is worth keeping. The rows it carries ARE the answer
        // (empty), so they replace what is on screen; a hard rejection below
        // does not, because it is not an answer to anything.
        setError(data.error ?? null);
        setModels(data.models);
        setEndpoint(data.endpoint ?? null);
        // Whether this machine holds a Hub token — never the token, only the
        // fact. It decides what a gated card offers (`gateChrome`), and it
        // comes from the same reply as the rows so the two cannot describe
        // different moments.
        setAuthenticated(!!data.authenticated);
      },
      (e: Error) => {
        if (!alive) return;
        setLoading(false);
        // The last good rows STAY. A rejected request is not news about the
        // Hub's contents, and clearing the grid would throw away the only
        // answer the reader has for a failure the next keystroke may not
        // repeat — errors are never cached, here or on the server (D255).
        setError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, [settled, authEpoch]);

  // Sorting by SIZE, which the Hub cannot do (`ResultSort`). The candidate set
  // above came back ranked by downloads; this measures it and reorders it here.
  //
  // **All at once, at the end — never progressively.** Every size is a separate
  // request, they land in whatever order the Hub answers in, and re-sorting on
  // each one would walk cards out from under a reader's cursor two dozen times.
  // So the grid keeps the server's order, dimmed the way a refetch dims it, with
  // the heading saying what it is waiting for; the order changes once.
  //
  // Keyed on the ANSWER (`models`) rather than on the query, because the answer
  // is what has to be measured, and the identity of that array is what changes
  // when a new one lands. A query or sort changed mid-pass flips `alive`, which
  // stops the lookups as well as the state write.
  useEffect(() => {
    if (!sortsOnPage(settled.sort) || !models || models.length === 0) {
      setSizes(null);
      setMeasuring(false);
      return;
    }
    // Only the repos with no weights estimate need asking about: the sort ranks
    // by the figure each card SHOWS (`hubSizeBytes`), and for most results that
    // figure rode in on the search reply for free. So a size sort asks the Hub
    // about exactly the repos a card would have asked about on its own — often
    // none at all — rather than one request per result.
    const ids = models.filter((m) => !m.estimatedSize).map((m) => m.id);
    // Already answered, every one of them — nothing to ask, a second visit to
    // this sort, or a grid the cards themselves measured on the way past. No note
    // and no dimming: the results simply arrive in order.
    if (ids.every((id) => knownTotalSize(id) !== undefined)) {
      setSizes(new Map(ids.map((id) => [id, knownTotalSize(id) as number | null])));
      setMeasuring(false);
      return;
    }
    let alive = true;
    // The previous answer's sizes are dropped rather than reused for the rows
    // they still cover: a half-applied order is a grid claiming to be sorted.
    setSizes(null);
    setMeasuring(true);
    measureSizes(ids, () => alive).then((got) => {
      if (!alive) return;
      setMeasuring(false);
      setSizes(got);
    });
    return () => {
      alive = false;
    };
  }, [models, settled.sort]);

  const chrome = searchChrome(settled.q, settled.task);
  // The host this page is asking, named and reachable. The server reports the
  // endpoint it actually used (HF_ENDPOINT, validated http(s) there), so a
  // machine pointed at a mirror says the mirror's name and links to the mirror
  // — the caption exists to disclose who is being asked, and a name that went
  // somewhere else would defeat it.
  const hostUrl = endpoint || "https://huggingface.co";
  const host = hostUrl.replace(/^https?:\/\//, "");
  const summary = resultsSummary(settled.q, models?.length ?? null, host, !!error);
  // The order the grid is DRAWN in. The server's, unless a page-level sort has
  // finished measuring — and then by the figure each card is SHOWING, so the
  // column of sizes beside the names actually ascends (`hubSizeBytes`).
  // `bySizeAscending` is stable, so the server's ranking survives as the
  // tie-break and the repos with no size at all stay in it, last.
  const shown =
    models && sortsOnPage(settled.sort) && sizes
      ? bySizeAscending(models, (m) => hubSizeBytes(m, sizes.get(m.id)))
      : models;

  return (
    <section className="am-section">
      <div className="am-section-head">
        {/* The section tier, in the slot "User downloaded models" occupies —
            because that is what it replaced, and a grid that took the page over
            without saying its own name would leave a reader who scrolled and
            looked back up with no idea which of two things they were in. */}
        <h3 className="am-section-title">{chrome.heading}</h3>
        <span className="am-discover-headmeta">
          {summary && <span className="am-discover-summary">{summary}</span>}
          {/* Why the grid is dimmed and not yet in the order that was asked
              for. In the heading rather than over the cards, beside the count
              it is about to reorder — and only while it is true, which is at
              most the few seconds a size sort spends measuring. */}
          {measuring && <span className="am-discover-summary">measuring sizes…</span>}
          {/* The second way back, in the row somebody looking at results they
              did not want is already reading. The ✕ in the box is the one you
              find when you go looking for it; this is the one you cannot miss,
              and it says where it GOES rather than what it erases — "clear"
              describes the mechanism, and the reader's question is "how do I
              get my models back". */}
          <button
            type="button"
            className="am-hub-back"
            onClick={onBack}
            title="Clear the search and the task filter (Esc)"
          >
            ← Back to models
          </button>
        </span>
      </div>

      {/* Said plainly, once, and only while a query is live: this is the one
          place in the app that asks a third party a question. */}
      {chrome.showsSearchNote && (
        <p className="cc-caption am-hub-note">
          Searching{" "}
          <a
            className="am-hub-host"
            href={hostUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={`Open ${host} in a new tab`}
          >
            {host}
          </a>
          , limited to models an engine here can load.
        </p>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {/* Only where a gated result needs it (`needsHubLogin`) — a standing
          offer of an account on every search would be this page recommending
          one to somebody who never hit a wall. */}
      {needsHubLogin(models, authenticated) && (
        <HubLogin onSignedIn={() => setAuthEpoch((n) => n + 1)} />
      )}

      {loading && models === null && <p className="cc-empty">Asking {host}…</p>}
      {models !== null && models.length === 0 && !error && (
        <p className="cc-empty">
          Nothing on {host} matches that — among the models this app can run.
        </p>
      )}
      {shown !== null && shown.length > 0 && (
        // A refetch in flight DIMS the rows rather than replacing them: the old
        // answer is the best one there is until the new one lands, and swapping
        // it for empty space makes typing feel like the page is breaking. A size
        // pass is the same situation — these are the right rows in the wrong
        // order — so it wears the same treatment rather than inventing one.
        <div className={"cc-mdgrid am-grid" + (loading || measuring ? " am-hub-stale" : "")}>
          {shown.map((m) => (
            <HubResultCard
              key={m.id}
              model={m}
              runner={runners.get(m.capability) ?? null}
              disk={resultDisk(m.id, cards)}
              authenticated={authenticated}
              busy={pulling(m.id)}
              job={jobByModel.get(m.id)}
              onDownload={() => onDownload(m.id, m.capability)}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
    </section>
  );
}
