// The embeddings stage: show what "search by meaning" is, in one click.
//
// An embedding model has no output a newcomer can look at — a vector is 768
// floats — so this stage demos the thing vectors are FOR: rank a handful of
// lines against a query by meaning rather than by shared words. The lines come
// prefilled and the query does not — a starter pill fills both and runs — and
// every sample's query deliberately shares no word with its best matches.
//
// One POST per run (`/api/ai/embed`, SPEC §40): the query and the lines ride
// the same batch. Vectors return unit-length, so a dot product IS the cosine
// similarity — no other math lives here.
//
// Bars are scaled against the BEST match, never drawn raw: SigLIP
// text-to-text cosines sit around 0.2–0.4, and a 31% bar on the right answer
// reads as broken. The raw score stays on hover. Query and lines are session
// state, never URL state — they are the transcript.
//
// **TWO CONTROLS THAT APPEAR PER MODEL, both off the server's own answer**
// (SPEC §40): a query/document toggle, only where `entry.promptScheme` is
// non-null; and an IMAGES mode, only where `entry.acceptsPaths` is true. Both
// read the server's flags and neither re-derives them.
import { useEffect, useRef, useState } from "react";
import { embedPaths, embedTexts, withModelReady } from "./client";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Button } from "@platform/shadcn/ui/button";
import { Card } from "@platform/shadcn/ui/card";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { cn } from "@platform/lib/utils";
import { Tiny } from "@platform/ui/flow/Typography";
import {
  AnswerBlock,
  ClearButton,
  ComposerCard,
  ComposerSide,
  ConfigPanel,
  RailChips,
  RailField,
  ResultSlot,
  RunButton,
  StageHeader,
  StarterCards,
  StatusLine,
  composerInputClass,
  useConfigOpen,
  type Starter,
} from "./controls";
import { StarterIcons } from "./starterIcons";

// The examples (D465). A sample here is a whole SCENARIO, not a prompt: the
// query and the six lines it is searched against travel together.
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

// The LINES are prefilled and the QUERY is not: the lines are the corpus, they
// live behind the settings cog, and an empty one leaves Search disabled for a
// reason the reader cannot see from the composer.
const DEFAULT_LINES = STARTERS[0].lines.join("\n");

// One under embed_common.MAX_ITEMS (64): the query rides in the same batch.
const MAX_LINES = 63;

// Pictures ride a SEPARATE batch from the query, so the full 64 is available
// here. Held well below it anyway.
const MAX_PICTURES = 12;

// What the file dialog offers, and what is checked after it regardless —
// `embed_common.open_image` reads anything Pillow can, plus HEIC.
const PICTURE_TYPES = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "heic", "heif"];

// The two things a text can BE to a retrieval model — `formats.TEXT_EMBED_KINDS`.
const KINDS = [
  { value: "query" as const, label: "Searching for",
    title: "Prefix these as a QUERY — the thing you are searching WITH" },
  { value: "document" as const, label: "Being searched",
    title: "Prefix these as DOCUMENTS — the things you are searching THROUGH" },
];

/** The last segment of a path, on either separator — `pickFile` hands back a
 *  NATIVE path, so a Windows `C:\\photos\\cat.png` split on "/" alone has no
 *  separator to find. */
function basename(path: string): string {
  const at = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return (at >= 0 ? path.slice(at + 1) : path) || path;
}

interface Ranked {
  text: string;
  score: number;
}

interface RankedPicture {
  path: string;
  name: string;
  score: number;
}

/** One ranked row: the match strength as a wash behind the line, scaled to
 *  the best match, so the top row is always full and the rest read relative
 *  to it. */
function RankedRow({
  bar,
  title,
  thumb,
  text,
  score,
}: {
  bar: number;
  title: string;
  thumb?: string;
  text: string;
  score: number;
}) {
  return (
    <li
      className={cn(
        "relative flex gap-3 overflow-hidden border-b border-border px-3 py-2 text-sm last:border-b-0",
        thumb ? "items-center" : "items-baseline",
      )}
      title={title}
    >
      <span
        className="pointer-events-none absolute inset-y-0 left-0 bg-primary/10"
        style={{ width: `${Math.max(0, bar * 100)}%` }}
        aria-hidden="true"
      />
      {thumb && (
        <img className="relative size-10 flex-none rounded-sm bg-muted object-cover" src={thumb} alt={text} />
      )}
      <span className="relative min-w-0 flex-1">{text}</span>
      <Tiny className="relative flex-none tabular-nums">{score.toFixed(2)}</Tiny>
    </li>
  );
}

