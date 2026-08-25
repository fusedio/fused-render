// The Home hero — wordmark, headline, and the prompt-first composer that
// names (haiku via /api/ai), scaffolds (POST /api/apps/new), and lands in the
// new app's claude chat. Shared by Home ("/") and the /apps hub, which is why
// it lives in the builder app rather than the shell.
import { useEffect, useRef, useState } from "react";
import { aiComplete, createApp, type DefaultModel, type SessionEffort } from "@platform/lib/api";
import { navigate, navigateUrl, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { startersFor } from "./starterPrompts";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { Select, TextArea } from "@platform/ui/field/fields";
import { type AppAnnotation } from "@platform/lib/appAnnotation";

// URL of an app folder's claude chat, attached to a specific live run.
// `_mode` is the shell's template selector; `run` is a plain view param the
// claude template reads through fused.params (its boot resumes that
// run, so a session started server-side is picked up exactly like one the
// page started itself). Folder-scoped on purpose: the server starts the
// scaffolding session via the claude agent on the app FOLDER, so the
// re-attach must land in the same template — same runs dir.
// (There is only one chat template now, so "which
// chat" is no longer a question at all; the `_mode` still has to be spelled out
// because the folder's default mode is the app itself, not the chat.)
// An ORDINARY explorer URL for the folder. It used to be the builder route
// (/apps/<tag>/<name>, rebuilt from the folder's last two path segments); that
// namespace is gone, and urlForFsPath takes the whole abspath the server
// returned — including its Windows-backslash normalization, which the old
// segment split had to do by hand or silently take the drive-rooted path as one
// segment.
// `model`/`effort` ride along when the composer's pickers were used, so
// the chat's own pills open showing what the scaffolding turn actually ran with
// and the NEXT turn keeps it. Omitted when empty: the template reads these
// through fused.params, and an empty param would beat its own detection of what
// this project is really being worked in.
export function claudeChatUrl(
  appDir: string,
  runId: string,
  model: DefaultModel = "",
  effort: SessionEffort = "",
): string {
  const params = new URLSearchParams({ _mode: "claude", run: runId });
  if (model) params.set("model", model);
  if (effort) params.set("effort", effort);
  return urlForFsPath(appDir.replace(/\/+$/, ""), "?" + params.toString());
}

// -- Prompt-first creation (the hero composer) --------------------------------

// Kebab-case whatever the model (or, as a fallback, the user's own prompt)
// gave us into a safe app folder name: lowercase, [a-z0-9-] only, at most
// five words. Never returns something _app_name_error would reject.
function kebabName(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .split("-")
      .filter(Boolean)
      .slice(0, 5)
      .join("-")
      .slice(0, 48) || "my-app"
  );
}

const NAME_SYSTEM_PROMPT =
  "You name software projects. Given a description of an app, reply with a " +
  "short kebab-case name for it: 2-4 lowercase words joined by hyphens, " +
  "letters and digits only. Reply with ONLY the name — no quotes, no prose.";

// A kebab-case folder name for an app described by `prompt`: ask the AI relay
// (haiku, the server default — cheap and fast), fall back to slugging the
// prompt's own words when the relay is unavailable or answers garbage.
async function suggestAppName(prompt: string): Promise<string> {
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT).then((t) => t.trim());
    // NAME_SYSTEM_PROMPT asks for a bare kebab-case reply; if the model added
    // prose instead (common even with "no prose" in the ask), `text` won't be
    // pure lowercase-and-hyphens. Taking the first token then would name the
    // folder after whatever word led the prose (e.g. "sure") instead of
    // falling through to the actual prompt slug below.
    if (/^[a-z0-9]+(-[a-z0-9]+)*$/.test(text)) {
      const name = kebabName(text);
      if (name !== "my-app") return name;
    }
  } catch {
    // relay down / claude missing: the slug fallback below still works
  }
  return kebabName(prompt);
}

// Create the app under a collision-proof name: on 409 retry with -2, -3, …
// Any other failure propagates.
async function createAppUnderFreeName(
  name: string,
  prompt: string,
  model: DefaultModel,
  effort: SessionEffort,
) {
  for (let i = 1; ; i++) {
    const attempt = i === 1 ? name : `${name}-${i}`;
    try {
      return await createApp(attempt, prompt, model, effort);
    } catch (e) {
      if ((e as { status?: number }).status !== 409 || i >= 20) throw e;
    }
  }
}

