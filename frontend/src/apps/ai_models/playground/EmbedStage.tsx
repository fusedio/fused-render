// The embeddings stage: show what "search by meaning" is, in one click.
//
// An embedding model has no output a newcomer can look at — a vector is 768
// floats — so this stage demos the thing vectors are FOR: rank a handful of
// lines against a query by meaning rather than by shared words. The lines come
// prefilled and the query does not — a starter pill fills both and runs, so the
// first ranked list still costs zero typing — and every sample's query
// deliberately shares no word with its best matches: that gap is the whole
// demonstration.
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
import { embedTexts, withModelReady } from "./client";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import { useConfigOpen, ConfigPanel, RailField, ResultSlot, StageHeader, StarterCards, type Starter } from "./controls";
import { StarterIcons } from "./starterIcons";

// The examples (D465). A sample here is a whole SCENARIO, not a prompt: the
// query and the six lines it is searched against travel together, because the
// demonstration is the gap between them. Every set is built the same way —
// three lines that match the query in meaning while sharing NO word with it,
// three that share nothing at all — so a keyword search would come back empty
// on exactly the lines this ranks first.
interface EmbedSample extends Starter {
  lines: string[];
}

const STARTERS: EmbedSample[] = [
  {
    name: "Good food",
    icon: StarterIcons.bowl,
    prompt: "good things to eat",
    detail: 'Rank six lines against "good things to eat" — the matches never say "food"',
    lines: [
      "The pasta at that little place was delicious",
      "My laptop battery died on the train",
      "Fresh bread from the corner bakery",
      "The hike took four hours in the rain",
      "She fixed the bug by reverting one commit",
      "We grilled vegetables in the garden all evening",
    ],
  },
  {
    name: "Debugging",
    icon: StarterIcons.code,
    prompt: "when the code finally behaved",
    detail: 'Rank six lines against "when the code finally behaved" — no line repeats a word of it',
    lines: [
      "She reverted one commit and the tests went green",
      "The pasta was far too salty",
      "Three hours staring at a missing comma",
      "Our flight left without us",
      "The stack trace pointed at the wrong file",
      "He repainted the kitchen on Sunday",
    ],
  },
  {
    name: "Money",
    icon: StarterIcons.chart,
    prompt: "spending too much",
    detail: 'Rank six lines against "spending too much" — the matches talk about rent and groceries',
    lines: [
      "The rent went up again in March",
      "I biked out to the lake at sunrise",
      "Half my salary vanishes into this flat",
      "The kettle finally boiled",
      "Groceries cost nearly double what they did",
      "We watched two films back to back",
    ],
  },
  {
    name: "Worn out",
    icon: StarterIcons.heart,
    prompt: "I need rest",
    detail: 'Rank six lines against "I need rest" — the matches never use the word tired',
    lines: [
      "I fell asleep on the sofa before nine",
      "The spreadsheet balanced on the first try",
      "Four nights in a row of broken sleep",
      "She won the tournament in straight sets",
      "My eyes will not stay open past lunch",
      "The garden badly needs weeding",
    ],
  },
  {
    name: "Getting away",
    icon: StarterIcons.plane,
    prompt: "a holiday somewhere warm",
    detail: 'Rank six lines against "a holiday somewhere warm" — no line says holiday',
    lines: [
      "Two weeks on a Greek island in September",
      "The compiler warning turned out to be harmless",
      "I booked a night train south to Trieste",
      "He alphabetised the whole bookshelf",
      "My passport expires next month",
      "Rain again, all afternoon",
    ],
  },
  {
    name: "Something to hear",
    icon: StarterIcons.music,
    prompt: "something to listen to",
    detail: 'Rank six lines against "something to listen to" — the matches are about records and playing',
    lines: [
      "That album got me through the whole winter",
      "The router needed a reboot again",
      "She plays cello in a tiny quartet",
      "I fixed the squeaky front door",
      "Vinyl crackle before the first track",
      "The bus was late for the third time",
    ],
  },
  {
    name: "Outside today",
    icon: StarterIcons.landscape,
    prompt: "what it is like outside",
    detail: 'Rank six lines against "what it is like outside" — the matches describe weather without naming it',
    lines: [
      "Fog sat in the valley until noon",
      "The invoice is still unpaid",
      "Hail bounced off the car roof",
      "He taught himself to solder",
      "Twenty-nine degrees and not a breath of wind",
      "The cat slept through the entire day",
    ],
  },
  {
    name: "Learning",
    icon: StarterIcons.book,
    prompt: "picking up a new skill",
    detail: 'Rank six lines against "picking up a new skill" — the matches are practice, not the word skill',
    lines: [
      "Six months of evening classes in Portuguese",
      "The fridge is empty again",
      "I finally understand how recursion works",
      "The train was cancelled without warning",
      "Practising scales an hour a day",
      "We repainted the hallway twice",
    ],
  },
];

