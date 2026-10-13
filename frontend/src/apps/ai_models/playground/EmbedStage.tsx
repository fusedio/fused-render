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
import { pickFiles, rawUrl, thumbUrl, type AiCatalogModel } from "@platform/lib/api";
import {
  blamedPath,
  indexProgress,
  plainError,
  planBatches,
  scoreAgainst,
  type IndexEntry,
} from "./embedIndex";
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
// The route's own ceiling per request is 64 (`embed_common.MAX_ITEMS`); the
// index sends small batches, so the cap here is about what a demo list can
// hold and still be read, not about one request. 64 matches the API's number
// so a page author trying the Playground meets the same limit they will in
// code.
const MAX_PICTURES = 64;

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
  // Mirrored for the same reason as `picturesRef` below: a text drop counts
  // its room AFTER reading the files, by which time the reader may have typed
  // or another drop may have landed.
  const linesRef = useRef(lines);
  linesRef.current = lines;
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
  // Adds can overlap — a dialog open while a drop uploads — so "Adding…" is
  // held by a COUNT of operations in flight, not the last one to finish.
  const activeAdds = useRef(0);
  // Slots claimed by uploads still in flight, so two overlapping Finder
  // drops cannot both read "room for three" off the list and copy six.
  const reservedSlots = useRef(0);
  const beginAdd = () => {
    activeAdds.current += 1;
    setAttaching(true);
  };
  const endAdd = () => {
    activeAdds.current = Math.max(0, activeAdds.current - 1);
    setAttaching(activeAdds.current > 0);
  };
  // The INDEX: one vector per file, built as files land (see embedIndex.ts).
  // A ref because the queue runner mutates it between renders; `indexTick`
  // is bumped after every change so the rows and the count re-render.
  const indexRef = useRef<Map<string, IndexEntry>>(new Map());
  const [, setIndexTick] = useState(0);
  const bumpIndex = () => setIndexTick((t) => t + 1);
  const pumping = useRef(false);
  // Set when the runner stopped on a run-level error (see `pumpIndex`), so
  // the finally-block does not restart it into the same failure; cleared by
  // the next add or Search, which are the reader acting.
  const halted = useRef(false);
  // Whether the reader is in Files mode right now, for the runner's
  // finally-block: an abort from a mode switch that was undone before the
  // aborted runner finished unwinding must still restart it.
  const filesMode = useRef(false);
  const progress = indexProgress(pictures, indexRef.current);

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
  const indexAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => {
    filesMode.current = false;
    abortRef.current?.abort();
    indexAbortRef.current?.abort();
  }, []);

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
    if (merged.next.length !== before.length) {
      setRankedPictures(null);
      // Every new file starts its journey now, not at Search time.
      for (const path of merged.next) {
        const entry = indexRef.current.get(path);
        // New, or added back after a failure: adding again IS the retry.
        if (!entry || entry.state === "failed") indexRef.current.set(path, { state: "pending" });
      }
      bumpIndex();
      // Only in Files mode: an upload that finishes after the reader switched
      // to Lines must not restart the runner that switch stopped. The rows
      // wait as pending; switching back to Files picks them up.
      if (filesMode.current) void pumpIndex();
    }
  };

  /** Embed whatever is pending, one small batch at a time, until nothing is.
   *
   *  One runner at a time (`pumping`): a drop landing mid-run is picked up by
   *  the loop's next `planBatches` rather than starting a second runner that
   *  would race it for the same files. The status line names the file being
   *  read, because the slow case — a 4K clip, thirty seconds on its own — is
   *  exactly the one the reader wants an explanation for; the model load, when
   *  that is the wait, speaks through `withModelReady`'s own status.
   *
   *  A batch that fails is pinned on the file the error names
   *  (`blamedPath`): that row is marked failed, the rest go back to pending
   *  and are sent again without it. An error naming no file (the model's slot
   *  was taken, the runner died) fails the whole batch with that sentence —
   *  there is nothing to retry until the reader acts. */
  const pumpIndex = async () => {
    if (pumping.current) return;
    pumping.current = true;
    halted.current = false;
    const controller = new AbortController();
    indexAbortRef.current = controller;
    try {
      for (;;) {
        const index = indexRef.current;
        const batches = planBatches(picturesRef.current, index);
        if (!batches.length || controller.signal.aborted) break;
        const batch = batches[0];
        for (const path of batch) index.set(path, { state: "indexing" });
        bumpIndex();
        const { done: finished, failed, total } = indexProgress(picturesRef.current, index);
        // "N of M" counts every file that has been THROUGH the index, failed
        // ones included — otherwise two unreadable files stall the count two
        // short of the total for the rest of the run.
        const done = finished + failed;
        const first = batch[0];
        const kind = mediaKindOf(first);
        setStatus(
          `Indexing ${done + 1}${batch.length > 1 ? `–${done + batch.length}` : ""} of ${total} · ` +
            `${displayName(first)}${batch.length > 1 ? ` +${batch.length - 1}` : ""}` +
            `${kind === "picture" ? "" : ` (${kind === "audio" ? "sound" : kind})`}…`,
        );
        try {
          const result = await withModelReady(() => embedPaths(model, batch, controller.signal), {
            signal: controller.signal,
            downloaded,
            onStatus: (text) => setStatus(text ?? `Indexing ${done + 1} of ${total}…`),
          });
          const made = result.response?.modelId ?? model;
          const listed = new Set(picturesRef.current);
          batch.forEach((path, at) => {
            // A file Removed while its batch was in flight stays removed —
            // writing it back would resurrect a row the reader just deleted.
            if (!listed.has(path)) return;
            const vector = result.embeddings[at];
            index.set(path, vector
              ? { state: "done", vector, model: made }
              : { state: "failed", error: "the model returned no vector for this file" });
          });
          setVectorModel(made);
        } catch (e) {
          if ((e as Error).name === "AbortError" || controller.signal.aborted) {
            // Leaving the stage: nothing is wrong with these files, and a
            // row stuck on "indexing…" would block every later Search.
            for (const path of batch) index.set(path, { state: "pending" });
            break;
          }
          const message = plainError((e as Error).message);
          const blamed = blamedPath(message, batch);
          if (blamed !== null) {
            // The error is about ONE file: that row fails, the others go
            // round again without it — and the sentence is shown, because a
            // ✗ in the settings panel is not where the reader is looking.
            const listed = new Set(picturesRef.current);
            for (const path of batch) {
              // Removed mid-batch stays removed — see the success path.
              if (!listed.has(path)) continue;
              index.set(path, path === blamed ? { state: "failed", error: message } : { state: "pending" });
            }
            setError(message);
          } else {
            // The error is about the RUN — the model lost its slot, the
            // runner died, the load was cancelled. Nothing is wrong with the
            // files, so they go back to pending and the runner STOPS: the
            // next add or Search starts it again, and the error line says
            // what happened meanwhile.
            for (const path of batch) index.set(path, { state: "pending" });
            setError(message);
            halted.current = true;
            bumpIndex();
            break;
          }
        }
        bumpIndex();
      }
    } finally {
      pumping.current = false;
      indexAbortRef.current = null;
      setStatus(null);
      // A file dropped in while the last batch was in flight is still pending:
      // one more pass picks it up — unless this runner was aborted (the stage
      // is going away, or Lines mode took over and has not handed back) or
      // stopped on a run-level error (see above; `halted` is what Search
      // clears when it tries again).
      if ((!controller.signal.aborted || filesMode.current) && !halted.current
          && planBatches(picturesRef.current, indexRef.current).length) {
        void pumpIndex();
      }
    }
  };

  /** Resolves once nothing is pending or indexing — what Search waits on —
   *  or as soon as `signal` aborts, so an unmount mid-wait leaves no timer
   *  behind. Restarts the runner if it stopped on a run-level error: a
   *  Search is the reader trying again. */
  const whenIndexed = (signal: AbortSignal) =>
    new Promise<void>((resolve) => {
      if (!pumping.current && planBatches(picturesRef.current, indexRef.current).length) void pumpIndex();
      const check = () => {
        // Three ways out: the index is idle, the search was abandoned, or the
        // runner STOPPED on a run-level error (`halted`) — files still
        // pending but nothing working on them. The last must resolve too,
        // or Search waits forever on a runner that has already given up,
        // with `busy` holding every other control shut.
        const idle = !indexProgress(picturesRef.current, indexRef.current).busy;
        if (signal.aborted || idle || (!pumping.current && halted.current)) resolve();
        else window.setTimeout(check, 150);
      };
      check();
    });

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
    beginAdd();
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
      endAdd();
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
        const current = linesRef.current;
        const room = Math.max(0, MAX_LINES - linesFromText(current).length);
        const fits = added.slice(0, room);
        if (fits.length) {
          const next = (current.trimEnd() ? current.trimEnd() + "\n" : "") + fits.join("\n");
          linesRef.current = next;
          setLines(next);
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
    beginAdd();
    const landed = [...paths];
    let refused: string | null = null;
    // The cap is applied BEFORE any upload, not after in `mergePaths`: a
    // Finder drop of forty videos with two slots left must copy two files
    // into the cache, not forty that then sit there unlisted. Each upload
    // claims its slot up front (`reservedSlots`), so a second drop landing
    // mid-upload sees the slots the first one is still filling.
    let over = 0;
    let claimed = 0;
    try {
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
        // `reservedSlots` already holds this drop's own uploads, so only the
        // PATH entries (dropped from the Explorer, never uploaded, never
        // reserved) are counted beside it — counting `landed` too charged
        // each upload twice and halved the room.
        const room = MAX_PICTURES - picturesRef.current.length - reservedSlots.current - paths.length;
        if (room <= 0) {
          over += 1;
          continue;
        }
        reservedSlots.current += 1;
        claimed += 1;
        landed.push(await saveDropped(file));
      }
      if (over) refused ??= `Up to ${MAX_PICTURES} files — ${over} not added.`;
      if (landed.length || refused) takePaths(landed, refused);
    } catch (err) {
      // The files already copied into the cache still land — a failure on
      // the third of five must not make the reader drop the first two again.
      // The upload error outranks an earlier refusal: it is the newer news.
      takePaths(landed, (err as Error).message);
    } finally {
      reservedSlots.current = Math.max(0, reservedSlots.current - claimed);
      endAdd();
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
    let afterNote: string | null = null;
    try {
      // The files are embedded as they land (`pumpIndex`); a search that
      // arrives first waits for the tail of that rather than re-sending
      // anything. Then the PHRASE is one small request, and the ranking is
      // arithmetic over vectors already here.
      if (indexProgress(picturesRef.current, indexRef.current).busy) {
        setStatus("Finishing the index first…");
        await whenIndexed(controller.signal);
        if (controller.signal.aborted) return;
        if (halted.current) {
          // The runner gave up (its sentence is on the error line); rank
          // what IS indexed rather than nothing, and say what is missing —
          // kept past the `finally` below, which otherwise clears status.
          const left = indexProgress(picturesRef.current, indexRef.current);
          afterNote = `${left.total - left.done - left.failed} not indexed — showing the rest.`;
        }
      }
      const phrase = await withModelReady(() => embedTexts(model, [asked]), {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
      const queryVector = phrase.embeddings[0] ?? [];
      const scored = scoreAgainst(queryVector, picturesRef.current, indexRef.current).map((row) => ({
        path: row.path,
        name: displayName(row.path),
        score: row.score,
      }));
      if (!scored.length) {
        const left = indexProgress(picturesRef.current, indexRef.current);
        afterNote = null;
        // The runner gave up with files still waiting: its sentence is
        // already on the error line and is the real reason — those rows
        // are pending, not failed, so the cog has nothing to show.
        if (halted.current && left.total - left.done - left.failed > 0) return;
        setError("None of the files could be read — open the settings cog to see why.");
        return;
      }
      setRankedPictures(scored);
      setVectorModel(phrase.response?.modelId ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(afterNote);
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
              filesMode.current = value === "pictures";
              // The index runner belongs to Files mode. Left running under
              // Lines it would race a lines Search for the model and for the
              // status/error line; aborted, its in-flight rows go back to
              // pending, and coming back picks them up where they stopped.
              if (value === "pictures") {
                if (planBatches(picturesRef.current, indexRef.current).length) void pumpIndex();
              } else {
                indexAbortRef.current?.abort();
              }
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
              {busy
                ? progress.busy ? "Indexing…" : "Searching…"
                : "Search"} <kbd className="pg-kbd">⏎</kbd>
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
                {!pictures.length
                  ? ranksMedia ? "Pictures, sound files and videos" : "Pictures"
                  : progress.busy
                    ? `Indexing ${progress.done + progress.failed + progress.indexing} of ${progress.total}…`
                    : `${progress.done} ${progress.done === 1 ? mediaNoun : `${mediaNoun}s`} ready` +
                      (progress.failed ? ` · ${progress.failed} could not be read` : "")}
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
                  {(() => {
                    const entry = indexRef.current.get(path);
                    if (!entry || entry.state === "pending") return <span className="pg-embed-state">queued</span>;
                    if (entry.state === "indexing") return <span className="pg-embed-state">indexing…</span>;
                    if (entry.state === "failed") {
                      // One mark, like the ✓: the settings panel is narrow and
                      // a sentence here squeezed the filename to nothing. The
                      // reason is the tooltip, and the error line under the
                      // composer says it in full when it happens.
                      return <span className="pg-embed-state is-failed" title={`Could not read: ${entry.error}`}>✗</span>;
                    }
                    return <span className="pg-embed-state is-done" title="Indexed">✓</span>;
                  })()}
                  <button
                    type="button"
                    className="pg-ghost-btn"
                    title={`Remove this ${mediaNoun}`}
                    onClick={() => {
                      const next = picturesRef.current.filter((p) => p !== path);
                      picturesRef.current = next;
                      setPictures(next);
                      indexRef.current.delete(path);
                      bumpIndex();
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
                      // `thumbUrl`, not `rawUrl`: a browser cannot decode an
                      // iPhone's HEIC, and a photo library is mostly HEIC —
                      // the server makes the JPEG.
                      <img className="pg-embed-thumb" src={thumbUrl(row.path)} alt={row.name} loading="lazy" />
                    )}
                    <span className="pg-embed-text">
                      {kindTag(mediaKindOf(row.path)) && (
                        <span className="pg-embed-kind">{kindTag(mediaKindOf(row.path))}</span>
                      )}
                      {row.name}
                    </span>
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
                  ? progress.busy
                    ? `Indexing ${progress.done + progress.failed + progress.indexing} of ${progress.total} — type what you are looking for meanwhile.`
                    : `The ${mediaNoun}s come back here, ordered by how close they are to the phrase.`
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
