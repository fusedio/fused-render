// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Two tabs (D125):
//   Render preferences — Appearance, Default model (which Claude model the
//     chat and fused.ai reach for when nothing else has said), Call log
//     (capture/redaction/retention for
//     fused_render/calls.py), Deploy to Fused account (the opt-in Deploy-button
//     toggle), and Accessibility. Always present; the
//     default (clean URL). No Tour button — the tour still runs itself on a
//     first visit (App.tsx's maybeAutoStartTour); it is onboarding, not a
//     preference. The app's OWN log is not here either: it is disposable
//     temp-dir output (D68) reached from the desktop tray's "Open app logs", and a
//     second "Logs" heading next to the Call log section only ever read as the
//     call log's own settings.
//   Inference engines  — which local-model backend serves each capability
//     (SPEC §40, D301). A tab rather than a section on the render tab, and
//     that is a deliberate call in a page whose whole comment block is about
//     not adding tabs lightly: this is the one control here that is about
//     MODELS rather than about rendering, its rows are per-capability and
//     grow with the registry, and each row carries an availability
//     explanation of its own. Folded into "Render preferences" it would have
//     been the longest section on the page and the least related to its
//     neighbours. Always present, like Indexing — an engine you cannot see is
//     one you cannot fix, and the tab is where "why did my suggested models
//     change?" is answered.
//   Fused account       — the account/sign-in/environments panel (formerly
//     its own `/view/_account` page, folded in once it stopped being a
//     separate sidebar entry). Shown only once Deploy is enabled — that's
//     the only reason this app cares about a Fused account.
// Deliberately NOT a third tab: the Claude Config panel (apps/claude_config)
// briefly sat here, and a settings page hosting a second settings app — with
// its own section nav and scroll containers — inside one of its tabs never read
// as one page. It has its own sidebar routes now (shell/GlobalSidebar).
// The active tab lives in the URL (`?tab=account`), same pattern as
// Templates' bindings/library tabs.
// Template bindings live in the dedicated /view/_templates view.
import { useEffect, useState } from "react";
import {
  getPrefs,
  putCallsEnabled,
  putCallsParamsMode,
  putCallsRetentionDays,
  putDefaultModel,
  putDeployEnabled,
  putEngineForCapability,
  putReaderEnabled,
} from "@platform/lib/api";
import type { CallsParamsMode, CapabilityEngine, Prefs } from "@platform/lib/api";
import { navigate, navigateUrl } from "@platform/lib/router";
import { notifyPrefsChanged } from "@platform/lib/prefs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { useThemePref } from "@platform/lib/theme";
import { AccountPanel } from "@shell/Account";
import { IndexingPanel } from "@shell/Indexing";
import {
  capabilityLabel,
  choiceReason,
  ignoredWarning,
  servingLine,
  wouldChangeEngine,
} from "@shell/engines";

type PrefsTab = "render" | "engines" | "indexing" | "account";

// The one section on this page that is deliberately NOT server-backed. Every
// other control here round-trips /api/prefs (shell/prefs.py); Appearance is
// per-browser-profile localStorage["fused-render:theme"] by decision — SPEC §30
// AP-1 / D134 — so a browser tab and the desktop window can legitimately hold
// different choices, and there is no server store to keep in sync. Writes are
// synchronous, hence no busy/locked/error plumbing.
function AppearanceSection() {
  const [pref, setPref] = useThemePref();
  return (
    <section className="prefs-section">
      <h2>Appearance</h2>
      <p className="deploy-muted">
        Light or dark for this app. Stored in this browser profile, so each browser and the
        desktop window remember their own choice. Applies immediately.
      </p>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "system"}
          onChange={() => setPref("system")}
        />
        <span>
          <b>System</b> — follows your desktop appearance, including a scheduled day/night
          switch.
        </span>
      </label>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "light"}
          onChange={() => setPref("light")}
        />
        <span>
          <b>Light</b> — always light, whatever your desktop is set to.
        </span>
      </label>
      <label className="prefs-radio">
        <input
          type="radio"
          name="appearance"
          checked={pref === "dark"}
          onChange={() => setPref("dark")}
        />
        <span>
          <b>Dark</b> — always dark, whatever your desktop is set to.
        </span>
      </label>
    </section>
  );
}