// The LINES are prefilled and the QUERY is not. The lines have to be: they are
// the corpus, they live behind the settings cog, and an empty one leaves Search
// disabled for a reason the reader cannot see from the composer. The query used
// to be prefilled too — the first sample's — and that is what the starter pills
// are for: a filled composer nobody typed into reads as a search already made,
// and it made the pills look like they would do something the box had already
// done. Empty, the box asks its question and the pills answer it in one click.
const DEFAULT_LINES = STARTERS[0].lines.join("\n");

// One under embed_common.MAX_ITEMS (64): the query rides in the same batch.
const MAX_LINES = 63;

interface Ranked {
  text: string;
  score: number;
}

export function EmbedStage({ model, downloaded }: { model: string; downloaded: boolean }) {
  const [query, setQuery] = useState("");
  const [lines, setLines] = useState(DEFAULT_LINES);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ranked, setRanked] = useState<Ranked[] | null>(null);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // The run itself is one quick POST, but the cold-start watch loop is not —
  // leaving the stage must stop it, same as the chat stage's rule.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // Both inputs are arguments with the current state as their default: a
  // sample card sets the query AND the corpus and runs in the same click, and
  // reading them back off state would run the PREVIOUS scenario (setState is
  // not visible until the next render).
  const run = async (askText = query, lineText = lines) => {
    const asked = askText.trim();
    const items = lineText
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
      // The same bounded wait the text stage uses (AI-5) — one place, because
      // this dance drifted the moment either copy learned anything.
      const result = await withModelReady(ask, {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
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
    <div className={"pg-work pg-embed" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        {/* The action, and the way to the settings. The hero card above names
            the model and its state. */}
        <StageHeader
          title="Search lines by meaning"
          configOpen={configOpen}
          onToggleConfig={toggleConfig}
        />
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
          {/* Same stack as the other composers — Clear at the top, Search at
              the foot — so Clear sits in one place across the playground and
              never appears BESIDE the input, stealing its width. */}
          <div className="pg-composer-side">
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
        </div>

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        <RailField label="Lines to search" hint={`One line per entry, up to ${MAX_LINES}.`}>
          <Textarea
            className="min-h-0 resize-y text-xs leading-relaxed"
            rows={7}
            value={lines}
            placeholder="One line per entry"
            onChange={(e) => setLines(e.target.value)}
          />
        </RailField>
      </ConfigPanel>


        {/* Until there is a ranking to read, the examples. Each one sets both
            halves of the scenario and runs it — see `run`'s arguments. */}
        {!ranked && !busy && (
          <StarterCards
            samples={STARTERS}
            onPick={(sample) => {
              const corpus = sample.lines.join("\n");
              setQuery(sample.prompt);
              setLines(corpus);
              void run(sample.prompt, corpus);
            }}
          />
        )}

        {status && <p className="pg-status">{status}</p>}
        {error && <p className="pg-error">{error}</p>}
        {ranked && !busy ? (
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
        ) : (
          // The slot covers BOTH "nothing has run" and "a re-search is in
          // flight": the ranking is dropped while `busy` so a stale order is
          // never read as the new one, and without the slot that left the
          // column briefly empty at exactly the moment something is happening.
          <ResultSlot
            label="Ranked by meaning"
            capability="embeddings"
            note="The lines come back here, ordered by how close they are to the query."
          />
        )}
      </Card>
    </div>
  );
}
