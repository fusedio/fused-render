// The one section a query puts on the Local page: what the Hub has that this
// machine can run.
//
// **It REPLACES the page rather than joining it** (`searchChrome`, the either/or
// in LocalTab): the capability lists answer "what should I even get"; once the
// reader has typed they have a better question, and two lists on screen would
// ask which of them is answering. So one section, named at the top in the slot
// the sections it replaced use, with the way back beside its own heading.
//
// **One of the five orderings is this section's own work.** The Hub's list
// endpoint cannot rank a search by size, so "Size" asks the server for the
// most-downloaded candidates and reorders them here, once, by the figure each
// row is SHOWING and only when every one of those is known.
//
// The join that makes an in-app Hub search worth having is "you already have
// this one", and the page's own walk is the only honest source for it.
import { useEffect, useState } from "react";
import { HubResultRow } from "./RecommendedRow";
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
import { ErrorNote } from "@apps/ai_models/shared/ErrorNote";
import {
  cancelHfLogin,
  getHfAuth,
  searchHubModels,
  startHfLogin,
  type HfAuth,
  type HubModel,
} from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";
import { Button } from "@platform/shadcn/ui/button";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { Muted, SectionTitle, Tiny } from "@platform/ui/flow/Typography";
import { cn } from "@platform/lib/utils";

/** A settled query — what the debounce hands over, and the only thing this
 *  section is keyed on. */
export interface SettledQuery {
  q: string;
  task: string;
  /** The PAGE's sort, which may be one the Hub cannot perform (`wireSort`). */
  sort: ResultSort;
}

/** How many rows one question is worth (the server over-fetches, HS-0a). */
const LIMIT = 24;

/** How many size lookups the page will have outstanding at once while sorting
 *  by size — the difference between a considerate burst and a rate limit. */
const SIZE_LOOKUPS = 4;

/** Every repo's total, measured with at most `SIZE_LOOKUPS` in flight. `alive`
 *  is asked between repos: a query that changed mid-pass must stop SENDING. */
async function measureSizes(ids: readonly string[], alive: () => boolean): Promise<Map<string, number | null>> {
  const out = new Map<string, number | null>();
  let next = 0;
  const worker = async () => {
    while (alive() && next < ids.length) {
      const id = ids[next++];
      out.set(id, await lookupTotalSize(id));
    }
  };
  await Promise.all(Array.from({ length: Math.min(SIZE_LOOKUPS, ids.length) }, () => worker()));
  return out;
}

/** Signing in to Hugging Face, offered where a gate makes it matter — the
 *  device-code flow, not a token box (D402). It POLLS while a login is pending:
 *  the thing being waited for is a person in another tab. */
function HubLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [auth, setAuth] = useState<HfAuth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pending = auth?.pending ?? null;
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const id = setInterval(() => {
      getHfAuth().then(
        (a) => alive && setAuth(a),
        () => undefined,
      );
    }, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
    // Keyed on WHETHER a login is in flight, not on the object.
  }, [pending !== null]);

  useEffect(() => {
    if (auth?.signedIn) onSignedIn();
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
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card px-4 py-2">
      {auth?.pending ? (
        <>
          <Button
            size="sm"
            nativeButton={false}
            render={<a href={auth.pending.url} target="_blank" rel="noopener noreferrer" />}
          >
            Authorize on huggingface.co
          </Button>
          <Tiny>
            Waiting for you to authorize. If asked for a code, enter{" "}
            <code className="font-mono">{auth.pending.userCode}</code> — it expires in{" "}
            {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
          </Tiny>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void act(cancelHfLogin)}>
            Cancel
          </Button>
        </>
      ) : (
        <>
          <Button
            size="sm"
            disabled={busy}
            title="Start the Hugging Face device-code login. The token is stored by huggingface_hub, never by this app."
            onClick={() => void act(() => startHfLogin())}
          >
            Log in to Hugging Face
          </Button>
          <Tiny>Some results below are gated. Downloading one needs a token on this machine.</Tiny>
        </>
      )}
      {(error || auth?.error) && <ErrorNote>{error ?? auth?.error}</ErrorNote>}
    </div>
  );
}

