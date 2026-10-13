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
//   tower, which a prose encoder does not have. Where `entry.acceptsAudio` /
//   `entry.acceptsVideo` are true as well (EmbeddingGemma 2), the same mode
//   takes sound files and videos through the same picker — the server sorts a
//   path by extension, so the only difference the reader sees is the file
//   filter and the noun.
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
import { pickFiles, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import {
  acceptedTypes,
  basename,
  displayName,
  linesFromText,
  mediaKindOf,
  mergePaths,
  pathsFromDrop,
  refusal,
  saveDropped,
  type MediaKind,
} from "./embedAttach";
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
// The extension lists themselves live in embedAttach.ts, beside the three
// ways a file gets in (dialog, Explorer drag, Finder drop) and the one check
// all three share.

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

/** A small word for the row — what kind of thing this file is, so a list that
 *  mixes a photo, a voice memo and a clip reads at a glance. Pictures get no
 *  tag: they are the default, and their thumbnail already says so. */
function kindTag(kind: MediaKind): string | null {
  return kind === "audio" ? "sound" : kind === "video" ? "video" : null;
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
  const ranksAudio = ranksPictures && entry.acceptsAudio === true;
  const ranksVideo = ranksPictures && entry.acceptsVideo === true;
  // The noun the mode is drawn with: "Pictures" on a SigLIP, "Files" on a
  // model that also takes sound and video — one picker, one list, one ranking.
  const ranksMedia = ranksAudio || ranksVideo;
  const mediaNoun = ranksMedia ? "file" : "picture";
  const mediaTypes = acceptedTypes({ audio: ranksAudio, video: ranksVideo });
  const [dragging, setDragging] = useState(false);
  // Drag enter/leave come in pairs per CHILD crossed, and WebKit leaves
  // `relatedTarget` null on drag events, so "did we leave the stage" is a
  // depth count rather than a target check: zero means gone.
  const dragDepth = useRef(0);
  const scheme = entry.promptScheme ?? null;
  const [wantPictures, setWantPictures] = useState(false);
  const pictureMode = ranksPictures && wantPictures;
  const [kind, setKind] = useState<"query" | "document">("document");
  const [pictures, setPictures] = useState<string[]>([]);
  // The list as last handed to React — mirrored so a merge that runs AFTER an
  // await (the dialog, an upload) folds into whatever landed meanwhile rather
  // than into the render this handler closed over.
  const picturesRef = useRef(pictures);
  picturesRef.current = pictures;
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
      // `response.modelId` — what the SERVER says it used, not what was asked for. A
      // bare call takes the capability's default, so the request's own `model` is
      // not always the answer.
      setVectorModel(result.response?.modelId ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      abortRef.current = null;
    }
  };

  /** Fold paths into the list — the one door every way in goes through.
   *
   *  Each path is checked against what THIS model reads (`refusal`), because a
   *  file the route would refuse by name must not sit in the list looking
   *  rankable; the first refused one is the error line, the rest still land.
   *  Deduplicated and capped in `mergePaths`, and the overflow is said once
   *  rather than the tail of a big selection vanishing. A new file invalidates
   *  the ranking rather than being appended to it unscored — a list where one
   *  row has no bar reads as a broken render. */
  const takePaths = (incoming: string[], prior: string | null = null) => {
    const ok: string[] = [];
    let why: string | null = prior;
    for (const path of incoming) {
      const no = refusal(path, mediaTypes);
      if (no) why ??= no;
      else ok.push(path);
    }
    // Merged against the mirror, not inside an updater (an updater runs at
    // React's convenience, so the overflow count would be read before it
    // ran) and not against the render's `pictures` (callers reach here after
    // an await, by which time another drop may have landed).
    const before = picturesRef.current;
    const merged = mergePaths(before, ok, MAX_PICTURES);
    picturesRef.current = merged.next;
    setPictures(merged.next);
    if (merged.over) why ??= `Up to ${MAX_PICTURES} files — ${merged.over} not added.`;
    setError(why);
    if (merged.next.length !== before.length) setRankedPictures(null);
  };

  /** Point the ranking at files ALREADY on this disk — no copy, no upload,
   *  several per dialog.
   *
   *  `<input type=file>` cannot do this: a browser hands over bytes and strips
   *  the path on purpose, and the route takes a PATH because the worker is a
   *  separate process that opens the file itself. So the way to one is the OS
   *  dialog raised in the server process (`pickFiles`), exactly as `ImageStage`
   *  does it, with multi-select on. */
  const addFiles = async () => {
    setError(null);
    setAttaching(true);
    try {
      const paths = await pickFiles({
        title: ranksMedia
          ? "Choose pictures, sound files or videos to rank"
          : "Choose pictures to rank",
        types: mediaTypes,
      });
      // A cancel is an answer: nothing changes and nothing is said about it.
      if (paths === null) return;
      takePaths(paths);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAttaching(false);
    }
  };

  /** A drop, anywhere on the stage. Two payloads: PATHS (the app's own
   *  Explorer, a Linux file manager) go straight in; BYTES (Finder) are
   *  written to the app's cache first and the copy goes in — the transcribe
   *  stage's rule for the same reason. In Lines mode a dropped text file
   *  becomes lines of the corpus; in Files mode text files are refused with
   *  the sentence that says where they go. */
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (busy) return;
    const paths = pathsFromDrop(e.dataTransfer);
    const files = paths.length ? [] : Array.from(e.dataTransfer.files ?? []);
    if (!paths.length && !files.length) return;
    setError(null);
    if (!pictureMode) {
      // Lines mode: text files only — read in the browser when they came as
      // bytes, through `/api/fs/raw` when they came as a path (the Explorer).
      // No upload either way: the corpus is text in a textarea.
      const textFiles = files.filter((f) => mediaKindOf(f.name) === "text");
      const textPaths = paths.filter((p) => mediaKindOf(p) === "text");
      if (!textFiles.length && !textPaths.length) {
        setError(
          ranksPictures
            ? "Drop text files here in Lines mode — switch to Files for pictures, sound and video."
            : "Drop a text file here — one line per entry.",
        );
        return;
      }
      try {
        const added: string[] = [];
        for (const file of textFiles) added.push(...linesFromText(await file.text()));
        for (const path of textPaths) {
          const res = await fetch(rawUrl(path), { headers: { "X-Fused": "1" } });
          if (!res.ok) throw new Error(`could not read ${basename(path)} (${res.status})`);
          added.push(...linesFromText(await res.text()));
        }
        // Appended to what is typed, verbatim — the reader's own blank lines
        // and spacing are theirs — and the cap is SAID, not applied silently:
        // what fits lands, the rest is counted in the error line, the same
        // "At most N lines" the run path refuses with.
        const have = linesFromText(lines).length;
        const room = Math.max(0, MAX_LINES - have);
        const fits = added.slice(0, room);
        if (fits.length) {
          setLines((current) => (current.trimEnd() ? current.trimEnd() + "\n" : "") + fits.join("\n"));
          setRanked(null);
        }
        if (added.length > room) {
          setError(`At most ${MAX_LINES} lines — ${added.length - room} not added.`);
        }
      } catch (err) {
        setError((err as Error).message);
      }
      return;
    }
    setAttaching(true);
    try {
      const landed = [...paths];
      let refused: string | null = null;
      for (const file of files) {
        // Refused BEFORE the upload: a PDF should not be copied into the
        // cache only to be refused by name afterwards. The first refusal is
        // carried into `takePaths`, which owns the error line, so a mixed
        // drop (a.pdf + b.png) lands the picture AND says why the other did
        // not.
        const no = refusal(file.name, mediaTypes);
        if (no) {
          refused ??= no;
          continue;
        }
        landed.push(await saveDropped(file));
      }
      if (landed.length || refused) takePaths(landed, refused);
    } catch (err) {
      setError((err as Error).message);
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
      const queryVector = phrase.embeddings[0] ?? [];
      const scored = pictures.map((path, at) => ({
        path,
        name: displayName(path),
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
    <div
      className={"pg-work pg-embed" + (configOpen ? " has-config" : "") + (dragging ? " dragging" : "")}
      onDragEnter={(e) => {
        e.preventDefault();
        dragDepth.current += 1;
        if (!busy) setDragging(true);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={() => {
        // Leaving a CHILD fires this too, paired with the enter that preceded
        // it — see `dragDepth`. Only the leave that brings the depth back to
        // zero is a leave of the stage.
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={(e) => void onDrop(e)}
    >
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        {/* The action, and the way to the settings. The hero card above names
            the model and its state. */}
        <StageHeader
          title={pictureMode ? `Search ${mediaNoun}s by meaning` : "Search lines by meaning"}
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
              { value: "pictures", label: ranksMedia ? "Files" : "Pictures",
                title: ranksMedia
                  ? "Rank pictures, sound files and videos on this disk against "
                    + "the phrase — one vector space, through this model's vision "
                    + "and audio towers"
                  : "Rank pictures on this disk against the phrase — the same "
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
              <button
                type="button"
                className="pg-ghost-btn pg-clear"
                title="Clear the results"
                onClick={() => (pictureMode ? setRankedPictures(null) : setRanked(null))}
              >
                Clear
              </button>
            )}
            <button
              type="button"
              className="btn btn-primary pg-send"
              disabled={
                busy
                || !query.trim()
                || (pictureMode ? !pictures.length : !lines.trim())
              }
              title="Enter to run"
              onClick={() => void (pictureMode ? runPictures() : run())}
            >
              {busy ? "Searching…" : "Search"} <kbd className="pg-kbd">⏎</kbd>
            </button>
          </div>
        </div>
        {/* The way IN, in the open — not behind the cog. The list of what is in
            (and Remove) stays behind the cog, where the Lines corpus also
            lives; this row is the door and the count. */}
        {pictureMode && (
          <div className={"pg-dropzone pg-embed-attach" + (dragging ? " dragging" : "") + (busy ? " busy" : "")}>
            <button
              type="button"
              className="pg-ghost-btn"
              disabled={attaching || busy || pictures.length >= MAX_PICTURES}
              onClick={() => void addFiles()}
            >
              {attaching ? "Adding…" : `Add ${mediaNoun}s…`}
            </button>
            <div className="pg-drop-copy">
              <p className="pg-drop-title">
                {pictures.length
                  ? `${pictures.length} ${pictures.length === 1 ? mediaNoun : `${mediaNoun}s`} to search`
                  : ranksMedia
                    ? "Pictures, sound files and videos"
                    : "Pictures"}
              </p>
              <p className="pg-drop-hint">
                Pick several at once, or drop them here from Finder or the Explorer.
                {pictures.length > 0 && " Open the settings cog to remove one."}
              </p>
            </div>
          </div>
        )}

      <ConfigPanel open={configOpen} animated={configTouched.current}>
        {pictureMode ? (
          <RailField
            label={ranksMedia ? "Files to search" : "Pictures to search"}
            hint={`Files already on this disk, up to ${MAX_PICTURES}.`}
          >
            <div className="pg-embed-pictures">
              {pictures.map((path) => (
                <div key={path} className="pg-embed-picture-row">
                  {kindTag(mediaKindOf(path)) && (
                    <span className="pg-embed-kind">{kindTag(mediaKindOf(path))}</span>
                  )}
                  <span className="pg-embed-picture-name" title={path}>
                    {displayName(path)}
                  </span>
                  <button
                    type="button"
                    className="pg-ghost-btn"
                    title={`Remove this ${mediaNoun}`}
                    onClick={() => {
                      setPictures((current) => current.filter((p) => p !== path));
                      setRankedPictures(null);
                    }}
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="pg-ghost-btn"
                disabled={attaching || pictures.length >= MAX_PICTURES}
                onClick={() => void addFiles()}
              >
                {attaching ? "Adding…" : `Add ${mediaNoun}s…`}
              </button>
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

        {status && <p className="pg-status">{status}</p>}
        {error && <p className="pg-error">{error}</p>}
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
                    {mediaKindOf(row.path) === "video" ? (
                      <video className="pg-embed-thumb" src={rawUrl(row.path)} muted playsInline preload="metadata" />
                    ) : mediaKindOf(row.path) === "audio" ? (
                      <span className="pg-embed-thumb pg-embed-thumb-audio" aria-label="sound file">♪</span>
                    ) : (
                      <img className="pg-embed-thumb" src={rawUrl(row.path)} alt={row.name} />
                    )}
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
                  ? `The ${mediaNoun}s come back here, ordered by how close they are to the phrase.`
                  : `Add a ${mediaNoun} or two above, or drop them in, then type what you are looking for.`
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
