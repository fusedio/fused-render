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
// them. That module did not move and did not change — if its tests needed
// touching, something moved that was supposed to move unchanged.
//
// The `.prefs-*` classes come with it. They are the app's settings vocabulary
// rather than the Preferences page's private one (Mounts uses them too), and
// re-dressing a moved control in card classes would make a switch that unloads
// a model look like one more thing on an inventory page.
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
  wouldChangeEngine,
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
}: {
  row: CapabilityEngine;
  auto: string;
  onChange: (p: Prefs) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<"switched" | "unloaded" | null>(null);
  const warning = ignoredWarning(row);

  const choose = async (code: string) => {
    if (busy || code === row.selected) return;
    // Worked out BEFORE the write, from the state that is about to be
    // replaced: afterwards the server's answer is the new reality and there is
    // nothing left to compare it against.
    const consequential = wouldChangeEngine(row, code, auto);
    setBusy(true);
    setError(null);
    try {
      const next = await putEngineForCapability(row.capability, code);
      onChange(next);
      // **Whether a model was unloaded is the SERVER's answer, not a guess from
      // here.** Nothing in this payload says what is resident, and the usual
      // case is that nothing is: a fresh app has loaded no model until the
      // first transcription, so switching engines evicts nothing — and the page
      // was announcing an eviction that had not happened. `consequential` still
      // decides whether the switch is worth mentioning at all, because it
      // answers a different question (did the effective engine move, which also
      // rewrites the Local tab's engine tags and Discover's suggestions); the
      // server decides which sentence.
      const unloaded = (next.engines.unloaded?.length ?? 0) > 0;
      setChanged(unloaded ? "unloaded" : consequential ? "switched" : null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="prefs-field">
      <h3>{capabilityLabel(row.capability)}</h3>
      <label>
        Engine{" "}
        <select value={row.selected} disabled={busy} onChange={(e) => choose(e.target.value)}>
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
      </label>
      <div className="deploy-muted">{servingLine(row)}</div>
      {warning && <div className="deploy-muted">{warning}</div>}
      {/* The consequence, in four words at most. It stays because an unload is
          a real thing that just happened to the user's machine and nothing else
          on screen would report it — but the paragraph explaining WHY the model
          was unloaded and how suggestion lists work was an essay after picking
          a menu item.

          TWO sentences because there are two outcomes, and the shorter one is
          not a weaker version of the longer: "Switched." is the whole truth
          when the engine moved and no model was resident to lose. */}
      {changed && (
        <div className="deploy-muted">
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
export default function AiModelsEngines() {
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
        <section className="prefs-section">
          <h2>Inference engines</h2>
          {/* One line. An earlier draft explained which backend wins on which
              platform and what a switch costs; a settings surface states what a
              control does, and the rest is an essay the reader did not open
              this tab for. What survives is only what cannot be inferred from
              the controls themselves — that the choice is per capability, and
              what Automatic means. */}
          <p className="deploy-muted">
            Which backend runs local models. <b>Automatic</b> picks the best one this machine
            can run.
          </p>
          {prefs.engines.capabilities.map((row) => (
            <CapabilityEngineRow
              key={row.capability}
              row={row}
              auto={prefs.engines.auto}
              onChange={setPrefs}
            />
          ))}
        </section>
      )}
    </div>
  );
}
