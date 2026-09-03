// The Hugging Face cache walk, and everything derived from it — held ONCE for
// the whole page, above the tab strip.
//
// **This is why the tabs are not four independent routes.** The scan is a
// filesystem crawl over every blob in the cache, and its answer is what BOTH
// faces of the Local tab read — the capability carousels, and the Hub search
// results that ask it "do I already have this one" (D426: one listing, one
// definition of on-this-machine, and no window where the two faces disagree
// about the same repo) — plus the page's own caption, which states the cache
// path and total. Mounting each tab as its own page would mean either three
// walks or three answers.
//
// It was inline in `AiModels.tsx`, tangled with the Local tab's delete dialogs
// and expander state. What moved here is only the part that is SHARED; what
// stayed in LocalTab is the part that is one tab's business (which card is
// expanded, which delete is pending, what a failed delete said).
import { useEffect, useRef, useState } from "react";
import { isBusy, refreshAiRuntime, useAiRuntime } from "./aiRuntime";
import {
  getAiModels,
  type AiLoadedModel,
  type AiModelRepo,
  type AiModelsResult,
} from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { activeJobByModel, fetchJobs, type Job } from "@platform/lib/jobs";

export type CacheLoad =
  | { status: "loading" }
  | { status: "ok"; data: AiModelsResult }
  | { status: "error"; message: string };

export interface CacheScan {
  load: CacheLoad;
  /** The listing, or null while it is loading or errored. */
  data: AiModelsResult | null;
  repos: AiModelRepo[];
  /** Resident workers by model id. */
  loadedById: Map<string, AiLoadedModel>;
  /** Download-manager rows by model id. */
  jobByModel: Map<string, Job>;
  /** Model ids with a pull in flight. */
  downloading: Set<string>;
  /** Models whose pull has ended but whose confirming walk has not landed. */
  settling: Set<string>;
  runtime: ReturnType<typeof useAiRuntime>;
  /** Which generation of the walk this is. Exposed so a tab with its OWN
   *  server-side answer to refresh can ride the same trigger rather than invent
   *  a second one — the Local tab's curated catalog is the case: a finished
   *  download changes which models are worth recommending, and an engine switch
   *  replaces the shortlist rather than reordering it, which are exactly the two
   *  things that bump this. Riding it is what keeps a recommended card and the
   *  repo card that replaces it from being one poll apart. */
  scanEpoch: number;
  /** Re-walk the cache. Not a Refresh button (D256) — every caller is the app
   *  noticing that the listing it holds is no longer true. */
  bumpScan: () => void;
  /** Adopt a listing the server just handed back (the delete endpoint answers
   *  with the fresh one, so a delete costs no second walk). */
  publishListing: (result: AiModelsResult) => void;
}

