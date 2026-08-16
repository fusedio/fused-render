// The Engines tab of /ai-models (SPEC §40, D302) — which local-model backend
// serves each capability.
//
// **It lived on Preferences, and moving it is the point of this module.** The
// setting is about MODELS, and every consequence of changing it is on this
// page: which cards can be loaded, what their engine tags say, and what
// Discover suggests. On the settings page a user had to know that "Inference
// engines" was the answer to "why can't I load this?" — a question they were
// asking with the unloadable card in front of them, two navigations away from
// the control. Here the control and its consequences are one page apart by a
// tab click, and every sentence that used to read "switch it in Preferences →
// Inference engines" now points at a tab beside the one you are on.
//
// A TAB rather than a panel above the listing: it is a settings surface, and
// stacking it on top of the grid would put a second heading hierarchy in front
// of the section headings the Local tab just gained.
//
// The rendering is four lines of JSX per row; the SENTENCES are where this can
// be wrong, and they live in `@shell/engines` with `engines.test.ts` driving
// them. Not one of them changed in the move. What the move DID add there is
// `switchOutcome`: on Preferences the consequences of a switch were on another
// page and arriving here refetched them, and now they are the tab next door.
//
// **The `.prefs-*` classes did NOT survive the move, and the argument for
// keeping them did not survive contact with the rendered page.** It was that a
// switch which unloads a model should not look like one more thing on an
// inventory page. What it produced was three bare native `<select>`s adrift on
// an empty page, each under a heading and beside a repeated "Engine" label,
// with its resolved status stranded on a line below — a form somebody forgot to
// lay out, in an app where the two tabs beside it are made of cards. The
// setting is not made less consequential by sharing the page's vocabulary; it
// is made findable. One card per capability, one row inside it: the name, the
// control, the reality.
import { useEffect, useState } from "react";
import { getPrefs, putEngineForCapability } from "@platform/lib/api";
import type { CapabilityEngine, Prefs } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import {
  capabilityLabel,
  choiceReason,
  ignoredWarning,
  servingLine,
  switchOutcome,
} from "@shell/engines";

