// Starter ideas under the /apps (and Home) composer: an icon + short label on
// the chip, and the verbose brief that actually lands in the box on click —
// detailed enough that Claude builds the right thing first pass.
//
// Its own module rather than a const in `HomeHero.tsx` because the pool is now
// the bulk of that file and it carries an INVARIANT worth testing on its own
// (starterPrompts.test.ts): every capability the Playground can annotate the
// composer with has at least four starters of its own, so a chip row filtered
// down to one model's capability is never thin or empty.
//
// `capability` is the field that makes that filtering possible: the Hub
// capability tag (the same vocabulary as `AiCatalogModel.capability` and
// `AppAnnotation.capability`) when the brief explicitly asks the session to
// build on a LOCAL model, and null for an ordinary app that needs no AI at
// all. The composer shows the whole mixed pool until a model is attached as a
// chip, then only that capability's briefs.
import { type ReactNode } from "react";

export interface StarterPrompt {
  label: string;
  prompt: string;
  glyph: ReactNode;
  // Hub capability tag, or null for a starter that asks for no AI.
  capability: string | null;
}

// The five capabilities a Playground annotation can carry — the same strings
// `buildAppAnnotation` puts in the chip. Spelled out here rather than imported
// from `@apps/ai_models` because an app may only import platform + itself; the
// test asserts the pool uses exactly these, so a typo cannot quietly create a
// sixth bucket nothing ever filters to.
export const STARTER_CAPABILITIES = [
  "text-generation",
  "text-to-image",
  "text-to-video",
  "automatic-speech-recognition",
  "embeddings",
] as const;

// One wrapper for every chip glyph: 13px at 2px stroke, the composer's own
// weight (MenuIcons is tuned 1.5px for menu rows and reads thin beside these).
const S = (paths: ReactNode): ReactNode => (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {paths}
  </svg>
);

// Every AI starter closes on the same two facts, because they are what the
// session cannot guess: the model runs locally (no key, no network) and the
// page reaches it through `fused.ai.*`. The seed prose for an ATTACHED model
// is far richer (appSeed.ts) — these briefs are what a user clicks when they
// picked the idea first and the model second.
const LOCAL =
  "Use the local AI models on this machine through the page's fused.ai API — no cloud keys, no network.";