// How many starter chips the row shows at once, and therefore how far the
// shuffle button advances. Four rather than three because the pool is deep
// enough now (starterPrompts.tsx) that three was showing a smaller share of it
// than the row had room for — `.home-composer-samples` wraps, so a narrow
// window folds the fourth chip onto its own line instead of overflowing.
const SAMPLE_ROW = 4;

// The composer's two session pickers — what the scaffolding turn runs
// with, and what the chat it lands in opens showing. Both lists are the claude
// template's own vocabulary (template.html MODELS / EFFORTS, and the server
// validates against the same sets), because these values are handed straight to
// the CLI as --model / --effort.
//
// "" is the FIRST option of each and the default: it means no flag at all, so
// the session keeps whatever the template would have detected for this project
// from its own transcripts and settings. A composer that shipped `sonnet` /
// `medium` preselected would silently override that detection for every app
// built from here, which is a stronger claim than the picker is making.
//
// The labels are BARE — "Auto", "opus", "high" — and not "Auto model" / "high
// effort": each pill's glyph names its axis, so repeating it in the text is the
// same word twice in one control. It is also what keeps two pills quiet enough
// to sit unbordered in the footer of the box rather than reading as buttons.
const MODEL_CHOICES: { value: DefaultModel; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "fable", label: "fable" },
  { value: "opus", label: "opus" },
  { value: "sonnet", label: "sonnet" },
  { value: "haiku", label: "haiku" },
];

const EFFORT_CHOICES: { value: SessionEffort; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
  { value: "xhigh", label: "xhigh" },
  { value: "max", label: "max" },
];

// A glyph each, because the two pickers sit side by side with no border to tell
// them apart: the icon is what says which control you are looking at before the
// value is read — and it is what lets the labels stay bare words ("Auto",
// "high") instead of spelling their own axis out. Drawn in the composer's own weight (13px,
// 2px stroke) rather than borrowed from MenuIcons, which is tuned 1.5px for menu
// rows — a menu glyph beside these chips reads thin and unrelated.
const PICK_GLYPHS = {
  // Model — the sparkle MenuIcons uses for "new", the app's existing mark for
  // the AI doing something on your behalf.
  model: (
    <path d="M11 3.5l1.6 4.4 4.4 1.6-4.4 1.6L11 15.5 9.4 11.1 5 9.5l4.4-1.6L11 3.5zM17.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z" />
  ),
  // Effort — a gauge: a needle on a dial is the one figure that reads as "how
  // hard is this being pushed" without a word beside it.
  effort: (
    <>
      <path d="M4.5 17a8 8 0 1 1 15 0" />
      <path d="M12 15l3.5-4" />
    </>
  ),
};

// One borderless picker: glyph, then a native select carrying its own chevron.
// A <label> around both so the glyph is part of the control's hit area rather
// than decoration beside it — the pill has no border to aim at, so the target
// has to be the whole thing.
function ComposerPick<T extends string>({
  glyph,
  label,
  value,
  choices,
  disabled,
  onPick,
}: {
  glyph: keyof typeof PICK_GLYPHS;
  label: string;
  value: T;
  choices: { value: T; label: string }[];
  disabled: boolean;
  onPick: (next: T) => void;
}) {
  return (
    <label className="home-composer-pick">
      <span className="home-composer-pick-glyph" aria-hidden="true">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {PICK_GLYPHS[glyph]}
        </svg>
      </span>
      <Select
        className="home-composer-pick-sel"
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(e) => onPick(e.target.value as T)}
      >
        {choices.map((c) => (
          <option key={c.value} value={c.value}>
            {c.label}
          </option>
        ))}
      </Select>
    </label>
  );
}

