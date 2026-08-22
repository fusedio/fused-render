// The embeddings stage: show what "search by meaning" is, in one click.
//
// An embedding model has no output a newcomer can look at — a vector is 768
// floats — so this stage demos the thing vectors are FOR: rank a handful of
// lines against a query by meaning rather than by shared words. The form
// comes prefilled so the first click produces a ranked list with zero typing,
// and the prefilled query deliberately shares no word with its best matches —
// that gap is the whole demonstration.
//
// One POST per run (`/api/ai/embed`, SPEC §40): the query and the lines ride
// the same batch, so every score comes from one forward pass. Vectors return
// unit-length (embed_common.unit_normalize, in both workers), so a dot
// product IS the cosine similarity — no other math lives here.
//
// Bars are scaled against the BEST match, never drawn raw: SigLIP
// text-to-text cosines sit around 0.2–0.4, and a 31% bar on the right answer
// reads as broken to the reader this tab exists for. The raw score stays on
// hover. Query and lines are session state, never URL state — they are the
// transcript, and the URL carries only the setup (PlaygroundTab's rule).
import { useEffect, useRef, useState } from "react";
import { embedTexts, ModelLoading, watchJob } from "./client";
import { AdvancedPanel } from "./controls";

// The prefill: a query about food against lines where the matches say
// "delicious" and "bakery", not "food" — a keyword search finds nothing here,
// which is exactly the point being made.
const DEFAULT_QUERY = "good things to eat";
const DEFAULT_LINES = [
  "The pasta at that little place was delicious",
  "My laptop battery died on the train",
  "Fresh bread from the corner bakery",
  "The hike took four hours in the rain",
  "She fixed the bug by reverting one commit",
  "We grilled vegetables in the garden all evening",
].join("\n");

// One under embed_common.MAX_ITEMS (64): the query rides in the same batch.
const MAX_LINES = 63;

interface Ranked {
  text: string;
  score: number;
}

export function EmbedStage({ model, downloaded }: { model: string; downloaded: boolean }) {
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [lines, setLines] = useState(DEFAULT_LINES);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ranked, setRanked] = useState<Ranked[] | null>(null);

  // The run itself is one quick POST, but the cold-start watch loop is not —
  // leaving the stage must stop it, same as the chat stage's rule.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const run = async () => {
    const asked = query.trim();
    const items = lines
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    if (!asked || !items.length || busy) return;
    if (items.length > MAX_LINES) {
      setError(`At most ${MAX_LINES} lines at a time — the search itself rides in the same batch.`);
      return;
    }
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const ask = () => embedTexts(model, [asked, ...items]);
    try {
      let result;
      try {
        result = await ask();
      } catch (e) {
        if (!(e instanceof ModelLoading)) throw e;
        // The run STARTED the load — watch it, then ask again, once (the same
        // dance the chat stage does on AI-5's 409).
        setStatus(
          downloaded
            ? "Loading the model into memory — the first run pays for this once…"
            : "Downloading the model — the first run pays for this once…",
        );
        if (e.jobId) {
          const outcome = await watchJob(e.jobId, controller.signal, (job) =>
            setStatus(job.detail || "Loading the model…"),
          );
          // Stopped from the Activity panel — asking again only earns a second
          // 409 (the same reasoning as the chat stage's dance).
          if (outcome.state === "cancelled") throw new Error("the model load was cancelled");
        }
        setStatus(null);
        result = await ask();
      }
      const [queryVector, ...itemVectors] = result.vectors;
      const scored = items.map((text, at) => ({
        text,
        score: (itemVectors[at] || []).reduce(
          (sum, value, dim) => sum + value * (queryVector?.[dim] ?? 0),
          0,
        ),
      }));
      scored.sort((a, b) => b.score - a.score);
      setRanked(scored);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      abortRef.current = null;
    }
  };

  const best = ranked?.length ? Math.max(ranked[0].score, 1e-6) : 1;

  return (
    <div className="pg-work pg-embed">
        {/* The action only: the hero card above names the model and its state. */}
        <h2 className="pg-work-title">Search lines by meaning</h2>
        <p className="pg-embed-intro">
          Lines are ranked by how close their meaning is to the search, even when they share no
          words with it. Run the example, then swap the lines in Config for your own.
        </p>

        <div className="pg-composer">
          <input
            type="text"
            value={query}
            placeholder="What are you looking for?"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void run();
            }}
          />
          {ranked && !busy && (
            <button
              type="button"
              className="pg-ghost-btn pg-clear"
              title="Clear the results"
              onClick={() => setRanked(null)}
            >
              Clear
            </button>
          )}
          <button
            type="button"
            className="btn btn-primary pg-send"
            disabled={busy || !query.trim() || !lines.trim()}
            title="Enter to run"
            onClick={() => void run()}
          >
            {busy ? "Searching…" : "Search"} <kbd className="pg-kbd">⏎</kbd>
          </button>
        </div>

        <AdvancedPanel>
          <label className="pg-ctl">
            <span className="pg-ctl-head">
              <span className="pg-ctl-label">Lines to search</span>
            </span>
            <textarea
              className="pg-embed-lines"
              rows={7}
              value={lines}
              placeholder="One line per entry"
              onChange={(e) => setLines(e.target.value)}
            />
            <span className="pg-ctl-hint">One line per entry, up to {MAX_LINES}.</span>
          </label>
        </AdvancedPanel>

        {status && <p className="pg-status">{status}</p>}
        {error && <p className="pg-error">{error}</p>}
        {ranked && !busy && (
          <div className="pg-answer-block">
            <p className="pg-answer-label">Ranked by meaning</p>
            <ol className="pg-embed-results">
              {ranked.map((row, at) => (
                <li
                  key={at}
                  className="pg-embed-row"
                  title={`Similarity ${row.score.toFixed(3)} — 1 is identical meaning, 0 is unrelated`}
                >
                  <span
                    className="pg-embed-bar"
                    style={{ width: `${Math.max(0, (row.score / best) * 100)}%` }}
                    aria-hidden="true"
                  />
                  <span className="pg-embed-text">{row.text}</span>
                  <span className="pg-embed-score">{row.score.toFixed(2)}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
    </div>
  );
}
