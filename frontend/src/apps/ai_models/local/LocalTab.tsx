// The Local tab: what the Hugging Face cache holds on this machine, and the
// deletions that free it.
//
// Biggest-first WITHIN a group, not across the page (lib/aiModelGroups.ts).
// One flat size sort put a 2.4GB component a runner fetched for itself fifth,
// between two models the user chose, and left the distinction to the quietest
// chip on the card. Position now carries meaning: what you chose, by what it
// does; then what an engine fetched; then the repos nothing here recognises.
// Every group states its own byte subtotal, because a group that can be skipped
// must still say what it costs.
//
// It manages that cache too (D250), two ways: delete a repo, or delete one
// revision of one. Both name their targets in a confirmation the user reads
// first, and the dangerous arithmetic (which blobs a revision actually owns)
// lives on the server, where the filesystem is.
//
// THE LISTING IS NOT THIS TAB'S. It arrives as `scan` from the page above
// (lib/useCacheScan.ts), because Discover reads the same walk to answer "do I
// already have this one" — see there. What IS this tab's is everything below:
// which card is expanded, which delete is pending, and what a delete that
// failed had to say about it.
import { useState } from "react";
import { DeleteDialogs } from "./DeleteDialogs";
import { RepoCard } from "./RepoCard";
import { shortCommit } from "./hub";
import { groupRepos, loadRefusal } from "@apps/ai_models/lib/aiModelGroups";
import { publishAiRuntime, refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { tabHref } from "@apps/ai_models/routes";
import {
  deleteAiModels,
  loadAiModel,
  unloadAiModel,
  type AiModelDeleteTarget,
  type AiModelRepo,
  type AiModelRevision,
} from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

/** What a confirmation is about. Every destructive action becomes one of these
 *  first — there is no path from a click straight to a delete. */
export type Pending =
  | { kind: "repo"; repo: AiModelRepo }
  | { kind: "revision"; repo: AiModelRepo; revision: AiModelRevision };

/** A section heading, and no figure beside it.
 *
 *  It carried a byte subtotal, on the argument that a section a reader may skip
 *  has to state its cost on the way past. Three levels of arithmetic said
 *  otherwise on the rendered page: the caption's "33 GB total", a section
 *  subtotal, and a per-capability subtotal, each correct and none of them the
 *  one being looked for. **The subtotals that survive are the ones next to the
 *  cards they are about** — the ALL-CAPS capability rows, which are the level a
 *  reader actually decides to skip at — with the page total in the caption
 *  above. The two figures in between were the sum of one and the parts of the
 *  other, restated in the middle.
 */
function SectionHead({ title }: { title: string }) {
  return (
    <div className="am-section-head">
      <h3 className="am-section-title">{title}</h3>
    </div>
  );
}
export function LocalTab({ scan }: { scan: CacheScan }) {
  const { load, data, repos, loadedById, jobByModel, downloading, publishListing } = scan;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  // Per-target refusals from the last delete (a symlinked repo, a row that was
  // already gone). A banner rather than a toast: it names things the user asked
  // for and did not get.
  const [failures, setFailures] = useState<string[]>([]);

  const runLoad = async (repo: AiModelRepo) => {
    setRuntimeError(null);
    try {
      // The capability travels with the request: without it the API defaults to
      // text generation, and a diffusion model would be handed to the chat
      // runner.
      await loadAiModel(repo.id, repo.capability ?? undefined);
      refreshAiRuntime();
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
  };

  const runUnload = async (repo: AiModelRepo) => {
    setRuntimeError(null);
    try {
      publishAiRuntime(await unloadAiModel(repo.id));
    } catch (e) {
      setRuntimeError((e as Error).message);
    }
  };

  const runDelete = async (targets: AiModelDeleteTarget[], label: string) => {
    setBusy(true);
    try {
      const result = await deleteAiModels(targets);
      // The endpoint answers with the fresh listing, so a delete costs no
      // second walk — published straight into the page's scan state.
      publishListing(result);
      setFailures(
        result.failures.map(
          (f) =>
            `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`,
        ),
      );
      // A deletion that freed nothing is worth saying out loud too — it means
      // every target failed, and the banner beside it says why.
      pushToast({
        msg: result.freed
          ? `Freed ${formatSize(result.freed)} — ${label}`
          : `Nothing deleted — ${label}`,
        tone: result.failures.length ? "error" : "info",
      });
      setPending(null);
    } catch (e) {
      // A transport/guard failure never reached the disk, so the listing on
      // screen is still true — surface it and leave the dialog open.
      setFailures([(e as Error).message]);
    } finally {
      setBusy(false);
    }
  };

  // Derived on every render rather than memoised: it is one pass over a list
  // whose length is the number of repos in a cache, and `repos` is a fresh array
  // each render anyway — a memo keyed on it would recompute every time and cost
  // the comparison on top.
  const grouped = groupRepos(repos);

  // One card, wherever it ends up. Written once because a section is only a
  // heading and a subset — nothing about a card changes with the group it is
  // drawn in, and two copies of this call site would be two places for a prop
  // to go missing.
  const card = (r: AiModelRepo) => (
    <RepoCard
      key={r.path}
      repo={r}
      expanded={expanded === r.dir}
      loaded={loadedById.get(r.id)}
      job={jobByModel.get(r.id)}
      busy={busy}
      fetching={downloading.has(r.id)}
      refusal={loadRefusal(r)}
      onToggle={() => setExpanded(expanded === r.dir ? null : r.dir)}
      onDeleteRepo={() => setPending({ kind: "repo", repo: r })}
      onDeleteRevision={(revision) => setPending({ kind: "revision", repo: r, revision })}
      onLoad={() => runLoad(r)}
      onUnload={() => runUnload(r)}
    />
  );

  return (
    <>
      {load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
      {runtimeError && <ErrorBanner>{runtimeError}</ErrorBanner>}
      {failures.length > 0 && (
        <ErrorBanner>
          {failures.map((f) => (
            <div key={f}>{f}</div>
          ))}
        </ErrorBanner>
      )}
      {load.status === "loading" && (
        <p className="cc-empty">Reading the Hugging Face cache…</p>
      )}
      {data &&
        (repos.length ? (
          <>
            {/* Section A. Everything somebody chose to download, under the
                capability that would serve it. Rendered at all only when
                there is one — a machine holding nothing but a runner's own
                components should not be told it has a Models section. */}
            {grouped.models.groups.length > 0 && (
              <section className="am-section">
                {/* "User downloaded models", not "Models". The page is
                    titled AI Models and every card on it is one, so the bare
                    word restated the page; what actually distinguishes this
                    section from the one below it is WHO asked for these —
                    which is the same distinction "Fetched by engines"
                    already draws from the other side. */}
                <SectionHead title="User downloaded models" />
                {grouped.models.groups.map((group) => (
                  <div className="am-subgroup" key={group.key}>
                    <div className="am-subgroup-head">
                      <h4 className="am-subgroup-title">{group.label}</h4>
                      <span className="am-subgroup-size">{formatSize(group.size)}</span>
                    </div>
                    {group.note && <p className="am-group-note">{group.note}</p>}
                    <div className="cc-mdgrid am-grid">{group.repos.map(card)}</div>
                  </div>
                ))}
              </section>
            )}
            {/* Section B. No sub-grouping: there are a handful of these, and
                it is the HEADING that does the work now — the cards already
                wear "part of X", and scattering them through a size-sorted
                list is what made that chip the only thing distinguishing a
                2.4GB machine-fetched repo from a model the user picked. */}
            {grouped.components.repos.length > 0 && (
              <section className="am-section">
                <SectionHead title="Fetched by engines" />
                {/* One sentence. The three it replaced said WHY these are
                    listed at all, which the heading and the "part of X" tag
                    on every card already answer — what only prose can carry
                    is that deleting one is safe, and that is what is left. */}
                <p className="am-group-note">
                  Automatically downloaded by a runner. If deleted, it will be downloaded
                  again on next run.
                </p>
                <div className="cc-mdgrid am-grid">{grouped.components.repos.map(card)}</div>
              </section>
            )}
          </>
        ) : (
          // Two different nothings: no cache dir at all (nothing has ever
          // pulled from the Hub) versus a cache that has been emptied. The
          // path itself is already in the caption above, so it isn't repeated
          // here.
          //
          // Either nothing ends in the SAME next move, which is why the
          // sidebar entry no longer has to guess whether this page is worth
          // offering (HF-8, D265): Discover is right here, and a machine with
          // no cache is precisely the one that needs it. A button rather than
          // a sentence naming the tab — the tab strip is at the top of the
          // page and the empty state is in the middle of it, so "use
          // Discover" would be an instruction where a control fits.
          <div className="cc-empty am-empty">
            <p>
              {data.exists
                ? "Nothing cached here yet."
                : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
            </p>
            <button type="button" className="btn btn-secondary" onClick={() => navigateUrl(tabHref("discover"))}>
              Search the Hub
            </button>
          </div>
        ))}
      <DeleteDialogs
        pending={pending}
        busy={busy}
        onClose={() => setPending(null)}
        onConfirm={runDelete}
      />
    </>
  );
}