// The hero's prompt box — the claude.ai / v0 "what do you want to build?"
// composer. Submitting names the app (haiku via /api/ai), scaffolds it, and
// lands in the new folder's claude chat exactly like the New-app panel does.
function HeroComposer({ onCreated }: { onCreated: () => void }) {
  const [prompt, setPrompt] = useState("");
  // The chip above the box — set by the Playground's "Build an app with this
  // AI" button (?annot=). null means no model is annotated; the composer
  // reads as a plain prompt box exactly like today.
  const [annotation, setAnnotation] = useState<AppAnnotation | null>(null);
  // Empty = "let the chat decide", the default; see MODEL_CHOICES.
  const [model, setModel] = useState<DefaultModel>("");
  const [effort, setEffort] = useState<SessionEffort>("");
  const [phase, setPhase] = useState<"idle" | "naming" | "creating">("idle");
  const [error, setError] = useState<string | null>(null);
  // Claude would not start for an app that WAS created — see the submit path.
  const [sessionError, setSessionError] = useState<string | null>(null);
  // Which window of SAMPLE_ROW starter chips is showing; shuffle advances it.
  const [sampleOffset, setSampleOffset] = useState(0);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  // A `?annot=` in the URL pre-fills the chip — the Playground's "Build an
  // app with this AI" hands its model + tuned settings through here as a
  // JSON `AppAnnotation`, encoded rather than dumped into the prompt text.
  // Consumed once and removed (replaceSearch, no history entry): an annot
  // that survived in the URL would reappear on the next mount.
  // Grow the box with its content. `Infinity` because the ceiling here is CSS,
  // not pixels: the 3-row floor and 10-row ceiling are min/max-height on
  // `.home-composer-input`, and past the ceiling the textarea scrolls. The hook
  // is what keeps the height it writes from going stale when the box's WIDTH
  // changes (a sidebar toggle, a window resize) and the same text rewraps —
  // this composer had that bug for exactly as long as it grew itself inline.
  // Driven from an effect on `prompt` rather than onChange because a starter
  // chip sets the text programmatically and has the longest briefs of anything
  // that lands in here.
  const { ref: inputRef, grow } = useAutoGrow(Infinity);
  useEffect(grow, [prompt, grow]);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const raw = params.get("annot");
    if (!raw) return;
    try {
      setAnnotation(JSON.parse(raw) as AppAnnotation);
    } catch {
      // Malformed/tampered param — drop it silently rather than crash the
      // composer over a URL someone hand-edited.
    }
    params.delete("annot");
    const search = params.toString();
    replaceSearch(location.pathname + (search ? "?" + search : ""));
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const busy = phase !== "idle";
  const canSubmit = prompt.trim().length > 0 && !busy;

  // The starters on offer right now: the whole mixed pool, or — once a model is
  // attached as a chip — only the briefs for what that model DOES, so the row
  // under an image model is four image apps rather than four ideas it cannot
  // help with. `startersFor` falls back to the full pool for a chip carrying no
  // capability (an older `?annot=` link).
  const samples = startersFor(annotation?.capability);
  // The window has to restart when the pool underneath it changes: an offset
  // picked in the 30-odd mixed pool is meaningless in a five-brief capability
  // slice — modulo would keep it in range but land somewhere arbitrary.
  useEffect(() => setSampleOffset(0), [annotation?.capability]);

  const submit = async () => {
    if (!canSubmit) return;
    const trimmed = prompt.trim();
    // The chip's detail is instructions for the CLAUDE SESSION, not something
    // the user typed — spliced in ahead of what they wrote so it reads as
    // "here's the model, here's the app I want" without the user ever
    // seeing or editing the model prose.
    const full = annotation ? `${annotation.detail}\n\nThe app I want: ${trimmed}` : trimmed;
    setError(null);
    setSessionError(null);
    setPhase("naming");
    try {
      const name = await suggestAppName(trimmed);
      if (!alive.current) return;
      setPhase("creating");
      const res = await createAppUnderFreeName(name, full, model, effort);
      // The folder exists from here on, so the Recent grid is stale — refresh it
      // now, since the session-error branch below stays on this page.
      onCreated();
      // Same landing logic as NewAppPanel: a session error must not read as
      // success, and a live run means the claude chat is the right landing.
      if (res.session_error) {
        if (alive.current) {
          // The FOLDER exists; only the session failed. Kept as its own state
          // so the card can say that plainly and still show the spawn error
          // verbatim — folding the two into one string made the error
          // unclassifiable (and unsearchable) by prefixing it.
          setSessionError(res.session_error);
          setPhase("idle");
        }
        return;
      }
      if (res.run_id) navigateUrl(claudeChatUrl(res.path, res.run_id, model, effort), { isDir: true });
      else navigate(res.entry_html, { isDir: false });
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setPhase("idle");
      }
    }
  };

  return (
    <div className="home-composer-wrap">
      <div className={"home-composer" + (busy ? " is-busy" : "")}>
        {annotation && (
          <div className="home-composer-annots">
            <span className="home-composer-annot" title={annotation.detail}>
              <span className="home-composer-annot-at" aria-hidden="true">@</span>
              {annotation.name}
              <button
                type="button"
                className="home-composer-annot-x"
                aria-label={`Remove ${annotation.name} annotation`}
                disabled={busy}
                onClick={() => setAnnotation(null)}
              >
                ✕
              </button>
            </span>
          </div>
        )}
        <TextArea
          ref={inputRef}
          className="home-composer-input"
          placeholder="What do you want to build?"
          aria-label="What do you want to build?"
          value={prompt}
          rows={1}
          disabled={busy}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            // Enter submits (the composer is a one-shot prompt, not a
            // document); Shift+Enter keeps the newline for longer briefs.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="home-composer-bar">
          {/* The bar's left end used to spell out ↵ / ⇧↵. Those are the two
              keystrokes every chat box on the machine already answers to, and
              the space is worth more spent on the one thing this composer could
              not say at all: WHICH Claude builds the app. The pickers
              stay mounted while a create is in flight — disabled, like the
              starter chips — and the phase text takes the space beside them
              rather than replacing them, so nothing moves when it appears. */}
          <div className="home-composer-picks">
            <ComposerPick
              glyph="model"
              label="Model"
              value={model}
              choices={MODEL_CHOICES}
              disabled={busy}
              onPick={setModel}
            />
            <ComposerPick
              glyph="effort"
              label="Effort"
              value={effort}
              choices={EFFORT_CHOICES}
              disabled={busy}
              onPick={setEffort}
            />
            <span className="home-composer-hint">
              {phase === "naming" && "Naming your app…"}
              {phase === "creating" && "Creating the app…"}
            </span>
          </div>
          <button
            type="button"
            className="home-composer-send"
            aria-label="Build it"
            title="Build it"
            disabled={!canSubmit}
            onClick={submit}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
      </div>
      <div className="home-composer-samples">
        {Array.from({ length: Math.min(SAMPLE_ROW, samples.length) }, (_, i) => {
          const s = samples[(sampleOffset + i) % samples.length];
          return (
            <button
              key={s.label}
              type="button"
              className="home-composer-sample"
              title={s.prompt}
              disabled={busy}
              onClick={() => setPrompt(s.prompt)}
            >
              <span className="home-composer-sample-glyph" aria-hidden="true">
                {s.glyph}
              </span>
              {s.label}
            </button>
          );
        })}
        <button
          type="button"
          className="home-composer-sample home-composer-shuffle"
          aria-label="More ideas"
          title="More ideas"
          disabled={busy}
          onClick={() => setSampleOffset((o) => (o + SAMPLE_ROW) % samples.length)}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v5h-5" />
          </svg>
        </button>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {/* THE OTHER PLACE A USER REACHES FOR CLAUDE, and the one where being
          told to install it matters most: they typed what to build. The app
          folder is already there, so the card says so rather than reading as a
          total failure — and the spawn error stays verbatim inside it, which a
          prefixed string could not do. */}
      {sessionError && (
        <TroubleCard
          what="starting a Claude session to build a new app"
          error={sessionError}
          facts={{ page: location.pathname + location.search }}
          onRetry={() => setSessionError(null)}
        >
          <span className="deploy-muted">
            The app folder was created — only the session failed.
          </span>
        </TroubleCard>
      )}
    </div>
  );
}

// The hero card itself — wordmark, headline, and the prompt composer.
// Exported so /apps can show the exact same hero above its grid; `onCreated`
// lets each page refresh its own app list once the folder exists.
export function HomeHero({ onCreated }: { onCreated: () => void }) {
  return (
    <header className="home-hero">
      {/* Fused mark + headline. The "App" wordmark that used to sit between
          them is gone — the sidebar entry that got you here already names the
          page — so the mark stands alone beside the tagline. Both theme
          renders are in the DOM; CSS shows the one matching data-theme. */}
      <h1 className="home-hero-brand">
        <img className="home-hero-logo home-hero-logo-dark" src={logoMarkDark} alt="" aria-hidden="true" />
        <img className="home-hero-logo home-hero-logo-light" src={logoMarkLight} alt="" aria-hidden="true" />
        <span className="home-hero-tagline">
          Build your next <span className="home-hero-accent">local app</span>
        </span>
      </h1>
      {/* The hero's only verb, prompt-first: describe the app right here
          and a named, scaffolded folder + claude session comes back. */}
      <HeroComposer onCreated={onCreated} />
    </header>
  );
}