export const STARTER_PROMPTS: StarterPrompt[] = [
  // -- No AI -----------------------------------------------------------------
  {
    label: "Habit tracker",
    capability: null,
    glyph: S(<path d="M20 6L9 17l-5-5" />),
    prompt:
      "A habit tracker. Let me define habits with a name and a target cadence " +
      "(daily or specific weekdays), check them off for today, and edit or delete them. " +
      "Show the current streak per habit and a weekly heatmap of completions. " +
      "Persist everything locally so my history survives restarts.",
  },
  {
    label: "Markdown notes",
    capability: null,
    glyph: S(<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />),
    prompt:
      "A markdown notes app. A sidebar lists my notes sorted by last edited; " +
      "I can create, rename, and delete notes, and edit them with a live markdown preview. " +
      "Include full-text search across all notes with matching snippets highlighted. " +
      "Store notes as plain .md files in the app folder so they stay portable.",
  },
  {
    label: "CSV dashboard",
    capability: null,
    glyph: S(<path d="M3 3v18h18M8 17V9M13 17V5M18 17v-6" />),
    prompt:
      "A CSV dashboard. Let me drop or pick a CSV file, then show a sortable, filterable " +
      "table of its rows plus summary stats per numeric column (min, max, mean, nulls). " +
      "Let me pick columns to chart as a bar, line, or scatter plot. " +
      "Handle large-ish files gracefully and remember the last file I opened.",
  },
  {
    label: "Mini game",
    capability: null,
    glyph: S(
      <path d="M6 12h4M8 10v4M15 11h.01M18 13h.01M17.3 5H6.7a4.7 4.7 0 0 0-4.6 5.6l1 5A3 3 0 0 0 8 17.4l.6-1.4h6.8l.6 1.4a3 3 0 0 0 4.9-1.8l1-5A4.7 4.7 0 0 0 17.3 5z" />,
    ),
    prompt:
      "A 2048-style sliding tile game. Arrow keys (and touch swipes) slide and merge " +
      "tiles on a 4x4 grid, with smooth animations and a score counter. " +
      "Detect game over and win states with a restart button, " +
      "and keep the best score locally so it survives restarts.",
  },
  {
    label: "Finance calculator",
    capability: null,
    glyph: S(<path d="M22 12h-4l-3 9L9 3l-3 9H2" />),
    prompt:
      "A compound-interest and loan calculator. Inputs for principal, rate, term, and " +
      "monthly contribution or payment; show the resulting balance or amortization " +
      "schedule as both a table and a line chart. " +
      "Update results live as inputs change and format all amounts as currency.",
  },
  {
    label: "Pomodoro timer",
    capability: null,
    glyph: S(
      <>
        <circle cx="12" cy="13" r="8" />
        <path d="M12 9v4l2.5 2.5M9 2h6" />
      </>,
    ),
    prompt:
      "A pomodoro focus timer. Configurable work/short-break/long-break durations, " +
      "a large countdown with start/pause/reset, and an automatic cycle through " +
      "sessions with a chime between them. " +
      "Log completed pomodoros per day and show a simple daily history.",
  },
  {
    label: "Expense splitter",
    capability: null,
    glyph: S(
      <>
        <circle cx="9" cy="8" r="3" />
        <path d="M3 20a6 6 0 0 1 12 0M16.5 6.5a3 3 0 0 1 0 5.8M19 20a6 6 0 0 0-3-5.2" />
      </>,
    ),
    prompt:
      "A shared-expenses splitter. Let me add people to a group, log expenses with " +
      "who paid and who shares each one (equal or custom shares), and see each " +
      "person's running balance. " +
      "Compute the minimum set of transfers that settles the group and keep everything locally.",
  },
  {
    label: "Reading board",
    capability: null,
    glyph: S(<path d="M4 19.5V6a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 1.5zM9 8h6M9 12h4" />),
    prompt:
      "A reading list as a kanban board. Columns for Want to read / Reading / Finished, " +
      "with drag-and-drop between them; each card holds a title, author, link, tags, " +
      "and a 1-5 rating once finished. " +
      "Support search and tag filters, and store the board as JSON in the app folder.",
  },

  // -- Text generation -------------------------------------------------------
  {
    label: "Local chat",
    capability: "text-generation",
    glyph: S(<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />),
    prompt:
      "A private chat app that talks to a local language model. Stream the reply token " +
      "by token, keep the conversation history in the request so follow-ups make sense, " +
      "and let me start a new conversation or revisit an old one from a sidebar. " +
      "Show which model is answering and let me switch it. " +
      LOCAL,
  },
  {
    label: "Rewrite tool",
    capability: "text-generation",
    glyph: S(<path d="M15 4l5 5M17.5 2.5a2.1 2.1 0 0 1 3 3L8 18l-5 1 1-5L17.5 2.5z" />),
    prompt:
      "A rewriting workbench. I paste text on the left, pick a tone (shorter, plainer, " +
      "more formal, friendlier) and get the rewritten version streaming in on the right, " +
      "with a diff view showing what changed and a copy button. " +
      "Let me keep the last few rewrites so I can compare them. " +
      LOCAL,
  },
  {
    label: "Notes summarizer",
    capability: "text-generation",
    glyph: S(
      <path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3M9 12h5M9 16h3M14 3h7v7" />,
    ),
    prompt:
      "A meeting-notes summarizer. I drop in raw notes or a transcript and get back a " +
      "one-paragraph summary, the decisions made, and a checklist of action items with " +
      "owners where the text names one. " +
      "Let me edit the result and save it as markdown in the app folder. " +
      LOCAL,
  },
  {
    label: "Flashcards",
    capability: "text-generation",
    glyph: S(
      <>
        <rect x="3" y="6" width="14" height="11" rx="2" />
        <path d="M8 3h11a2 2 0 0 1 2 2v9" />
      </>,
    ),
    prompt:
      "A flashcard maker. I paste study material and a local model turns it into " +
      "question/answer cards; I can edit, delete, or regenerate any card. " +
      "Then drill them in a spaced-repetition review mode that tracks what I get wrong " +
      "and persists the deck locally. " +
      LOCAL,
  },
  {
    label: "Commit writer",
    capability: "text-generation",
    glyph: S(
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M3 12h6M15 12h6" />
      </>,
    ),
    prompt:
      "A commit-message and changelog writer. Let me pick a git repo folder, read its " +
      "staged diff (or a commit range) with Python, and have a local model draft a " +
      "conventional-commit subject plus a short body explaining the why. " +
      "For a range, group the commits into a release changelog I can copy. " +
      LOCAL,
  },

  // -- Image generation ------------------------------------------------------
  {
    label: "Icon studio",
    capability: "text-to-image",
    glyph: S(
      <>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
        <path d="M14 6.5h7M6.5 14v7" />
      </>,
    ),
    prompt:
      "An icon studio. I describe a subject and a style once, then generate a whole set " +
      "of matching icons from a list of names, each on a flat single-colour background, " +
      "with a seed I can lock so the set stays consistent. " +
      "Show them as a grid and let me regenerate or save any one to disk. " +
      LOCAL,
  },
  {
    label: "Poster maker",
    capability: "text-to-image",
    glyph: S(
      <>
        <rect x="4" y="3" width="16" height="18" rx="2" />
        <path d="M8 8h8M8 12h5" />
      </>,
    ),
    prompt:
      "A poster and cover maker. I type a title and a description of the artwork; the page " +
      "generates the image with a local model, then composes my title and subtitle over it " +
      "with font, size, and placement controls. " +
      "Let me re-roll the art without losing the text layout, and export the result as PNG. " +
      LOCAL,
  },
  {
    label: "Storyboard",
    capability: "text-to-image",
    glyph: S(
      <>
        <rect x="2.5" y="6" width="8" height="7" rx="1.5" />
        <rect x="13.5" y="6" width="8" height="7" rx="1.5" />
        <path d="M4 17h6M15 17h6" />
      </>,
    ),
    prompt:
      "A storyboard sketcher. I write a scene per row — shot description plus a caption — " +
      "and each row generates its frame with a local image model, sharing one style prompt " +
      "and seed so the boards look like one film. " +
      "Let me reorder rows, re-roll a single frame, and export the whole board as a contact sheet. " +
      LOCAL,
  },
  {
    label: "Mood board",
    capability: "text-to-image",
    glyph: S(
      <>
        <circle cx="8" cy="8" r="4" />
        <circle cx="16" cy="16" r="4" />
      </>,
    ),
    prompt:
      "A mood board generator. From one theme I get nine variations at once, each with a " +
      "slightly different style modifier, laid out as a masonry grid with the prompt and " +
      "seed shown under every tile. " +
      "Let me pin the ones I like into a keep row and save that row to a folder. " +
      LOCAL,
  },
  {
    label: "Avatar maker",
    capability: "text-to-image",
    glyph: S(
      <>
        <circle cx="12" cy="9" r="3.5" />
        <path d="M5 20a7 7 0 0 1 14 0" />
      </>,
    ),
    prompt:
      "An avatar maker. Pickers for style, hair, expression, and background colour build the " +
      "prompt for me; a local image model renders a square portrait I can re-roll with a new " +
      "seed or refine by editing the prompt directly. " +
      "Crop to a circle preview and export at 512 and 1024 px. " +
      LOCAL,
  },

  // -- Video generation ------------------------------------------------------
  // Every video brief ends by telling the session to check the catalog: this
  // capability is Apple Silicon only with no fallback, so a page that assumes
  // it is a page that is simply broken on the machine it was built for.
  {
    label: "Clip sketchpad",
    capability: "text-to-video",
    glyph: S(
      <>
        <rect x="2.5" y="6" width="13" height="12" rx="2" />
        <path d="M15.5 12l6-3.5v11l-6-3.5z" />
      </>,
    ),
    prompt:
      "A text-to-video sketchpad. I describe a shot, pick a resolution and frame count, and a " +
      "local video model renders it with a visible progress bar; the clip plays inline with " +
      "controls and its prompt and seed shown beside it. " +
      "Keep every render in a session gallery I can replay and save from. " +
      "Check the model catalog first and say plainly if this machine cannot run video. " +
      LOCAL,
  },
  {
    label: "Logo sting",
    capability: "text-to-video",
    glyph: S(
      <>
        <path d="M12 3.5l2 5.5 5.5 2-5.5 2-2 5.5-2-5.5-5.5-2 5.5-2z" />
        <path d="M19 18.5l1 2 2 1-2 1" />
      </>,
    ),
    prompt:
      "A logo-sting generator. I pick a brand colour and describe the motion (a slow reveal, " +
      "an ink bloom, a light sweep); a local video model renders a two-second clip and the page " +
      "overlays my wordmark on top of it. " +
      "Let me re-roll the motion with a new seed and export the clip. " +
      "Check the model catalog first and say plainly if this machine cannot run video. " +
      LOCAL,
  },
  {
    label: "Loop background",
    capability: "text-to-video",
    glyph: S(<path d="M4 8h11a4 4 0 0 1 0 8H8m0 0l3-3m-3 3l3 3" />),
    prompt:
      "A looping-background maker. I describe an ambient scene, the page renders a short clip " +
      "with a local video model, then previews it as a seamless loop behind sample text so I can " +
      "judge it as an actual backdrop. " +
      "Offer a few length and resolution presets and let me save the loop. " +
      "Check the model catalog first and say plainly if this machine cannot run video. " +
      LOCAL,
  },
  {
    label: "How-to short",
    capability: "text-to-video",
    glyph: S(
      <>
        <rect x="4" y="2.5" width="16" height="19" rx="2.5" />
        <path d="M10.5 9.5l4.5 2.5-4.5 2.5z" />
      </>,
    ),
    prompt:
      "A how-to short builder. I list the steps of a recipe or task; each step becomes a vertical " +
      "clip rendered by a local video model from that step's text, with the step caption burned " +
      "over it, and the page plays the steps back to back as one short. " +
      "Let me re-render a single step without touching the rest. " +
      "Check the model catalog first and say plainly if this machine cannot run video. " +
      LOCAL,
  },
  {
    label: "Shot list",
    capability: "text-to-video",
    glyph: S(<path d="M4 6h2M4 12h2M4 18h2M10 6h10M10 12h10M10 18h6" />),
    prompt:
      "A shot-list renderer. I write a table of shots — description, camera move, seconds — and " +
      "render them one at a time with a local video model, queued so the page stays usable, with " +
      "per-row status and progress. " +
      "Show the finished clips as a timeline strip I can play through in order. " +
      "Check the model catalog first and say plainly if this machine cannot run video. " +
      LOCAL,
  },

  // -- Transcription ---------------------------------------------------------
  {
    label: "Voice memos",
    capability: "automatic-speech-recognition",
    glyph: S(
      <>
        <rect x="9" y="2.5" width="6" height="11" rx="3" />
        <path d="M5 11a7 7 0 0 0 14 0M12 18.5V22" />
      </>,
    ),
    prompt:
      "A voice-memo notebook. I pick or drop an audio file and a local speech model transcribes " +
      "it with segments streaming in as they land, timestamps down the side, and the text " +
      "editable once it finishes. " +
      "Keep every memo in a list with its date, duration, and a title I can rename. " +
      LOCAL,
  },
  {
    label: "Transcript search",
    capability: "automatic-speech-recognition",
    glyph: S(
      <>
        <circle cx="11" cy="11" r="6" />
        <path d="M15.5 15.5L21 21" />
      </>,
    ),
    prompt:
      "A searchable transcript reader for podcasts and lectures. Transcribe a long audio or video " +
      "file with a local speech model, then let me search the transcript and jump the player to " +
      "any hit, with the current segment highlighted as it plays. " +
      "Cache the transcript beside the media file so reopening is instant. " +
      LOCAL,
  },
  {
    label: "Subtitle maker",
    capability: "automatic-speech-recognition",
    glyph: S(
      <>
        <rect x="3" y="5" width="18" height="14" rx="2" />
        <path d="M7 14h4M13 14h4" />
      </>,
    ),
    prompt:
      "A subtitle maker. Transcribe a video with a local speech model, show the segments as an " +
      "editable cue list beside the player, and let me fix text or nudge timings. " +
      "Export valid .srt and .vtt next to the source video, and burn a live preview of the " +
      "current cue over the player. " +
      LOCAL,
  },
  {
    label: "Voice to todo",
    capability: "automatic-speech-recognition",
    glyph: S(
      <>
        <path d="M4 7l2.5 2.5L11 5" />
        <path d="M14 6h6M14 12h6M4 15l2.5 2.5L11 13M14 18h4" />
      </>,
    ),
    prompt:
      "A spoken-capture inbox. I drop a voice note, a local speech model transcribes it, and each " +
      "sentence that sounds like a task becomes a checkbox I can accept, edit, or discard. " +
      "Keep the accepted ones as a persistent todo list with the audio timestamp each came from. " +
      LOCAL,
  },
  {
    label: "Interview notes",
    capability: "automatic-speech-recognition",
    glyph: S(
      <>
        <circle cx="8" cy="8" r="3" />
        <path d="M2.5 19a5.5 5.5 0 0 1 11 0M17 5.5a4 4 0 0 1 0 9" />
      </>,
    ),
    prompt:
      "An interview log. Transcribe a recorded conversation with a local speech model, let me " +
      "label who is speaking for each segment, and keep those labels when I reopen the file. " +
      "Let me star quotes and copy them out with their timestamp as a citation. " +
      LOCAL,
  },

  // -- Embeddings ------------------------------------------------------------
  {
    label: "Semantic search",
    capability: "embeddings",
    glyph: S(
      <>
        <circle cx="10.5" cy="10.5" r="5.5" />
        <path d="M14.5 14.5L21 21M8 10.5h5" />
      </>,
    ),
    prompt:
      "A meaning-based file search. Point it at a folder of text or markdown files, embed them in " +
      "chunks with a local embedding model, and cache the vectors on disk so a rescan is cheap. " +
      "Then I type a question in plain words and get the closest passages ranked by cosine " +
      "similarity, each with its file path and a snippet. " +
      LOCAL,
  },
  {
    label: "Related notes",
    capability: "embeddings",
    glyph: S(
      <>
        <circle cx="6" cy="12" r="2.5" />
        <circle cx="18" cy="6.5" r="2.5" />
        <circle cx="18" cy="17.5" r="2.5" />
        <path d="M8.3 11l7.4-3.3M8.3 13l7.4 3.3" />
      </>,
    ),
    prompt:
      "A related-notes finder. Embed every note in a folder with a local embedding model, then when " +
      "I open one show the five most similar notes with their similarity scores and matching lines. " +
      "Re-embed only files whose mtime changed, and draw the whole set as a simple similarity graph. " +
      LOCAL,
  },
  {
    label: "Bookmark clusters",
    capability: "embeddings",
    glyph: S(
      <>
        <circle cx="7" cy="7" r="2" />
        <circle cx="11" cy="10" r="2" />
        <circle cx="17" cy="16" r="2" />
        <circle cx="13.5" cy="18.5" r="2" />
      </>,
    ),
    prompt:
      "A bookmark clusterer. I paste a list of links with titles and notes, they get embedded with a " +
      "local model, and the page groups them into clusters by meaning with a generated name per " +
      "cluster and an outlier bucket. " +
      "Let me set how many clusters and drag a link into a different group. " +
      LOCAL,
  },
  {
    label: "Duplicate finder",
    capability: "embeddings",
    glyph: S(
      <>
        <rect x="3" y="3" width="12" height="12" rx="2" />
        <rect x="9" y="9" width="12" height="12" rx="2" />
      </>,
    ),
    prompt:
      "A near-duplicate finder for text. Embed every row of a CSV or every file in a folder with a " +
      "local embedding model, then list the pairs above a similarity threshold I control with a " +
      "slider, side by side with their differences highlighted. " +
      "Let me mark a pair as keep-both or pick a survivor, and export the decisions. " +
      LOCAL,
  },
  {
    label: "Photo search",
    capability: "embeddings",
    glyph: S(
      <>
        <rect x="3" y="4" width="18" height="15" rx="2" />
        <circle cx="9" cy="10" r="2" />
        <path d="M4 18l5-4.5 4 3.5 3-2.5 4 3.5" />
      </>,
    ),
    prompt:
      "A photo search by description. Embed the images in a folder with a local multimodal " +
      "embedding model, cache the vectors, then let me type what I remember about a picture and " +
      "get the closest matches as a thumbnail grid with scores. " +
      "Clicking a thumbnail shows the full image and its path. " +
      LOCAL,
  },
];

