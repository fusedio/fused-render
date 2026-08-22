// The Hugging Face cache walk, and everything derived from it — held ONCE for
// the whole page, above the tab strip.
//
// **This is why the tabs are not five independent routes.** The scan is a
// filesystem crawl over every blob in the cache, and its answer is what THREE
// tabs read: Local draws the cards, Discover asks it "do I already have this
// one" (`onDisk` — one listing, one definition of on-this-machine, and no
// window where the two tabs disagree about the same repo), and the page's own
// caption states the cache path and total. Mounting each tab as its own page
// would mean either three walks or three answers.
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
import { fetchJobs, type Job } from "@platform/lib/jobs";

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
  /** id → on-disk path for every repo with a materialised snapshot, or null
   *  while the walk has not answered — so a Discover card says neither "you
   *  have this" nor "you don't" before the page has any idea. */
  onDisk: Map<string, string> | null;
  /** Model ids with a pull in flight. */
  downloading: Set<string>;
  /** Models whose pull has ended but whose confirming walk has not landed. */
  settling: Set<string>;
  runtime: ReturnType<typeof useAiRuntime>;
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
    // Held at the PAGE level, not per tab: a Download started from Discover is
    // a job row Discover draws on its own cards, and gating the poll on the
    // Local tab left those cards frozen on "Starting…".
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
  // Matched by TITLE, which the supervisor sets to the model id, rather than by
  // re-deriving the job id here: that derivation sanitises characters, and a
  // second copy of the rule in TypeScript would drift from the Python one the
  // moment either changed.
  const jobByModel = new Map(jobs.filter((j) => j.owner === "server").map((j) => [j.title, j]));
  // What Discover means by "you already have this one". A MATERIALISED
  // snapshot, not merely a folder: huggingface_hub creates `models--org--name/`
  // the moment a pull starts, so a set built from folder names alone flipped a
  // suggestion to "✓ downloaded" seconds after Download was pressed.
  //
  // A MAP, id → path, not a set of ids: the same walk that knows we have a
  // model knows where it is, and Discover's Explore link needs the second half.
  // Read from the search reply instead, that path was frozen at the moment of
  // the search and went stale the instant a download finished (`localCopy`).
  const onDisk = data
    ? new Map(repos.filter((r) => r.revisions > 0).map((r) => [r.id, r.path]))
    : null;
  const downloading = new Set(runtime.downloading.map((d) => d.model));

  return {
    load,
    data,
    repos,
    loadedById,
    jobByModel,
    onDisk,
    downloading,
    settling,
    runtime,
    bumpScan: () => setScan((n) => n + 1),
    publishListing: (result) => setLoad({ status: "ok", data: result }),
  };
}