export function useCacheScan(): CacheScan {
  const [load, setLoad] = useState<CacheLoad>({ status: "loading" });
  // Bumped to re-walk. Two writers are the disk really changing: a finished
  // download is a new repo, and a page still showing "not downloaded" beside a
  // finished pull is the same lie the ✓-on-click bug was. The third is an
  // engine switch, where the disk is untouched and the ANSWER about it changed
  // instead — `repo.engine` is the registry's verdict under the current
  // preference, so a switch rewrites a tag and a Load refusal on every card
  // without moving a byte.
  const [scan, setScan] = useState(0);
  const [jobs, setJobs] = useState<Job[]>([]);
  // Models whose pull has ended but whose confirming walk has not landed. For
  // that moment they are in neither the runtime's downloading list nor the
  // listing, and a card reading only those two put a Download button back on a
  // model that had just finished downloading.
  const [settling, setSettling] = useState<Set<string>>(new Set());

  // What is resident, and the download-manager rows for anything mid-bring-up.
  // The runtime is polled by a shared subscriber (the sidebar dot reads the
  // same one); the job rows are read here because only this page joins them
  // onto cards, and only while something is actually running.
  const runtime = useAiRuntime();
  // Returning to the page re-checks what's loaded RIGHT NOW rather than waiting
  // out the idle poll's 10s (aiRuntime.ts) — the same "cheap state, re-read on
  // return" posture as the deploy dot and account status (lib/hooks.ts).
  // Deliberately narrower than the disk walk below: that scan is a filesystem
  // crawl over every blob and stays gated on a KNOWN change (a delete or a
  // finished download), never on a focus tick.
  useRefreshOnReturn(refreshAiRuntime);

  useEffect(() => {
    let alive = true;
    // A RE-walk keeps the listing on screen while it runs: replacing a good
    // grid with "Reading the cache…" because a download finished would make the
    // page flash for news that only adds one card.
    setLoad((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    getAiModels().then(
      (data) => alive && setLoad({ status: "ok", data }),
      (e: Error) => alive && setLoad({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
    // Once per mount, then only when the listing is KNOWN to be wrong — never
    // on a focus/return tick, which would re-walk tens of thousands of files
    // every time the user alt-tabbed back, and never behind a Refresh button,
    // which asked the user to know when a re-walk was worth it.
  }, [scan]);

  const anyBusy = isBusy(runtime);
  useEffect(() => {
    // Held at the PAGE level, not per tab: a Download started from the
    // playground's own picker is a job row the Local tab draws on its cards, and
    // gating the poll on one tab left the other's cards frozen on "Starting…".
    // Only while something is live: the manager already polls these for its own
    // list, and a second poller on an idle machine is two requests a second for
    // an empty array.
    if (!anyBusy) {
      setJobs([]);
      return;
    }
    let alive = true;
    const tick = () =>
      fetchJobs().then(
        (s) => alive && setJobs(s.jobs),
        () => {},
      );
    void tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [anyBusy]);

  // A download that has STOPPED being reported has landed (or failed), and
  // either way the disk is not what the last walk said it was. This is the one
  // honest trigger for a re-walk: the transition, not a timer and not a click.
  const downloadingKey = runtime.downloading
    .map((d) => d.model)
    .sort()
    .join(" ");
  const previousDownloads = useRef<string[]>([]);
  useEffect(() => {
    const now = downloadingKey ? downloadingKey.split(" ") : [];
    const before = previousDownloads.current;
    previousDownloads.current = now;
    // Only on a set that SHRANK. A set that GREW means a pull just started, and
    // a walk then would find exactly the disk the page already knows about.
    const finished = before.filter((model) => !now.includes(model));
    if (!finished.length) return;
    // The walk takes a moment, and for that moment the model is in NEITHER the
    // downloading list nor the listing — which is how a finished download got a
    // "Download" button back for a beat. It stays "finishing" until the walk it
    // just triggered answers for it.
    setSettling((s) => new Set([...s, ...finished]));
    setScan((n) => n + 1);
  }, [downloadingKey]);

  const data = load.status === "ok" ? load.data : null;

  useEffect(() => {
    // Any fresh listing settles every pending question: it either found the
    // model or it did not, and a failed or cancelled pull must not sit as
    // "finishing" for the rest of the session waiting for a success that is
    // not coming.
    if (data) setSettling((s) => (s.size ? new Set() : s));
  }, [data]);

  const repos = data?.repos ?? [];
  const loadedById = new Map(runtime.loaded.map((m) => [m.model, m]));
  const jobByModel = activeJobByModel(jobs);
  // What "you already have this one" MEANS is deliberately NOT answered here.
  // It was, once — a map of id → path for every repo with a materialised
  // snapshot, held at this level so the Local and Discover tabs could not
  // disagree. Since D424 there are two questions and this map answered neither
  // well: "this machine HAS the model" (a ✓, a settled Download click) is not
  // "this model already has a card here", which a cancelled download also
  // satisfies. The reading lives in `aiModelGroups.diskCards` now, next to the
  // merge that consumes it, and the one surface that used this map is drawn from
  // that one (D426) — so there is still exactly one definition per page, it is
  // simply the right one.
  const downloading = new Set(runtime.downloading.map((d) => d.model));

  return {
    load,
    data,
    repos,
    loadedById,
    jobByModel,
    downloading,
    settling,
    runtime,
    scanEpoch: scan,
    bumpScan: () => setScan((n) => n + 1),
    publishListing: (result) => setLoad({ status: "ok", data: result }),
  };
}