// Fisher-Yates over a COPY (an in-place shuffle would reorder the pool the test
// imports — same module instance). `rand` is a parameter so the test can draw
// deterministically instead of depending on the module-load draw.
export function shuffleStarters(
  pool: StarterPrompt[],
  rand: () => number = Math.random,
): StarterPrompt[] {
  const out = pool.slice();
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

// Drawn ONCE per page load, at module scope, not per render: the composer's
// chip row is a window into this array, so a fresh draw on every render would
// reshuffle the chips out from under the cursor. The pool above is written
// grouped by capability (all the text-generation briefs together, and so on),
// and this is what mixes the kinds so an unfiltered row is not four image
// prompts in a run.
const SHUFFLED = shuffleStarters(STARTER_PROMPTS);

// The starters to offer for an attached model's capability: only that
// capability's briefs — the chip means "build with THIS model", so an app that
// needs no AI is not an answer to it. The whole mixed pool when nothing is
// attached, and also when the capability is unknown here (an older `?annot=`
// carrying no capability, or one this build has no starters for), where showing
// everything beats showing nothing.
export function startersFor(capability: string | null | undefined): StarterPrompt[] {
  if (!capability) return SHUFFLED;
  const hits = SHUFFLED.filter((s) => s.capability === capability);
  return hits.length ? hits : SHUFFLED;
}
