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

// Every AI starter closes on the same facts, because they are what the
// session cannot guess: the model runs locally (no key, no network), the page
// reaches it through `fused.ai.*`, the fused-render-ai skill documents that
// surface, and model ids are best taken from the catalog.
// The seed prose for an ATTACHED model is far richer (appSeed.ts) — these
// briefs are what a user clicks when they picked the idea first and the model
// second.
const LOCAL =
  "Use the local AI models on this machine through the page's fused.ai API — " +
  "read the fused-render-ai skill first; no cloud keys, no network. Take model ids " +
  "from fused.ai.models.catalog() where you can, and disable the run button " +
  "while a call is in flight.";

// The one capability that can be unservable on the machine running the page:
// off Apple Silicon `catalog()` reports `default: null` for text-to-video and
// every `fused.ai.video` call rejects `unavailable`. Appended to every video
// brief so the session builds the check before it builds the button.
const VIDEO =
  "Before anything else read fused.ai.models.catalog(): if the text-to-video row's " +
  "default is null this machine cannot run video, so say so plainly and hide the " +
  "render button instead of offering one that always fails. ";

export const STARTER_PROMPTS: StarterPrompt[] = [
  // -- No AI -----------------------------------------------------------------
  {
    label: "Habit tracker",
    capability: null,
    glyph: S(<path d="M20 6L9 17l-5-5" />),
    prompt:
      "A habit tracker. Layout: a top bar with today's date and an Add habit button; below " +
      "it one row per habit with a checkbox for today, the name, the cadence badge, and the " +
      "current streak. Add/edit is a small dialog with name and cadence (daily, or a " +
      "weekday multi-select). Under the list, a 12-week GitHub-style heatmap per habit " +
      "(columns = weeks, rows = weekdays, four intensity levels). Data: habits.json in the " +
      "app folder, shape {habits: [{id, name, days: [0-6]}], done: {date: [habitId]}}, read " +
      "with fused.readFile on load and written with fused.writeFile after every change. " +
      "Streak counts only scheduled days, so a weekday-only habit is not broken by a " +
      "weekend. Empty state explains how to add the first habit.",
  },
  {
    label: "Markdown notes",
    capability: null,
    glyph: S(
      <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />,
    ),
    prompt:
      "A markdown notes app. Layout: a left sidebar (search box on top, note list sorted by " +
      "last edited showing title and relative time, New note button) and a main pane split " +
      "into a textarea editor and a live rendered preview, with a toggle for editor-only / " +
      "preview-only. Notes are plain .md files in a notes/ folder beside the app; the title " +
      "is the first heading or the filename; rename renames the file, delete asks once. " +
      "Autosave 500ms after typing stops with fused.writeFile. Search is full-text over " +
      "every note and shows a two-line snippet with matches highlighted in the list. " +
      "Remember the last open note in URL params.",
  },
  {
    label: "CSV dashboard",
    capability: null,
    glyph: S(<path d="M3 3v18h18M8 17V9M13 17V5M18 17v-6" />),
    prompt:
      "A CSV dashboard. Layout: a header with a Pick file button and the current filename; " +
      "a stats strip with one card per numeric column (min, max, mean, null count); a Chart " +
      "panel with column pickers (x, y, type: bar, line, scatter) and the chart; then a " +
      "virtualized table with click-to-sort headers and a per-column filter box. Parse and " +
      "aggregate in a Python data file called through fused.runPython (pandas): one " +
      "function returns column types and stats, another returns the filtered, sorted rows " +
      "for the current page so files with 100k rows stay fast. Remember the last file path " +
      "in the app folder and reopen it on launch. Show a clear error when a file will not " +
      "parse.",
  },
  {
    label: "Mini game",
    capability: null,
    glyph: S(
      <path d="M6 12h4M8 10v4M15 11h.01M18 13h.01M17.3 5H6.7a4.7 4.7 0 0 0-4.6 5.6l1 5A3 3 0 0 0 8 17.4l.6-1.4h6.8l.6 1.4a3 3 0 0 0 4.9-1.8l1-5A4.7 4.7 0 0 0 17.3 5z" />,
    ),
    prompt:
      "A 2048-style sliding tile game. A centered 4x4 board with rounded tiles coloured by " +
      "value, the score and best score above it, and a Restart button. Arrow keys and touch " +
      "swipes slide and merge; tiles animate their slide (~100ms) and pop when merged; a " +
      "new 2 (90%) or 4 (10%) tile spawns after every valid move. Ignore moves that change " +
      "nothing. Show a translucent overlay for Game over (no moves left) and for reaching " +
      "2048 with a Keep going option. Store the best score in score.json in the app folder.",
  },
  {
    label: "Finance calculator",
    capability: null,
    glyph: S(<path d="M22 12h-4l-3 9L9 3l-3 9H2" />),
    prompt:
      "A compound-interest and loan calculator with two tabs. Savings: principal, annual " +
      "rate, years, monthly contribution, compounding frequency; output final balance, " +
      "total contributed, total interest, and a line chart of balance over time. Loan: " +
      "principal, annual rate, term in years, extra monthly payment; output the monthly " +
      "payment, total interest, payoff date, and a full amortization table (month, payment, " +
      "principal, interest, balance) plus a chart of principal vs interest. Every input is " +
      "a slider with a number box, results update live with no submit button, and the " +
      "inputs live in URL params so a scenario can be shared. Format amounts as currency " +
      "with a locale-aware formatter.",
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
      "A pomodoro focus timer. A large centered mm:ss countdown inside a ring that drains, " +
      "the current phase label (Focus / Short break / Long break), Start-Pause and Reset " +
      "buttons, and a row of dots showing progress toward the long break (every 4th). A " +
      "settings popover sets the three durations and auto-start. Timing uses a stored end " +
      "timestamp, not a decrementing counter, so it stays right in a background tab; play a " +
      "short chime and flash the title at each transition. Log each completed focus session " +
      "with its date to sessions.json in the app folder and show a small 7-day bar chart of " +
      "sessions per day below.",
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
      "A shared-expenses splitter. Layout: a People chip row with an add box; an Add " +
      "expense form (description, amount, paid by, split among as checkboxes, equal or " +
      "custom share per person); an expense list with edit and delete; a Balances panel " +
      "showing each person's net (owed or owes, coloured) and a Settle up list with the " +
      "minimum set of transfers. Amounts in cents internally to avoid float drift; custom " +
      "shares must sum to the total and the form says so. Store the whole group as " +
      "group.json in the app folder and support export as CSV.",
  },
  {
    label: "Reading board",
    capability: null,
    glyph: S(
      <path d="M4 19.5V6a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 1.5zM9 8h6M9 12h4" />,
    ),
    prompt:
      "A reading list as a kanban board. Three columns — Want to read, Reading, Finished — " +
      "with drag-and-drop between and within columns. Each card shows title, author, and " +
      "tag chips; Finished cards also show a 1-5 star rating. Clicking a card opens an edit " +
      "drawer with title, author, link, tags, rating, and notes; an Add button on each " +
      "column creates a card there. A top bar holds a search box and a tag filter that both " +
      "narrow all columns. Store the board as board.json in the app folder {columns: [{id, " +
      "title, cardIds}], cards: {id: {...}}} and write it after every change.",
  },

  // -- Text generation -------------------------------------------------------
  {
    label: "Local chat",
    capability: "text-generation",
    glyph: S(
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
    ),
    prompt:
      "A private chat app on a local language model. Layout: a sidebar listing " +
      "conversations (title = first user message, newest first, New chat button) and a main " +
      "thread of bubbles with a composer at the bottom. Call fused.ai with onChunk so the " +
      "reply streams into the last bubble, pass the prior turns as history, and render " +
      "markdown in replies. A model picker in the header lists the text-generation rows " +
      "from fused.ai.models.catalog() and shows which one answered; a Stop button calls " +
      "fused.ai.cancel(). Persist conversations as chats.json in the app folder. " +
      LOCAL,
  },
  {
    label: "Rewrite tool",
    capability: "text-generation",
    glyph: S(
      <path d="M15 4l5 5M17.5 2.5a2.1 2.1 0 0 1 3 3L8 18l-5 1 1-5L17.5 2.5z" />,
    ),
    prompt:
      "A rewriting workbench. Two equal panes: source textarea on the left, result on the " +
      "right. Above them a tone segmented control (Shorter, Plainer, More formal, " +
      "Friendlier, Custom with its own instruction box) and a Rewrite button. Stream the " +
      "rewrite via fused.ai with onChunk, the tone in systemPrompt and the source text as " +
      "the prompt; ask for the rewritten text only, no preamble. Below the result a Diff " +
      "toggle shows a word-level diff (added green, removed red) and a Copy button. Keep " +
      "the last five rewrites as tabs above the result so I can compare. " +
      LOCAL,
  },
  {
    label: "Notes summarizer",
    capability: "text-generation",
    glyph: S(
      <path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3M9 12h5M9 16h3M14 3h7v7" />,
    ),
    prompt:
      "A meeting-notes summarizer. Left: a textarea for raw notes or a transcript plus a " +
      "Summarize button. Right: three editable sections — Summary (one paragraph), " +
      "Decisions (bullets), Action items (checkboxes with owner in bold where the text " +
      "names one). One fused.ai call with a systemPrompt that demands exactly those three " +
      "markdown headings; stream with onChunk and split the result by heading as it " +
      "arrives. A Save button writes the edited result as YYYY-MM-DD-title.md in a " +
      "summaries/ folder beside the app with fused.writeFile, and a sidebar lists past " +
      "summaries to reopen. " +
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
      "A flashcard maker with two modes. Create: paste study material, pick a card count, " +
      "and fused.ai returns a JSON array of {question, answer} — the systemPrompt demands " +
      "JSON only, parse defensively and retry once if it fails; show the cards as an " +
      "editable list with delete and regenerate-one. Review: flip cards one at a time " +
      "(click or Space), rate Again / Hard / Good / Easy, and schedule with a simple SM-2 " +
      "interval; show today's due count and a progress bar. Persist decks as decks.json in " +
      "the app folder with each card's interval, ease, and due date. " +
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
      "A commit-message and changelog writer. Header: a Pick repo folder button and the " +
      "current path. Mode tabs: Staged diff and Commit range (two ref inputs). A Python " +
      "data file called through fused.runPython runs git (git diff --cached, git log " +
      "--patch A..B) and returns the text, truncated to a sane size with a note. fused.ai " +
      "drafts a conventional-commit subject plus a body explaining the why, streamed with " +
      "onChunk into an editable textarea with a Copy button; for a range, group commits " +
      "under Features / Fixes / Other as a markdown changelog. Show a friendly error when " +
      "the folder is not a git repo. " +
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
      "An icon studio. Left panel: subject, style description, background colour picker, a " +
      "locked seed with a dice button, and a textarea of icon names one per line. Right: a " +
      "grid of tiles, one per name, each with the name, a Regenerate button, and a Save " +
      "button. Generate calls fused.ai.image({prompt, seed, width: 512, height: 512}) per " +
      "name, sequentially, with the prompt built as '<name>, <style>, flat icon on a solid " +
      "<colour> background, centered'; show job.previewUrl while a tile renders and the " +
      "finished file via fused.rawUrl. Save copies the PNG into an icons/ folder beside the " +
      "app; Save all writes every tile. " +
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
      "A poster and cover maker. Left controls: artwork description, size preset (A-series " +
      "portrait, square, 16:9), seed with re-roll, and a text layer editor (title, " +
      "subtitle, font family, size, colour, position: top/middle/bottom, alignment). Right: " +
      "the poster preview. fused.ai.image({prompt, width, height, seed}) renders the art; " +
      "draw it on a canvas and composite the text on top so re-rolling the art never " +
      "touches the text layout. Show a progress bar from the job and job.previewUrl while " +
      "rendering. Export PNG at full size via canvas toBlob into an exports/ folder beside " +
      "the app. " +
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
      "A storyboard sketcher. A global style prompt and seed at the top, then a list of " +
      "scene rows, each with a shot description textarea, a caption, a frame slot, and " +
      "Re-roll / Delete buttons; Add scene appends a row and rows drag to reorder. Render " +
      "all runs the rows one at a time with fused.ai.image({prompt: description + ', ' + " +
      "style, seed, width: 768, height: 432}) showing a per-row progress bar and " +
      "job.previewUrl. Export draws every frame with its caption onto one canvas as a " +
      "3-column contact sheet and saves it as a PNG. Save the board (rows, style, seed, " +
      "frame paths) to board.json in the app folder. " +
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
      "A mood board generator. A theme input and a Generate button at the top; below, a " +
      "masonry grid of nine tiles, each rendered with fused.ai.image using the theme plus " +
      "one of nine fixed style modifiers and its own seed, queued one at a time with " +
      "job.previewUrl in the slot while it renders. Each tile shows its modifier and seed " +
      "and has Pin and Re-roll buttons; pinned tiles move into a Keep row at the top. Save " +
      "keep copies those PNGs into a moodboard-<theme>/ folder beside the app. " +
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
      "An avatar maker. Left: dropdowns for style (flat illustration, pixel art, " +
      "watercolour, 3D render), hair, expression, and accessory, a background colour " +
      "picker, and a Prompt box that shows the assembled prompt and stays editable. Right: " +
      "a square preview with a circular mask toggle, Generate, Re-roll (new seed), and " +
      'Cancel wired to fused.ai.cancel("text-to-image"). Render with ' +
      "fused.ai.image({prompt, seed, width: 1024, height: 1024}) and show job.previewUrl " +
      "while it works. Export writes 512 and 1024 px PNGs (via canvas resize) into an " +
      "avatars/ folder beside the app. " +
      LOCAL,
  },

  // -- Video generation ------------------------------------------------------
  // Every video brief ends with VIDEO: this capability is Apple Silicon only
  // with no fallback, so a page that assumes it is a page that is simply
  // broken on the machine it was built for.
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
      "A text-to-video sketchpad. Left: a shot description textarea, a resolution preset " +
      "select, a frame-count select, a seed with dice, and Render / Cancel " +
      '(fused.ai.cancel("text-to-video")). Right: the current clip in a <video controls ' +
      "loop> with its prompt and seed beside it and a progress bar driven by the job while " +
      "it renders via fused.ai.video({prompt, seed, ...}). Below, a session gallery of " +
      "every finished clip as thumbnails; clicking one loads it in the player and Save " +
      "copies the file into a clips/ folder beside the app. " +
      VIDEO +
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
      "A logo-sting generator. Inputs: wordmark text, font, brand colour picker, and a " +
      "motion select (slow reveal, ink bloom, light sweep, particle burst) that maps to a " +
      "fixed prompt template, plus a seed. Render a two-second clip with fused.ai.video and " +
      "play it in a canvas that composites the wordmark centered on top ; show the job's " +
      "progress bar while rendering. Re-roll keeps the prompt and changes the seed. Export " +
      "captures the composited canvas to a WebM via MediaRecorder into an exports/ folder " +
      "beside the app. " +
      VIDEO +
      LOCAL,
  },
  {
    label: "Loop background",
    capability: "text-to-video",
    glyph: S(<path d="M4 8h11a4 4 0 0 1 0 8H8m0 0l3-3m-3 3l3 3" />),
    prompt:
      "A looping-background maker. Inputs: an ambient scene description, a length preset " +
      "(2s/4s/6s), a resolution preset, and a seed. Render with fused.ai.video, then " +
      "preview the clip full-bleed behind a sample headline and paragraph so I can judge " +
      "legibility, with a Text colour toggle (light/dark) and a crossfade at the loop seam " +
      "so the repeat is not a hard cut. Keep a strip of previous renders to switch between, " +
      "and Save copies the chosen clip into a backgrounds/ folder beside the app. " +
      VIDEO +
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
      "A how-to short builder. Left: a list of step rows (step text, auto-numbered, drag to " +
      "reorder, delete) and a global visual style input. Render all runs the steps one at a " +
      "time through fused.ai.video in a vertical 9:16 preset with a per-step progress bar; " +
      "each finished step shows a thumbnail and a Re-render button that re-does only that " +
      "step. Right: a phone-shaped player that plays the steps back to back with the step " +
      "caption burned over the bottom third (drawn on a canvas over the video). Save writes " +
      "the clips and a steps.json into a short/ folder beside the app. " +
      VIDEO +
      LOCAL,
  },
  {
    label: "Shot list",
    capability: "text-to-video",
    glyph: S(<path d="M4 6h2M4 12h2M4 18h2M10 6h10M10 12h10M10 18h6" />),
    prompt:
      "A shot-list renderer. An editable table of shots with columns description, camera " +
      "move (select), seconds, seed, status, and progress; Add row and Render all. Render " +
      "runs the rows one at a time through fused.ai.video, queued so the page stays usable, " +
      "updating each row's status (queued, rendering with a progress bar, done, failed with " +
      "the error message) and a per-row Retry. Below the table a timeline strip shows the " +
      "finished clips as thumbnails proportional to their length; Play all plays them in " +
      "order in one player. Save the table as shots.json and the clips in a shots/ folder " +
      "beside the app. " +
      VIDEO +
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
      "A voice-memo notebook. Left: a memo list (title, date, duration) with New memo (pick " +
      "an audio file, or record with fused.capture.audio when fused.capture.sources() says " +
      "it is available) and a search box. Right: an audio player, then the transcript as " +
      "segments streaming in through fused.ai.transcribe's onChunk with a timestamp " +
      "gutter; clicking a timestamp seeks the player, the current segment highlights during " +
      "playback, and the text becomes editable once the job resolves. Save each memo's " +
      "transcript JSON beside its audio and the list as memos.json in the app folder; " +
      "titles are editable inline. " +
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
      "A searchable transcript reader for podcasts and lectures. Top: a Pick file button " +
      "(audio or video) and a search box. Left: the media player with a progress bar that " +
      "marks search hits. Right: the transcript as paragraphs built from " +
      "fused.ai.transcribe's {text, startSecond, endSecond} segments, streaming in via onChunk, with " +
      "the current segment highlighted and auto-scrolled during playback; clicking any " +
      "segment seeks. Search filters to matching segments with the match highlighted and " +
      "Prev/Next hit buttons. Cache the transcript as <file>.transcript.json beside the " +
      "media and load it instead of re-transcribing when present. " +
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
      "A subtitle maker. Left: a video player with the current cue drawn over the bottom as " +
      "a live preview. Right: a cue table from fused.ai.transcribe's {start, end, text} " +
      "segments, streaming in via onChunk, with editable text, editable start/end fields, " +
      "and nudge buttons (-/+ 100ms), plus Split and Merge on the selected cue. The active " +
      "cue follows playback and clicking a cue seeks. Export writes valid .srt and .vtt " +
      "next to the source video with fused.writeFile, and a language select passes language " +
      "to the transcription. " +
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
      "A spoken-capture inbox. Top: drop or pick a voice note; fused.ai.transcribe turns it " +
      "into {text, startSecond, endSecond} segments. Middle: an Inbox of candidate tasks — each " +
      "sentence containing an imperative or a need to / should / remind me pattern — as " +
      "rows with a checkbox to accept, an editable text field, and a discard button, each " +
      "showing its source timestamp which seeks a small audio player. Bottom: the accepted " +
      "Todo list with done toggles and the timestamp each came from. Persist todos as " +
      "todos.json in the app folder and keep the transcripts beside their audio. " +
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
      "An interview log. Transcribe a recorded conversation with fused.ai.transcribe({path, " +
      "diarize: true}) and render segments as a chat-style transcript, each speaker in its " +
      "own colour with the label from the speaker field (null shows as Unknown). A speaker " +
      "legend lets me rename Speaker 1 to a real name and the rename applies everywhere and " +
      "is saved to <file>.speakers.json beside the audio. An audio player syncs with the " +
      "highlighted segment. Each segment has a star toggle; a Quotes panel lists starred " +
      "segments and Copy yields '\"text\" — Name, mm:ss'. " +
      LOCAL,
  },

  // -- Embeddings ------------------------------------------------------------
  //
  // **`kind` appears in exactly ONE of these five briefs, and that is the whole
  // of the judgement here.** The default embeddings model is a RETRIEVAL encoder
  // (SPEC §40): it was trained with a question marked differently from a
  // passage, and using one side for both costs real recall — silently, since the
  // vectors still come back unit length and comparable. So an ASYMMETRIC app —
  // one that indexes a corpus and then searches it with something that is not
  // itself a corpus entry — has to pass `kind`, and "Semantic search" is the
  // only one of these that is shaped that way.
  //
  // The other three text briefs are SYMMETRIC by construction: "Related notes"
  // compares notes to notes, "Bookmark clusters" clusters bookmarks against each
  // other, "Duplicate finder" scores pairs of the same kind of thing. There both
  // sides ARE the same kind, so the uniform default (`"document"`) is correct and
  // splitting them would be the bug rather than the fix. Do not add `kind` to
  // them for symmetry with the one above.
  //
  // "Photo search" needs neither: it names a multimodal model from `catalog()`,
  // and a dual encoder has no retrieval convention at all — the route refuses
  // `kind` on one.
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
      "A meaning-based file search. Header: a Pick folder button, an Index button with " +
      "progress, and a search box. Indexing walks the folder's .md and .txt files, splits " +
      "each into ~500-character chunks on paragraph boundaries, embeds them with " +
      "fused.ai.embed({texts, kind: \"document\"}), and writes index.json (file, offset, " +
      "text, vector, mtime) beside the app so a rescan re-embeds only changed files. " +
      "Search embeds the query with the same model and kind: \"query\" — the default " +
      "model is a retrieval encoder that instructs a question differently from a passage, " +
      "and using one side for both quietly costs recall — then ranks chunks by cosine " +
      "similarity in JS, and lists the top 20 " +
      "with score, file path, and a snippet with the query terms highlighted; clicking a " +
      "result opens the file. " +
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
      "A related-notes finder. Left: a Pick folder button and the list of notes. Right: the " +
      "selected note rendered as markdown, and a Related panel listing the five most " +
      "similar notes with cosine score bars and the two most similar lines from each. Embed " +
      "every note in a folder with fused.ai.embed({texts}), cache to embeddings.json with " +
      "each file's mtime and re-embed only changed files. A Graph tab draws notes as nodes " +
      "on a canvas with edges above a similarity slider's threshold. " +
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
      "A bookmark clusterer. Left: a textarea for links, one per line as URL, title, and " +
      "optional note, a cluster-count slider (2-12), and Cluster. Right: a column per " +
      "cluster with a heading and the bookmark cards (title as link, note) plus an Outliers " +
      "column. Embed title + note per bookmark with fused.ai.embed({texts}), run k-means in " +
      "JS with the slider's k, name each cluster from the three most central items' shared " +
      "words, and send far-from-centroid items to Outliers. Cards drag between columns; " +
      "Save writes clusters.json in the app folder. " +
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
      "A near-duplicate finder for text. Header: pick a CSV (with a column select) or a " +
      "folder of text files, a similarity threshold slider (0.80-0.99), and Scan. Embed " +
      "every row or file with fused.ai.embed({texts}) with a progress bar, compute all " +
      "pairwise cosine similarities in JS, and list pairs above the threshold sorted by " +
      "score. Each pair shows both texts side by side with a word-level diff highlighted " +
      "and Keep both / Keep left / Keep right buttons. Export writes decisions.csv (left, " +
      "right, score, decision) beside the source. Cache vectors in a JSON file beside the " +
      "source so re-scans with a new threshold are instant. " +
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
      "A photo search by description. Header: a Pick folder button, an Index button with " +
      "progress, and a search box. Indexing embeds every jpg/png/webp in the folder with " +
      "fused.ai.embed({paths}) using a multimodal embedding model from " +
      "fused.ai.models.catalog() and caches vectors to photos.json beside the app keyed by " +
      "path and mtime so re-indexing skips unchanged files. Search embeds the typed text " +
      "with the same model and shows the top 24 matches as a thumbnail grid (fused.rawUrl " +
      "for the images) with score badges; clicking a thumbnail opens a lightbox with the " +
      "full image, its path, and a Reveal in folder button. " +
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
export function startersFor(
  capability: string | null | undefined,
): StarterPrompt[] {
  if (!capability) return SHUFFLED;
  const hits = SHUFFLED.filter((s) => s.capability === capability);
  return hits.length ? hits : SHUFFLED;
}