export function HubResults({
  settled,
  cards,
  runners,
  curated,
  jobByModel,
  pulling,
  onDownload,
  onCancel,
  onBack,
}: {
  settled: SettledQuery;
  /** What this machine already has, from the page's ONE walk — never the
   *  `local` field on the search reply. Null while the walk has not answered. */
  cards: ReadonlyMap<string, DiskCard> | null;
  runners: ReadonlyMap<string, SectionRunner>;
  /** Every repo id the curation names, for the seal beside a result's name. */
  curated: ReadonlySet<string>;
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
  // Bumped when a login lands, so the SAME query is asked again with the token.
  const [authEpoch, setAuthEpoch] = useState(0);
  // Sizes for the PAGE-LEVEL sort, and only once every row has one.
  const [sizes, setSizes] = useState<ReadonlyMap<string, number | null> | null>(null);
  const [measuring, setMeasuring] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
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
        // `error`; the rows it carries ARE the answer (empty).
        setError(data.error ?? null);
        setModels(data.models);
        setEndpoint(data.endpoint ?? null);
        setAuthenticated(!!data.authenticated);
      },
      (e: Error) => {
        if (!alive) return;
        setLoading(false);
        // The last good rows STAY: a rejected request is not news about the
        // Hub's contents (D255).
        setError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, [settled, authEpoch]);

  // Sorting by SIZE, which the Hub cannot do. All at once, at the end — never
  // progressively, which would walk rows out from under a reader's cursor.
  useEffect(() => {
    if (!sortsOnPage(settled.sort) || !models || models.length === 0) {
      setSizes(null);
      setMeasuring(false);
      return;
    }
    const ids = models.filter((m) => !m.estimatedSize).map((m) => m.id);
    if (ids.every((id) => knownTotalSize(id) !== undefined)) {
      setSizes(new Map(ids.map((id) => [id, knownTotalSize(id) as number | null])));
      setMeasuring(false);
      return;
    }
    let alive = true;
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
  // The host this page is asking, named and reachable (HF_ENDPOINT-aware).
  const hostUrl = endpoint || "https://huggingface.co";
  const host = hostUrl.replace(/^https?:\/\//, "");
  const summary = resultsSummary(settled.q, models?.length ?? null, host, !!error);
  const shown =
    models && sortsOnPage(settled.sort) && sizes
      ? bySizeAscending(models, (m) => hubSizeBytes(m, sizes.get(m.id)))
      : models;

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SectionTitle>{chrome.heading}</SectionTitle>
        <div className="flex items-center gap-3">
          {summary && <Tiny>{summary}</Tiny>}
          {measuring && <Tiny>measuring sizes…</Tiny>}
          {/* The second way back, in the row somebody looking at results they
              did not want is already reading. It says where it GOES. */}
          <Button variant="ghost" size="sm" onClick={onBack} title="Clear the search and the task filter (Esc)">
            ← Back to models
          </Button>
        </div>
      </div>

      {/* Said plainly, once: this is the one place in the app that asks a third
          party a question. */}
      {chrome.showsSearchNote && (
        <Tiny>
          Searching{" "}
          <a
            className="underline-offset-4 hover:text-foreground hover:underline"
            href={hostUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={`Open ${host} in a new tab`}
          >
            {host}
          </a>
          , limited to models an engine here can load.
        </Tiny>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {/* Only where a gated result needs it (`needsHubLogin`). */}
      {needsHubLogin(models, authenticated) && <HubLogin onSignedIn={() => setAuthEpoch((n) => n + 1)} />}

      {loading && models === null && <Muted className="py-6 text-center">Asking {host}…</Muted>}
      {models !== null && models.length === 0 && !error && (
        <Muted className="py-6 text-center">Nothing on {host} matches that — among the models this app can run.</Muted>
      )}
      {shown !== null && shown.length > 0 && (
        // A refetch in flight DIMS the rows rather than replacing them; a size
        // pass wears the same treatment.
        <EntityList className={cn("transition-opacity", (loading || measuring) && "opacity-60")}>
          {shown.map((m) => (
            <HubResultRow
              key={m.id}
              model={m}
              curated={curated.has(m.id)}
              runner={runners.get(m.capability) ?? null}
              disk={resultDisk(m.id, cards)}
              authenticated={authenticated}
              busy={pulling(m.id)}
              job={jobByModel.get(m.id)}
              onDownload={() => onDownload(m.id, m.capability)}
              onCancel={onCancel}
            />
          ))}
        </EntityList>
      )}
    </section>
  );
}