// One capability's engine: a <select> holding Automatic and every backend.
//
// A dropdown rather than a radio list, so that a machine with three engines for
// one capability is one line high instead of four — and so this reads like
// every other choice-of-several in this app's settings, which are all selects.
//
// **The reason an unavailable engine cannot be picked is folded into its own
// option label**, which is the whole difficulty a dropdown has here and the
// reason this was radios first. A greyed-out radio carries its explanation
// beside it permanently; a disabled <option> is invisible until the menu is
// opened, and its `title` is not reliably shown at all. So the sentence — the
// registry's own, which the page cannot synthesise — goes in the text: "MLX
// Whisper (Apple Silicon) — needs Apple Silicon (this is windows/amd64)".
// State that would otherwise need a line of prose beside the control goes into
// the option labels, so the control still explains itself when it is read.
//
// Unavailable engines stay in the menu, disabled. Hidden, a Windows user would
// have no way to learn that the MLX path exists and why it is not for them —
// and a stored preference for one is what the select still SHOWS as its value,
// because "your choice, and why it is not in force" is exactly what the muted
// lines underneath go on to explain. It is the same rule the cards on the Local
// tab now follow for their own controls: disabled and explained, never absent.
function CapabilityEngineRow({
  row,
  auto,
  onChange,
  onSwitched,
}: {
  row: CapabilityEngine;
  auto: string;
  onChange: (p: Prefs) => void;
  /** A switch that changed something the rest of the page is showing. */
  onSwitched: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<"switched" | "unloaded" | null>(null);
  const warning = ignoredWarning(row);
  const label = capabilityLabel(row.capability);

  const choose = async (code: string) => {
    if (busy || code === row.selected) return;
    setBusy(true);
    setError(null);
    try {
      const next = await putEngineForCapability(row.capability, code);
      // `row` is still the row this closure captured, i.e. the one the PUT
      // replaced — which is the only state the "did anything move" half of the
      // outcome can be measured against. Read `switchOutcome` for what each of
      // the two halves is answering and who answers it.
      const outcome = switchOutcome(row, code, auto, next);
      onChange(next);
      setChanged(outcome);
      // The Local tab is one tab click away and is STILL MOUNTED behind this
      // one: its listing was fetched under the preference that just changed, so
      // anything the switch moved has to reach it now. Nothing else would tell
      // it — this page no longer remounts on the way here (it was a Preferences
      // tab, and the navigation back is what used to refetch).
      if (outcome) onSwitched();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cc-mdcard am-engine-card">
      {/* Capability, control, reality — one row, in that order. The three used
          to be three stacked blocks, which is what made "Engine" a visible
          label at all: a select on its own line has to say what it is for.
          Beside the capability's own name it does not, and the repeated word
          down a column of three was the loudest thing on the tab. The name
          IS the label, so it is a <label> and the select has no other. */}
      <div className="am-engine-row">
        <label className="am-engine-cap" htmlFor={`engine-${row.capability}`}>
          {label}
        </label>
        <select
          id={`engine-${row.capability}`}
          className="field-control am-engine-select"
          value={row.selected}
          disabled={busy}
          onChange={(e) => choose(e.target.value)}
        >
          {/* First, and the only option with no engine behind it. */}
          <option value={auto}>Automatic</option>
          {row.choices.map((choice) => {
            // Null for an engine that CAN be picked, which is the whole of what
            // this adds to a label: what a backend is LIKE ("transcribes on the
            // GPU") is editorial and lives on the Discover tab, where somebody
            // is choosing what to download. What is left is the registry's
            // sentence about why a greyed-out option is greyed out, and in a
            // menu the only place it can be read is here.
            const reason = choiceReason(choice);
            return (
              <option key={choice.code} value={choice.code} disabled={busy || !choice.available}>
                {choice.label}
                {reason ? ` — ${reason}` : ""}
              </option>
            );
          })}
        </select>
        {/* What is ACTUALLY serving this capability, beside the control rather
            than under it: the select shows the choice, this reports reality,
            and they are allowed to differ — which only reads as a pair when
            they are on one line. */}
        <span className="am-engine-serving">{servingLine(row)}</span>
      </div>
      {warning && <div className="am-engine-note">{warning}</div>}
      {/* The consequence, in four words at most. It stays because an unload is
          a real thing that just happened to the user's machine and nothing else
          on screen would report it — but the paragraph explaining WHY the model
          was unloaded and how suggestion lists work was an essay after picking
          a menu item.

          TWO sentences because there are two outcomes, and the shorter one is
          not a weaker version of the longer: "Switched." is the whole truth
          when the engine moved and no model was resident to lose. */}
      {changed && (
        <div className="am-engine-note">
          {changed === "unloaded" ? "Switched. Loaded model unloaded." : "Switched."}
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

/** The tab's whole content: one row per capability, over its own copy of prefs.
 *
 *  It fetches `/api/prefs` itself rather than being handed them, because this is
 *  the only thing on /ai-models that reads a preference — threading a prefs
 *  load through the page would make every visit to the Local tab pay for a
 *  request nothing on it uses. The tab is not mounted until it is selected, so
 *  the fetch happens on the click.
 */
export default function AiModelsEngines({ onSwitched }: { onSwitched: () => void }) {
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getPrefs()
      .then((p) => alive && setPrefs(p))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="am-engines">
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!prefs && !error && <SkeletonLines rows={3} label="Loading engines" />}
      {prefs && (
        <>
          {/* ONE line, and no heading. The page's own head already says "AI
              Models" over "Which backend runs each kind of local model", so an
              <h2> reading "Inference engines" above a paragraph reading "Which
              backend runs local models" was the tab restating its own chrome
              twice before saying anything. What cannot be inferred from three
              labelled selects is what the first option in each of them means,
              and that is all this says. */}
          <p className="am-engines-note">
            <b>Automatic</b> picks the best engine this machine can run.
          </p>
          {prefs.engines.capabilities.map((row) => (
            <CapabilityEngineRow
              key={row.capability}
              row={row}
              auto={prefs.engines.auto}
              onChange={setPrefs}
              onSwitched={onSwitched}
            />
          ))}
        </>
      )}
    </div>
  );
}
