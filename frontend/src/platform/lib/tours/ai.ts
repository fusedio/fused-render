// The AI Models tour (/ai-models/*): what you can DO here, done rather than
// described. It used to be one line per tab — four descriptions of what each
// tab IS, which reads as a table of contents nobody asked for. Now the middle
// of it is a real turn: the tour writes a sample prompt into the composer, the
// user edits it or sends it, and SENDING is what walks the tour on to the reply
// it just produced. The two "where the next thing lives" steps follow.
//
// Steps 1–3 point at the PLAYGROUND'S OWN CONTROLS. The tour auto-fires on
// /ai-models/* and playground is the default tab, so the model rail, the
// composer and the response block are mounted for the visit this tour is
// written for. If the user landed on another subtab, those elements are not in
// the DOM and `presentSteps` (registry.ts) simply drops them — a tour never
// breaks on an absent target.
//
// Steps 4–5 point at TAB LINKS, not at the controls inside those tabs, for the
// same reason: only the active tab's internals are mounted, so a step aimed at
// the Hub search box or the benchmark Run button would drop out of every run
// that starts on playground — which is all of them. The tab link is the thing
// that is always there, and it is also the click the step is asking for.
//
// Selectors are the tab strip's own `data-tab` attributes (AiModelsPage) and
// the playground's own class names, not nth-child: a tab added or reordered in
// AI_MODELS_TABS must not silently repoint a step. Plain strings rather than an
// import of `routes.ts` — this is the platform layer, which may not reach into
// an app (check-boundaries.mjs).
import type { Tour } from "./registry";
import { prefillInput } from "./prefill";

// The playground composer, inside out: the box, the button, the reply. The
// response block is one element in two states (the dashed idle slot and the
// filled answer both render `.pg-answer-block`, see ResultSlot), so the step
// that lands on it after the send has something to spotlight either way.
const BOX = ".pg-composer textarea";
const SEND = ".pg-composer .pg-send";
const ANSWER = ".pg-answer-block";

// Short, harmless, and CAPABILITY-NEUTRAL: whichever model the rail has picked
// — chat, image, video — "a tiny robot reading in a cozy library" is a sensible
// thing to ask of it, where a chat-shaped prompt fed to an image model's
// "describe a picture" box read as nonsense.
const SAMPLE = "A tiny robot reading a book in a cozy library.";

export const aiTour: Tour = {
  id: "ai",
  title: "AI Models",
  matches: (pathname) => pathname === "/ai-models" || pathname.startsWith("/ai-models/"),
  // The playground tab by name, matching the sidebar's own AI Models row
  // (`tabHref("playground", "")`): the bare prefix is redirected by App.tsx, and
  // a replay that lands on a URL it is immediately rewritten off is a replay
  // whose first steps race that redirect. Spelled out rather than imported for
  // the boundary reason in this file's header.
  startPath: "/ai-models/playground",
  steps: () => [
    {
      element: ".pg-side",
      popover: {
        title: "Pick a model",
        description: "Choose which AI model to use.",
      },
    },
    {
      element: ".pg-composer",
      popover: {
        title: "Try this prompt",
        description:
          "We filled one in for you. Edit it or leave it, then press Run to send it.",
      },
      // Fills the box only if it is empty, through React's own onChange path
      // (see prefill.ts) — so the Run button, disabled on an empty prompt,
      // actually comes alive.
      onEnter: () => prefillInput(BOX, SAMPLE),
      // No Next on this step: sending IS next. Run for the mouse, Enter for the
      // keyboard — the composer submits from its own onKeyDown without ever
      // touching the button, so both paths are named.
      advanceOn: SEND,
      actionText: "Run it",
      advanceOnEnter: BOX,
    },
    {
      element: ANSWER,
      popover: {
        title: "Watch the reply",
        description: "The answer streams in here, token by token, straight off this machine.",
      },
    },
    {
      element: '[data-tab="local"]',
      popover: {
        title: "Get more models",
        description: "Search and download more models, then try them in the Playground.",
      },
    },
    {
      element: '[data-tab="benchmark"]',
      popover: {
        title: "Compare speed",
        description: "Run a benchmark to see each model's speed.",
      },
    },
  ],
};