export function EmbedStage({
  model,
  downloaded,
  entry,
}: {
  model: string;
  downloaded: boolean;
  entry: AiCatalogModel;
}) {
  const [query, setQuery] = useState("");
  const [lines, setLines] = useState(DEFAULT_LINES);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ranked, setRanked] = useState<Ranked[] | null>(null);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // The server's two per-model answers, read once and never re-derived.
  // `pictureMode` is FORCED off the moment the selected model cannot serve it.
  const ranksPictures = entry.acceptsPaths === true;
  const scheme = entry.promptScheme ?? null;
  const [wantPictures, setWantPictures] = useState(false);
  const pictureMode = ranksPictures && wantPictures;
  const [kind, setKind] = useState<"query" | "document">("document");
  const [pictures, setPictures] = useState<string[]>([]);
  const [rankedPictures, setRankedPictures] = useState<RankedPicture[] | null>(null);
  const [attaching, setAttaching] = useState(false);

  // **Which model produced the scores currently on screen — recorded at the run,
  // not read live.** `model` is the SIDEBAR's selection and changes the instant
  // the reader picks another one, while the results below are still the old
  // model's. Two models can share a dimension and produce vectors in a
  // different space with no error anywhere.
  const [vectorModel, setVectorModel] = useState<string | null>(null);

  // The cold-start watch loop must stop when the stage is left.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // Both inputs are arguments with the current state as their default: a
  // sample card sets the query AND the corpus and runs in the same click.
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
    // `kind` on the corpus side only where the model HAS a scheme.
    const ask = () => embedTexts(model, [asked, ...items], scheme ? kind : undefined);
    try {
      const result = await withModelReady(ask, {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
      const [queryVector, ...itemVectors] = result.embeddings;
      const scored = items.map((text, at) => ({
        text,
        score: (itemVectors[at] || []).reduce(
          (sum, value, dim) => sum + value * (queryVector?.[dim] ?? 0),
          0,
        ),
      }));
      scored.sort((a, b) => b.score - a.score);
      setRanked(scored);
      // `response.modelId` — what the SERVER says it used, not what was asked for.
      setVectorModel(result.response?.modelId ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      abortRef.current = null;
    }
  };

  /** Point the ranking at pictures ALREADY on this disk — no copy, no upload:
   *  the OS dialog raised in the server process, exactly as `ImageStage`. */
  const addPicture = async () => {
    setError(null);
    setAttaching(true);
    try {
      const path = await pickFile({
        title: "Choose a picture to rank",
        types: PICTURE_TYPES,
      });
      // A cancel is an answer: nothing changes and nothing is said about it.
      if (path === null) return;
      setPictures((current) =>
        current.includes(path) || current.length >= MAX_PICTURES
          ? current
          : [...current, path],
      );
      // A new picture invalidates the ranking rather than being appended to it
      // unscored.
      setRankedPictures(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAttaching(false);
    }
  };

  /** Rank the chosen pictures against the typed phrase. TWO calls, not one —
   *  the towers are two separate sessions — landing in the SAME space. No
   *  `kind` on either call. */
  const runPictures = async (askText = query) => {
    const asked = askText.trim();
    if (!asked || !pictures.length || busy) return;
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const phrase = await withModelReady(() => embedTexts(model, [asked]), {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
      const images = await embedPaths(model, pictures);
      const queryVector = phrase.embeddings[0] ?? [];
      const scored = pictures.map((path, at) => ({
        path,
        name: basename(path),
        score: (images.embeddings[at] || []).reduce(
          (sum, value, dim) => sum + value * (queryVector[dim] ?? 0),
          0,
        ),
      }));
      scored.sort((a, b) => b.score - a.score);
      setRankedPictures(scored);
      setVectorModel(images.response?.modelId ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      abortRef.current = null;
    }
  };

  const best = ranked?.length ? Math.max(ranked[0].score, 1e-6) : 1;
  const bestPicture = rankedPictures?.length
    ? Math.max(rankedPictures[0].score, 1e-6)
    : 1;

  return (
    <Card className="w-full flex-none gap-3.5 px-(--card-spacing) [--card-spacing:--spacing(6)]">
      <StageHeader
        title={pictureMode ? "Search pictures by meaning" : "Search lines by meaning"}
        configOpen={configOpen}
        onToggleConfig={toggleConfig}
      />
      {/* The MODE switch, drawn only where the model has a vision tower to
          serve the second half (`entry.acceptsPaths`). */}
      {ranksPictures && (
        <RailChips
          label="Mode"
          options={[
            { value: "lines", label: "Lines",
              title: "Rank written lines against the phrase" },
            { value: "pictures", label: "Pictures",
              title: "Rank pictures on this disk against the phrase — the same "
                + "vector space, through this model's vision tower" },
          ]}
          active={pictureMode ? "pictures" : "lines"}
          onPick={(value) => {
            setWantPictures(value === "pictures");
            // Each mode keeps its own ranking, and neither is shown under the
            // other's heading.
            setError(null);
          }}
        />
      )}
      {/* The retrieval KIND, drawn only where this model has a prompt scheme
          to apply — and never in the pictures mode. */}
      {scheme && !pictureMode && (
        <RailChips label="Text kind" options={KINDS} active={kind} onPick={setKind} />
      )}
      <ComposerCard>
        <input
          type="text"
          className={composerInputClass}
          value={query}
          placeholder="What are you looking for?"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void (pictureMode ? runPictures() : run());
          }}
        />
        {/* Same stack as the other composers — Clear at the top, Search at
            the foot. */}
        <ComposerSide>
          {(pictureMode ? rankedPictures : ranked) && !busy && (
            <ClearButton
              title="Clear the results"
              onClick={() => (pictureMode ? setRankedPictures(null) : setRanked(null))}
            />
          )}
          <RunButton
            disabled={
              busy
              || !query.trim()
              || (pictureMode ? !pictures.length : !lines.trim())
            }
            title="Enter to run"
            onClick={() => void (pictureMode ? runPictures() : run())}
          >
            {busy ? "Searching…" : "Search"}
          </RunButton>
        </ComposerSide>
      </ComposerCard>

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        {pictureMode ? (
          <RailField
            label="Pictures to search"
            hint={`Files already on this disk, up to ${MAX_PICTURES}.`}
          >
            <div className="flex flex-col items-start gap-1">
              {pictures.map((path) => (
                <div key={path} className="flex w-full items-center gap-2 text-xs">
                  <span className="min-w-0 flex-1 truncate" title={path}>
                    {basename(path)}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    className="text-muted-foreground"
                    title="Remove this picture"
                    onClick={() => {
                      setPictures((current) => current.filter((p) => p !== path));
                      setRankedPictures(null);
                    }}
                  >
                    Remove
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="xs"
                disabled={attaching || pictures.length >= MAX_PICTURES}
                onClick={() => void addPicture()}
              >
                {attaching ? "Choosing…" : "Add a picture…"}
              </Button>
            </div>
          </RailField>
        ) : (
          <RailField label="Lines to search" hint={`One line per entry, up to ${MAX_LINES}.`}>
            <Textarea
              className="min-h-0 resize-y text-xs leading-relaxed"
              rows={7}
              value={lines}
              placeholder="One line per entry"
              onChange={(e) => setLines(e.target.value)}
            />
          </RailField>
        )}
      </ConfigPanel>

      {/* Until there is a ranking to read, the examples. Each one sets both
          halves of the scenario and runs it. */}
      {!ranked && !busy && !pictureMode && (
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

      {status && <StatusLine status="loading">{status}</StatusLine>}
      {error && <StatusLine status="error">{error}</StatusLine>}
      {pictureMode ? (
        rankedPictures && !busy ? (
          <AnswerBlock label="Ranked by meaning" provenance={vectorModel}>
            <ol className="m-0 list-none overflow-hidden rounded-lg border border-border bg-card p-0">
              {rankedPictures.map((row) => (
                <RankedRow
                  key={row.path}
                  bar={row.score / bestPicture}
                  title={`${row.path} — similarity ${row.score.toFixed(3)}`}
                  // Through `/api/fs/raw`, the one door every local file in
                  // this app goes through.
                  thumb={rawUrl(row.path)}
                  text={row.name}
                  score={row.score}
                />
              ))}
            </ol>
          </AnswerBlock>
        ) : (
          <ResultSlot
            label="Ranked by meaning"
            capability="embeddings"
            note={
              pictures.length
                ? "The pictures come back here, ordered by how close they are to the phrase."
                : "Add a picture or two behind the settings cog, then type what you are looking for."
            }
          />
        )
      ) : ranked && !busy ? (
        <AnswerBlock label="Ranked by meaning" provenance={vectorModel}>
          <ol className="m-0 list-none overflow-hidden rounded-lg border border-border bg-card p-0">
            {ranked.map((row, at) => (
              <RankedRow
                key={at}
                bar={row.score / best}
                title={`Similarity ${row.score.toFixed(3)} — 1 is identical meaning, 0 is unrelated`}
                text={row.text}
                score={row.score}
              />
            ))}
          </ol>
        </AnswerBlock>
      ) : (
        // The slot covers BOTH "nothing has run" and "a re-search is in
        // flight": the ranking is dropped while `busy` so a stale order is
        // never read as the new one.
        <ResultSlot
          label="Ranked by meaning"
          capability="embeddings"
          note="The lines come back here, ordered by how close they are to the query."
        />
      )}
    </Card>
  );
}