function DeployToggle({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.deploy.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putDeployEnabled(!enabled));
      // The sidebar's signed-in dot (useDeployEnabled) is mounted alongside
      // this page, not remounted by navigation — without this it would only
      // pick up the flip on the next focus/visibility return.
      notifyPrefsChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <label className="prefs-radio">
        <input type="checkbox" checked={enabled} disabled={busy} onChange={toggle} />
        <span>
          <b>Show the Deploy button</b> on renderable pages. Deploy publishes a page to a
          public hosted URL through the <code>fused</code> CLI.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

function ReaderToggle({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.reader.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putReaderEnabled(!enabled));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <label className="prefs-radio">
        <input type="checkbox" checked={enabled} disabled={busy} onChange={toggle} />
        <span>
          <b>Reader (listen to files)</b>. Adds a Reader mode to text files and PDFs that reads
          them aloud.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </>
  );
}

function AccessibilitySection({
  prefs,
  onChange,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}) {
  return (
    <section className="prefs-section">
      <h2>Accessibility</h2>
      <ReaderToggle prefs={prefs} onChange={onChange} />
    </section>
  );
}

// Human labels for the short model names the server accepts. Keyed off the
// server's `choices` list rather than hardcoding the options, so the page can
// never offer a value a PUT would reject — an unknown name still renders (as
// itself) instead of vanishing from a control the user has one of selected.
const MODEL_LABELS: Record<string, string> = {
  "": "Automatic",
  fable: "Fable",
  opus: "Opus",
  sonnet: "Sonnet",
  haiku: "Haiku (fastest)",
};

function ModelSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <section className="prefs-section">
      <h2>Default model</h2>
      <p className="deploy-muted">
        Which Claude model this app reaches for when nothing else has said. It preselects the
        chat's model chip and picks the model behind <code>fused.ai</code>. A model chosen in a
        chat, or one a page passes to <code>fused.ai</code> itself, still wins — this only
        answers when nobody asked. <b>Automatic</b> leaves each to its own default.
      </p>
      <div className="prefs-field">
        <label>
          Model{" "}
          <select
            value={prefs.model.default}
            disabled={busy}
            onChange={async (e) => {
              const next = e.target.value as Prefs["model"]["default"];
              setBusy(true);
              setError(null);
              try {
                onChange(await putDefaultModel(next));
              } catch (err) {
                setError((err as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {prefs.model.choices.map((m) => (
              <option key={m} value={m}>
                {MODEL_LABELS[m] ?? m}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

// The retention window as the "Currently keeping ..." line says it, matching
// the select's own option labels so the forced value reads like a choice.
function describeRetention(days: number): string {
  if (days === 0) return "until the size cap";
  return days === 1 ? "1 day" : `${days} days`;
}

function CallLogSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const calls = prefs.calls;
  // Same shape as the Engine section's `locked`: a non-null raw env value means
  // the process overrides the pref, so the control is shown but not actionable.
  // Non-null is the server's assertion that the variable is actually IN FORCE,
  // not merely set — it withholds the value when the writer ignores it (an empty
  // or non-numeric retention window, say). So never re-derive this from the
  // value's shape here: a client-side "is it a number?" check is the second copy
  // of a rule the writer already owns, and lockout is what it costs to get wrong.
  const enabledLocked = calls.enabled_forced_by !== null;
  const retentionLocked = calls.retention_forced_by !== null;

  const apply = async (fn: () => Promise<Prefs>) => {
    setBusy(true);
    setError(null);
    try {
      onChange(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Call log</h2>
      <p className="deploy-muted">
        Records every API call your pages make — each <code>runPython</code>, <code>readFile</code>,{" "}
        <code>stat</code> and <code>writeFile</code>, with its duration, result size, output and
        any traceback. A page with recorded calls gains a <b>Calls</b> view mode showing charts and
        a per-target breakdown; <code>fused-render calls</code> reads the same log from a terminal.
      </p>
      {/* The checkbox shows the STORED pref and the muted line below shows what
          is actually in force, exactly as the Engine section does: the control
          reflects the choice you made (and what a PUT round-trips), the line
          reports reality. They diverge whenever FUSED_RENDER_CALLS wins, and
          the control is disabled then so the discrepancy can't be acted on. */}
      <label className="prefs-radio">
        <input
          type="checkbox"
          checked={calls.enabled}
          disabled={busy || enabledLocked}
          onChange={() => apply(() => putCallsEnabled(!calls.enabled))}
        />
        <span>
          <b>Record API calls</b> made by pages rendered in this app.
        </span>
      </label>
      <div className="deploy-muted">
        Currently <b>{calls.effective_enabled ? "recording" : "not recording"}</b>
        {enabledLocked && (
          <>
            {" "}
            — locked by <code>FUSED_RENDER_CALLS={calls.enabled_forced_by}</code> for this process;
            the checkbox applies once the variable is removed.
          </>
        )}
      </div>
      <div className="prefs-field">
        <label>
          Parameters{" "}
          {/* Gated on what is actually recording, not on the stored pref —
              otherwise an env-forced off state leaves these live, and an
              env-forced on state greys them out while calls are landing. */}
          <select
            value={calls.params}
            disabled={busy || !calls.effective_enabled}
            onChange={(e) => apply(() => putCallsParamsMode(e.target.value as CallsParamsMode))}
          >
            <option value="full">Record values</option>
            <option value="keys">Record names only</option>
            <option value="off">Record nothing</option>
          </select>
        </label>
        <p className="deploy-muted">
          A run's parameters are usually the whole repro, so they are recorded by default — they
          are already visible in the URL. Switch to names-only if a page passes a secret as a
          parameter.
        </p>
      </div>
      <div className="prefs-field">
        <label>
          Keep for{" "}
          <select
            value={String(calls.retention_days)}
            disabled={busy || !calls.effective_enabled || retentionLocked}
            onChange={(e) => apply(() => putCallsRetentionDays(Number(e.target.value)))}
          >
            <option value="1">1 day</option>
            <option value="7">7 days</option>
            <option value="14">14 days</option>
            <option value="90">90 days</option>
            <option value="0">Until the size cap</option>
          </select>
        </label>
        {retentionLocked && (
          <p className="deploy-muted">
            Currently keeping <b>{describeRetention(calls.effective_retention_days)}</b> — locked by{" "}
            <code>FUSED_RENDER_CALLS_RETENTION_DAYS={calls.retention_forced_by}</code> for this
            process; the choice above applies once the variable is removed.
          </p>
        )}
      </div>
      <p className="deploy-muted">
        Stored at <code>{calls.dir}</code>
        {calls.dir_exists ? "." : " — no calls recorded yet, so the folder does not exist."}
      </p>
      {/* Navigates IN-APP, not to the OS file manager: the explorer is how you
          reach the Calls view — open the folder, click a .calls.jsonl, and it
          renders in the same viewer the mode switcher offers.

          Disabled until the store exists: the writer creates it on its first
          append, so browsing beforehand navigates to a path that fails to stat
          — an error card where the answer is simply "nothing has run yet",
          which is also the answer to "why has no page got a Calls mode?". */}
      <button
        type="button"
        disabled={!calls.dir_exists}
        title={calls.dir_exists ? undefined : "No calls have been recorded yet"}
        onClick={() => navigate(calls.dir, { isDir: true })}
      >
        Browse call logs
      </button>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

// One capability's engine: a radio per backend, plus Automatic.
//
// Radios rather than a select, matching the Appearance section: the options
// each need a sentence beside them (what the backend is like, or why it cannot
// run here) and a <select> has nowhere to put one. It also makes the DISABLED
// case work — a greyed-out option in a dropdown is invisible until you open it,
// while a greyed-out radio with its reason next to it explains itself in place,
// which is the same idiom the Call log section uses for an env-locked control.
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
  const [changed, setChanged] = useState<string | null>(null);
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
      onChange(await putEngineForCapability(row.capability, code));
      setChanged(consequential ? code : null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="prefs-field">
      <h3>{capabilityLabel(row.capability)}</h3>
      <label className="prefs-radio">
        <input
          type="radio"
          name={`engine-${row.capability}`}
          checked={row.selected === auto}
          disabled={busy}
          onChange={() => choose(auto)}
        />
        <span>
          <b>Automatic</b>
        </span>
      </label>
      {row.choices.map((choice) => {
        // ONLY for a disabled option. A radio that cannot be clicked and says
        // nothing about why is worse than the verbosity being cut here, and the
        // sentence is the registry's — the page cannot know it. An available
        // engine gets its name and nothing else: what it is LIKE ("transcribes
        // on the GPU") is editorial, and this is a settings page.
        const reason = choice.available ? null : choiceReason(choice);
        return (
          <label className="prefs-radio" key={choice.code}>
            <input
              type="radio"
              name={`engine-${row.capability}`}
              checked={row.selected === choice.code}
              // Unavailable backends are shown and not offered. Hidden, a user
              // on a Windows machine would have no way to learn that the MLX
              // path exists and why it is not for them.
              disabled={busy || !choice.available}
              onChange={() => choose(choice.code)}
            />
            <span>
              <b>{choice.label}</b>
              {reason ? ` — ${reason}` : ""}
            </span>
          </label>
        );
      })}
      <div className="deploy-muted">{servingLine(row)}</div>
      {warning && <div className="deploy-muted">{warning}</div>}
      {/* The consequence, in four words. It stays because an unload is a real
          thing that just happened to the user's machine and nothing else on
          screen would report it — but the paragraph explaining WHY the model
          was unloaded and how suggestion lists work was an essay after a radio
          click. */}
      {changed && <div className="deploy-muted">Switched. Loaded model unloaded.</div>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

function EnginesPanel({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  return (
    <section className="prefs-section">
      <h2>Inference engines</h2>
      {/* One line. An earlier draft explained which backend wins on which
          platform and what a switch costs; a settings page states what a
          control does, and the rest is an essay the reader did not open this
          tab for. What survives is only what cannot be inferred from the
          controls themselves — that the choice is per capability, and what
          Automatic means. */}
      <p className="deploy-muted">
        Which backend runs local models. <b>Automatic</b> picks the best one this machine
        can run.
      </p>
      {prefs.engines.capabilities.map((row) => (
        <CapabilityEngineRow
          key={row.capability}
          row={row}
          auto={prefs.engines.auto}
          onChange={onChange}
        />
      ))}
    </section>
  );
}

function DeploymentsSection({
  prefs,
  onChange,
  onOpenAccount,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
  onOpenAccount: () => void;
}) {
  return (
    <section className="prefs-section">
      <h2>Deploy to Fused account</h2>
      <DeployToggle prefs={prefs} onChange={onChange} />
      {prefs.deploy.enabled && (
        <p className="deploy-muted">
          The per-environment share list (every deployed mount, with Revoke) lives on the{" "}
          <button type="button" className="link-button" onClick={onOpenAccount}>
            Fused account tab
          </button>{" "}
          beside your environments.
        </p>
      )}
    </section>
  );
}

export default function Preferences() {
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

  // Requested tab lives in the URL (`?tab=account`) — bookmarkable, and how
  // the Deploy modal and the old `/view/_account` redirect (App.tsx) land
  // here directly on the account tab. Falls back to "render" whenever the
  // account tab wouldn't be offered (Deploy not enabled) rather than showing
  // a tab with no button pointing at it.
  const requested = new URLSearchParams(location.search).get("tab");
  const requestedTab: PrefsTab =
    requested === "account"
      ? "account"
      : requested === "indexing"
        ? "indexing"
        : requested === "engines"
          ? "engines"
          : "render";
  const tab: PrefsTab =
    requestedTab === "account" && !prefs?.deploy.enabled ? "render" : requestedTab;
  const setTab = (next: PrefsTab) => {
    const params = new URLSearchParams(location.search);
    if (next === "render") params.delete("tab");
    else params.set("tab", next);
    const search = params.toString();
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };

  return (
    <div className="prefs-page">
      {/* Page names itself — the topbar that used to carry "Preferences" is
          gone (settings pages render chrome-free). */}
      <h1 className="prefs-title">Preferences</h1>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!prefs && !error && <SkeletonLines rows={4} label="Loading preferences" />}
      {prefs && (
        <>
          <div className="prefs-tabs">
            <button
              type="button"
              className={"prefs-tab" + (tab === "render" ? " active" : "")}
              onClick={() => setTab("render")}
            >
              Render preferences
            </button>
            {/* Indexing — the file index behind the explorer's search. Always
                present: unlike the account tab it needs no opt-in, and a user
                looking for "why is search finding/missing this" has nowhere
                else to go. */}
            <button
              type="button"
              className={"prefs-tab" + (tab === "indexing" ? " active" : "")}
              onClick={() => setTab("indexing")}
            >
              Indexing
            </button>
            {/* Inference engines — which local-model backend serves each
                capability. Always present for the same reason Indexing is: a
                machine with only one usable backend per capability still
                benefits from being able to SEE that, and it is where "why did
                the suggested models change?" is answered. */}
            <button
              type="button"
              className={"prefs-tab" + (tab === "engines" ? " active" : "")}
              onClick={() => setTab("engines")}
            >
              Inference engines
            </button>
            {prefs.deploy.enabled && (
              <button
                type="button"
                className={"prefs-tab" + (tab === "account" ? " active" : "")}
                onClick={() => setTab("account")}
              >
                Fused account
              </button>
            )}
          </div>
          <div className="prefs-tabpanel">
            {tab === "render" && (
              <>
                <AppearanceSection />
                <ModelSection prefs={prefs} onChange={setPrefs} />
                <CallLogSection prefs={prefs} onChange={setPrefs} />
                <DeploymentsSection
                  prefs={prefs}
                  onChange={setPrefs}
                  onOpenAccount={() => setTab("account")}
                />
                <AccessibilitySection prefs={prefs} onChange={setPrefs} />
              </>
            )}
            {tab === "engines" && <EnginesPanel prefs={prefs} onChange={setPrefs} />}
            {tab === "indexing" && <IndexingPanel />}
            {tab === "account" && prefs.deploy.enabled && <AccountPanel />}
          </div>
        </>
      )}
    </div>
  );
}
