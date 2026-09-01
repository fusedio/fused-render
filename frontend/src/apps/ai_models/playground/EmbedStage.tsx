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
//
// **TWO CONTROLS THAT APPEAR PER MODEL, both off the server's own answer**
// (SPEC §40), because the capability serves two shapes of checkpoint and the
// route refuses the wrong parameter for either:
//
// * a query/document toggle, only where `entry.promptScheme` is non-null. A
//   retrieval encoder instructs a question and a passage differently; a dual
//   encoder has no such convention and the route 400s a `kind` sent to one,
//   because a parameter that changed nothing would be worse than one refused.
// * an IMAGES mode, only where `entry.acceptsPaths` is true. That is a vision
//   tower, which a prose encoder does not have.
//
// Both read the server's flags and neither re-derives them, which is the rule
// `imageInput.ts` states for `acceptsImage`: a control drawn off anything else
// is a control whose request comes back 400. `=== true` / `!= null` rather than
// truthiness on a possibly-absent field, for that module's reason too — an older
// server sends neither, and absence has to read as "no control".
import { useEffect, useRef, useState } from "react";
import { embedPaths, embedTexts, withModelReady } from "./client";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";
import { Button } from "@platform/shadcn/ui/button";
import { Kbd } from "@platform/shadcn/ui/kbd";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { useConfigOpen, ConfigPanel, RailChips, RailField, ResultSlot, StageHeader, StarterCards, type Starter } from "./controls";
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

// Pictures ride a SEPARATE batch from the query — one call per tower, since the
// two towers are two sessions — so the full 64 is available here. Held well
// below it anyway: this is a demonstration a reader assembles by hand through
// the OS file dialog, one picture per click.
const MAX_PICTURES = 12;

// What the file dialog offers, and what is checked after it regardless. The
// checks are the runner's real ones: `embed_common.open_image` reads anything
// Pillow can, plus HEIC through pillow-heif — which is the format an iPhone
// photo library is actually in, so leaving it out would make the mode work on
// screenshots and fail on the photographs.
const PICTURE_TYPES = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "heic", "heif"];

// The two things a text can BE to a retrieval model — `formats.TEXT_EMBED_KINDS`,
// and the route refuses anything else. "Searching for" / "Being searched" rather
// than "query" / "document": this tab is read by someone who has not met the
// vocabulary, and the API names are on the composer seed and in the skill where
// a page author meets them.
const KINDS = [
  { value: "query" as const, label: "Searching for",
    title: "Prefix these as a QUERY — the thing you are searching WITH" },
  { value: "document" as const, label: "Being searched",
    title: "Prefix these as DOCUMENTS — the things you are searching THROUGH" },
];

