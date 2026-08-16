// The Discover half of /ai-models: the models this app can actually download
// and run, per capability, with what this machine already has marked.
//
// **It used to be a Hub search box, and removing that is the point of this
// module's current shape.** The search worked — it queried huggingface.co,
// joined the results against this disk and sorted them — and every result it
// returned was read-only, because nothing in this app can load an arbitrary
// repo: a model runs here only if one of the registered engines reads its
// weight format. So the tab offered a browsing surface over tens of thousands
// of models to a page that could act on about a dozen of them, and the caption
// under the box had to say so ("Search results are read-only"). A control whose
// own caption explains that it does nothing is not a feature with a rough edge,
// it is dead weight, and the curated shortlist underneath was the whole answer
// the entire time.
//
// What is left is the curation, and it is worth having inside the app rather
// than in a browser tab for the reason the join always was: huggingface.co
// cannot tell you that the model you are reading about is already in your
// cache and would cost nothing to open — this page can, because its sibling tab
// already measured exactly that.
//
// **Nothing here reaches the network on its own.** The catalog is the server's
// own registry, read locally; the only outbound traffic this tab can cause is a
// download somebody pressed a button for.
import { useEffect, useState } from "react";
import { downloadAiModel, getAiCatalog, type AiCatalogCapability } from "@platform/lib/api";
import { refreshAiRuntime } from "./aiRuntime";
import { ModelProgress } from "./AiProgress";
import type { Job } from "@platform/lib/jobs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// The curated shortlist, per capability, with what this machine can serve.
//
// These lists used to live inside the apps that used them — three MLX models in
// local_chat, one FLUX model hard-coded in the image worker — which put the
// curation where nobody browsing for a model would ever see it.
export default function AiModelsDiscover({
  onDisk,
  downloading,
  settling,
  jobByModel,
}: {
  /** Repo ids with a MATERIALISED snapshot on this disk, or null while the walk
   *  is still running. Owned by the page, so both tabs mean one thing by it. */
  onDisk: Set<string> | null;
  downloading: Set<string>;
  /** Pulls that have STOPPED being reported and whose confirming walk has not
   *  landed yet — the far end of the same gap `pending` covers at the near end. */
  settling: Set<string>;
  jobByModel: Map<string, Job>;
}) {
  // `null` while the catalog request is in flight, which is a state this tab
  // did not have to distinguish before: the search grid used to fill the page
  // while the curation loaded behind it, so an empty `catalog` was invisible.
  // It is the whole tab now, and "still loading" and "the server has nothing"
  // must not both render as a blank page.
  const [catalog, setCatalog] = useState<AiCatalogCapability[] | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    // Just the curation. Whether each entry is on this disk is the PAGE's
    // answer, arriving as `onDisk` — this used to run its own cache walk beside
    // the page's, which meant two definitions of "downloaded" and two moments
    // they were true.
    getAiCatalog().then(
      (cat) => setCatalog(cat.capabilities),
      (e: Error) => {
        // Said out loud rather than swallowed into an empty list. This used to
        // fail silently because the tab had a search box to fall back to; with
        // the catalog as the entire content, a swallowed failure is a page
        // that renders nothing and explains nothing.
        setCatalog([]);
        setLoadError(e.message);
      },
    );
  }, []);

  // The click is held until something ELSE can speak for the pull — the runtime
  // reporting it, `settling` carrying it through the walk, or the walk finding
  // it on disk (a "download" that was a cache hit and finished before any poll
  // saw it). Clearing on the POST's reply put the card back to "Download" for
  // the beat before the next runtime poll, which reads as the button having
  // done nothing.
  const spokenFor =
    pending !== null &&
    (downloading.has(pending) || settling.has(pending) || !!onDisk?.has(pending));
  useEffect(() => {
    if (spokenFor) setPending(null);
  }, [spokenFor]);

  const start = async (model: string, capability: string) => {
    setError(null);
    setPending(model);
    try {
      await downloadAiModel(model, capability);
      // The pull is the server's now. Asking the runtime for a fresh read is the
      // whole follow-up: the card's state comes from what is actually happening,
      // never from the fact that a button was pressed.
      refreshAiRuntime();
    } catch (e) {
      setError((e as Error).message);
      setPending(null);
    }
  };

  return (
    <>
      {loadError && <ErrorBanner>{loadError}</ErrorBanner>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {catalog === null && <p className="cc-empty">Reading the model catalog…</p>}
      {catalog !== null && catalog.length === 0 && !loadError && (
        <p className="cc-empty">No models are suggested for this machine.</p>
      )}
      {(catalog ?? []).map((group) => (
        <section className="am-section" key={group.capability}>
          {/* The Local tab's section heading, exactly: an ALL-CAPS title over a
              rule, with the one secondary fact about the group at the far right
              — there a byte subtotal, here WHICH backend will load these.
              It used to be a lowercase run of inline text ("text generation
              via MLX LM") that read as a caption belonging to the card under
              it rather than as a heading over all of them, so the same page
              said "these things go together" two different ways on two tabs. */}
          <div className="am-section-head">
            <h3 className="am-subgroup-title">{group.capability.replace(/-/g, " ")}</h3>
            {/* WHICH backend will load these, named. One capability can have
                two runners now (text generation: MLX on Apple Silicon, PyTorch
                everywhere else), and the shortlist below differs completely
                between them — so a heading that said only "text generation"
                left the reader with no way to tell which list they were looking
                at, or why it was not the one in the docs. Since D302 that
                backend is a CHOICE rather than only a hardware fact, and the
                title says where from: "my suggested models changed" is
                otherwise an unexplainable event. */}
            {group.available && group.runnerShortLabel && (
              <span
                className="am-suggested-runner"
                title="Chosen on the Engines tab. Each backend loads its own model format, so this list changes with it."
              >
                via {group.runnerShortLabel}
              </span>
            )}
            {/* Shown even when it cannot run here, with the reason: hiding a
                capability leaves someone hunting for a feature that never was. */}
            {!group.available && (
              <span className="am-suggested-why" title={group.reason ?? undefined}>
                unavailable — {group.reason}
              </span>
            )}
          </div>
          {/* What running on this backend is LIKE — the memory ceiling on the
              MLX image runner, the GPU speed of MLX Whisper. It sits ABOVE the
              cards deliberately: it is the thing to know BEFORE starting a
              multi-gigabyte download, and the same sentence discovered
              afterwards is an apology rather than information.
              MUTED, not warning-coloured. It is a standing fact about the
              runner, and three orange paragraphs down a page teach the reader
              that orange here means nothing — what a backend is like is
              context for a choice, not an alarm about one. */}
          {group.available && group.runnerNote && (
            <p className="am-group-note am-suggested-note">{group.runnerNote}</p>
          )}
          <div className="cc-mdgrid am-grid">
            {group.models.map((m) => {
              // FOUR states, and every one of them was a bug at some point:
              //
              //   unknown — the cache walk has not answered yet. Neither the ✓
              //     nor the button, because both would be a claim. Treating
              //     null as an empty set showed Download on a model already on
              //     disk for the length of the first walk.
              //   busy    — a pull is running. This spans three sources, and it
              //     needs all three: `pending` from the click until the runtime
              //     poll sees it, `downloading` while the runtime reports it,
              //     and `settling` from the moment it stops being reported until
              //     the walk that confirms it lands. Drop any one and the
              //     Download button flickers back on live work.
              //   have    — a materialised snapshot. The ✓.
              //   neither — the button.
              const known = onDisk !== null;
              const busy = downloading.has(m.id) || pending === m.id || settling.has(m.id);
              const have = !!onDisk?.has(m.id);
              return (
                <div key={m.id} className="cc-mdcard am-card am-suggestcard">
                  <div className="cc-mdcard-head">
                    <a
                      className="cc-mdcard-name am-card-name"
                      href={`https://huggingface.co/${m.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`Open ${m.id} on the Hub`}
                    >
                      {m.label}
                    </a>
                    {have && !busy && (
                      <span className="am-suggest-have" title={`${m.id} is already on this machine`}>
                        ✓ downloaded
                      </span>
                    )}
                    {/* An unmeasured size is a dash, never a guess. */}
                    <span
                      className="am-card-size"
                      title={
                        m.size_gb === null
                          ? "Nobody has recorded this one's download size yet."
                          : undefined
                      }
                    >
                      {m.size_gb === null ? "—" : `${m.size_gb} GB`}
                    </span>
                  </div>
                  <div className="am-suggest-note">{m.note}</div>
                  {/* No `detail` override: the job says what it is doing
                      ("Fetching weights…", "Preparing MLX…") and a fixed word
                      here would paper over a venv build with "Downloading". */}
                  {busy && <ModelProgress job={jobByModel.get(m.id)} />}
                  <div className="cc-mdcard-foot">
                    <span className="cc-mdcard-meta cc-mono">{m.id}</span>
                    {known && !have && !busy && group.available && (
                      <button
                        type="button"
                        className="am-card-power"
                        onClick={() => start(m.id, group.capability)}
                        title={
                          m.size_gb === null
                            ? `Download ${m.id}`
                            : `Download ${m.id} (~${m.size_gb} GB)`
                        }
                      >
                        Download
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </>
  );
}