/** The last segment of a path, on either separator.
 *
 * `pickFile` hands back a NATIVE path, so a Windows `C:\\photos\\cat.png` split
 * on "/" alone has no separator to find and the whole string becomes the
 * "name" — the full drive path rendered as a filename in the corpus list and in
 * every ranked row. Both separators, because this string's shape depends on the
 * reader's OS and not on anything this component controls.
 */
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

  // The server's two per-model answers, read once and never re-derived — see
  // the module header. `pictureMode` is state because the reader chooses it,
  // but it is FORCED off the moment the selected model cannot serve it: the mode
  // survives a model switch otherwise (the stage remounts per model id, but a
  // reader who switched engines mid-session would otherwise see an images tab
  // whose every request 400s), which is `usableBase`'s argument applied here.
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
  // model's. Rendering `model` beside them would label a list with the name of
  // something that did not compute it, which is worse than saying nothing: this
  // stage is where a person watches scores change and forms a belief about what
  // a model does.
  //
  // It matters here more than the shape suggests. Two models on ONE engine's
  // list can share a dimension — `nomic-embed-text-v1.5` and the SigLIP2 base
  // export are both 768 — so switching models produces vectors that are the same
  // size, in a different space, with no error anywhere. The scores just quietly
  // stop meaning what they meant. `SKILL.md`'s "Store the model beside the
  // vectors" is that rule for a page that persists them; this is the same rule
  // for a surface that displays them.
  const [vectorModel, setVectorModel] = useState<string | null>(null);

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
    // `kind` on the corpus side only where the model HAS a scheme — see the
    // module header on why sending it otherwise is a 400 rather than a no-op.
    // The query and the lines ride one batch, so one `kind` covers both, which
    // is why the toggle says what THESE texts are rather than pretending the two
    // sides can differ in one call. A reader who wants the asymmetric pair runs
    // the corpus as "Being searched" and the query as "Searching for", which is
    // exactly what `fused.ai.embed` lets a page do with two calls.
    const ask = () => embedTexts(model, [asked, ...items], scheme ? kind : undefined);
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
      // `result.model` — what the SERVER says it used, not what was asked for. A
      // bare call takes the capability's default, so the request's own `model` is
      // not always the answer.
      setVectorModel(result.model ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      abortRef.current = null;
    }
  };

  /** Point the ranking at pictures ALREADY on this disk — no copy, no upload.
   *
   *  `<input type=file>` cannot do this: a browser hands over bytes and strips
   *  the path on purpose, and the route takes a PATH because the worker is a
   *  separate process that opens the file itself. So the only way to one is the
   *  OS dialog raised in the server process, exactly as `ImageStage` does it. */
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
      // unscored — a list where one row has no bar reads as a broken render.
      setRankedPictures(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAttaching(false);
    }
  };

  /** Rank the chosen pictures against the typed phrase.
   *
   *  TWO calls, not one, and that is the shape of a dual encoder rather than an
   *  inefficiency: the towers are two separate sessions, so the phrase goes
   *  through the text tower and the pictures through the vision one. They land
   *  in the SAME space — that is what a dual encoder IS — so the dot product
   *  across them is the cosine similarity, the same arithmetic the text mode
   *  uses.
   *
   *  No `kind` on either call: a model that reports `acceptsPaths` is a dual
   *  encoder, and a dual encoder has no retrieval convention, so the toggle is
   *  not drawn in this mode and the route would refuse the field anyway. */
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
      const queryVector = phrase.vectors[0] ?? [];
      const scored = pictures.map((path, at) => ({
        path,
        name: basename(path),
        score: (images.vectors[at] || []).reduce(
          (sum, value, dim) => sum + value * (queryVector[dim] ?? 0),
          0,
        ),
      }));
      scored.sort((a, b) => b.score - a.score);
      setRankedPictures(scored);
      setVectorModel(images.model ?? model);
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
    <div className={"pg-work pg-embed" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        {/* The action, and the way to the settings. The hero card above names
            the model and its state. */}
        <StageHeader
          title={pictureMode ? "Search pictures by meaning" : "Search lines by meaning"}
          configOpen={configOpen}
          onToggleConfig={toggleConfig}
        />
        {/* The MODE switch, and it is drawn only where the model has a vision
            tower to serve the second half (`entry.acceptsPaths`). A prose
            encoder sees no tab at all rather than a disabled one: there is
            nothing the reader could do about it, and a control that is always
            grey is a question the page keeps asking and answering itself. */}
        {ranksPictures && (
          <RailChips
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
              // other's heading — a stale list read as the new one is the same
              // failure `ResultSlot` exists to prevent on a re-search.
              setError(null);
            }}
          />
        )}
        {/* The retrieval KIND, drawn only where this model has a prompt scheme
            to apply — and never in the pictures mode, where there is no text
            corpus for it to be about. A dual encoder reports no scheme, so the
            SigLIP rows never show this. */}
        {scheme && !pictureMode && (
          <RailChips options={KINDS} active={kind} onPick={setKind} />
        )}
        <div className="pg-composer">
          <input
            type="text"
            value={query}
            placeholder="What are you looking for?"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void (pictureMode ? runPictures() : run());
            }}
          />
          {/* Same stack as the other composers — Clear at the top, Search at
              the foot — so Clear sits in one place across the playground and
              never appears BESIDE the input, stealing its width. */}
          <div className="pg-composer-side">
            {(pictureMode ? rankedPictures : ranked) && !busy && (
              <Button
                variant="ghost"
                size="sm"
                className="mr-2 mb-auto flex-none"
                title="Clear the results"
                onClick={() => (pictureMode ? setRankedPictures(null) : setRanked(null))}
              >
                Clear
              </Button>
            )}
            <Button
              className="flex-none"
              disabled={
                busy
                || !query.trim()
                || (pictureMode ? !pictures.length : !lines.trim())
              }
              title="Enter to run"
              onClick={() => void (pictureMode ? runPictures() : run())}
            >
              {busy ? "Searching…" : "Search"} <Kbd>⏎</Kbd>
            </Button>
          </div>
        </div>

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        {pictureMode ? (
          <RailField
            label="Pictures to search"
            hint={`Files already on this disk, up to ${MAX_PICTURES}.`}
          >
            <div className="pg-embed-pictures">
              {pictures.map((path) => (
                <div key={path} className="pg-embed-picture-row">
                  <span className="pg-embed-picture-name" title={path}>
                    {basename(path)}
                  </span>
                  <Button
                    variant="ghost"
                    size="xs"
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
                variant="outline"
                size="sm"
                className="self-start"
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
            halves of the scenario and runs it — see `run`'s arguments. */}
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

        {status && <p className="m-0 text-xs text-muted-foreground">{status}</p>}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {pictureMode ? (
          rankedPictures && !busy ? (
            <div className="pg-answer-block">
              <p className="pg-answer-label">
                Ranked by meaning
                {vectorModel && (
                  <span className="pg-answer-provenance" title={
                    `These scores were computed by ${vectorModel}. Vectors from two `
                    + `models are not comparable, even when they are the same size.`}
                  >
                    {vectorModel}
                  </span>
                )}
              </p>
              <ol className="pg-embed-results pg-embed-thumbs">
                {rankedPictures.map((row) => (
                  <li
                    key={row.path}
                    className="pg-embed-row"
                    title={`${row.path} — similarity ${row.score.toFixed(3)}`}
                  >
                    <span
                      className="pg-embed-bar"
                      style={{ width: `${Math.max(0, (row.score / bestPicture) * 100)}%` }}
                      aria-hidden="true"
                    />
                    {/* Through `/api/fs/raw`, the one door every local file in
                        this app goes through — the shell has no other way to
                        read a picture off the user's own disk. */}
                    <img className="pg-embed-thumb" src={rawUrl(row.path)} alt={row.name} />
                    <span className="pg-embed-text">{row.name}</span>
                    <span className="pg-embed-score">{row.score.toFixed(2)}</span>
                  </li>
                ))}
              </ol>
            </div>
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
          <div className="pg-answer-block">
            <p className="pg-answer-label">
              Ranked by meaning
              {vectorModel && (
                <span className="pg-answer-provenance" title={
                  `These scores were computed by ${vectorModel}. Vectors from two `
                  + `models are not comparable, even when they are the same size.`}
                >
                  {vectorModel}
                </span>
              )}
            </p>
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
