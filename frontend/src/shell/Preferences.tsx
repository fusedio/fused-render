// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Its tabs (D125), the count deliberately
// not stated here since it has been wrong three times:
//   Render preferences — Appearance, Default model (which Claude model the
//     chat and fused.ai reach for when nothing else has said), Hugging Face
//     (signing in to the Hub for model downloads — and NOT a preference: the
//     token belongs to huggingface_hub, which stores it, so that section talks
//     to /api/hf/* and holds no state of this page's), Call log
//     (capture/redaction/retention for
//     fused_render/calls.py), and Accessibility. Always present; the
//     default (clean URL). No Tour button — the tour still runs itself on a
//     first visit (App.tsx's maybeAutoStartTour); it is onboarding, not a
//     preference. The app's OWN log is not here either: it is disposable
//     temp-dir output (D68) reached from the desktop tray's "Open app logs", and a
//     second "Logs" heading next to the Call log section only ever read as the
//     call log's own settings.
// **Inference engines used to be a tab here and is not any more** — it is the
// Engines tab of /ai-models (shell/AiModelsEngines.tsx). It was the one control
// on this page about MODELS rather than about rendering, and every consequence
// of changing it — which cached models can be loaded, what their engine tags
// say, what Discover suggests — is on that page, where the question it answers
// is actually asked. `/preferences?tab=engines` is rewritten to the new url in
// `platform/lib/router.rewriteLegacyUrl`, so an old bookmark still lands on the
// control rather than on this page's default tab.
// Deliberately NOT a third tab: the Claude Config panel (apps/claude_config)
// briefly sat here, and a settings page hosting a second settings app — with
// its own section nav and scroll containers — inside one of its tabs never read
// as one page. It has its own sidebar routes now (shell/GlobalSidebar).
// The active tab lives in the URL (`?tab=indexing`), same pattern as
// Templates' bindings/library tabs.
// Template bindings live in the dedicated /view/_templates view.
import { useEffect, useState } from "react";
import {
  getPrefs,
  putCallsEnabled,
  putCallsParamsMode,
  putCallsRetentionDays,
  cancelHfLogin,
  getHfAuth,
  hfLogout,
  putDefaultModel,
  putReaderEnabled,
  startHfLogin,
} from "@platform/lib/api";
import type { CallsParamsMode, HfAuth, Prefs } from "@platform/lib/api";
import { navigate, navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { useThemePref } from "@platform/lib/theme";
import { IndexingPanel } from "@shell/Indexing";

type PrefsTab = "render" | "indexing";

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

// Signing in to Hugging Face (server/routers/hf_auth.py, D394).
//
// **No token passes through this component in either direction.** The button
// starts huggingface_hub's own device-code login; the user authorizes on
// huggingface.co; hf stores the result — with a refresh token it renews itself —
// and every consumer (model downloads inside a worker, the Discover search)
// reads it back through `get_token()`. So there is no box to paste a secret
// into, nothing to mask, and nothing for this page to persist. Someone who
// needs a specific fine-grained token exports HF_TOKEN instead, which hf reads
// ahead of its own store and which this section reports as being in force.
//
// The page POLLS while a login is pending rather than holding a request open:
// the thing being waited for is a person going to another tab, which can take
// as long as it takes, and hf's device code lives for ~15 minutes.
function HuggingFaceSection() {
  const [auth, setAuth] = useState<HfAuth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getHfAuth()
      .then((a) => alive && setAuth(a))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  // One poll loop, armed only while a login is actually in flight — a settings
  // page must not sit on a timer for a flow nobody started.
  const pending = auth?.pending ?? null;
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const id = setInterval(() => {
      getHfAuth()
        .then((a) => alive && setAuth(a))
        .catch(() => undefined); // a blip mid-login is not worth a banner
    }, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [pending !== null]);

  const act = async (fn: () => Promise<HfAuth>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setAuth(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const locked = auth?.forcedByVar != null;
  return (
    <section className="prefs-section">
      <h2>Hugging Face</h2>
      <p className="deploy-muted">
        Sign in to download AI models. Without an account the Hub serves this machine
        anonymously — a lower rate limit, slower downloads, and no access to gated or private
        repos. Signing in hands the token to <code>huggingface_hub</code>, which stores it the
        same way <code>hf auth login</code> does and keeps it renewed; this app never holds it.
      </p>
      {!auth && !error && <SkeletonLines rows={2} label="Loading Hugging Face status" />}
      {auth && (
        <>
          {auth.pending ? (
            <div className="prefs-field">
              <p>
                <a
                  className="btn btn-primary hf-authorize-link"
                  href={auth.pending.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Authorize on huggingface.co
                </a>
              </p>
              {/* The code is shown as well as embedded in that link: the link
                  carries it, but the Hub asks for confirmation, and somebody who
                  opened the page in a different browser needs to type it. */}
              <p className="deploy-muted">
                Waiting for you to authorize. If asked for a code, enter{" "}
                <code>{auth.pending.userCode}</code>. This code expires in{" "}
                {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
              </p>
              <div className="prefs-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy}
                  onClick={() => void act(cancelHfLogin)}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="prefs-actions">
              {auth.signedIn ? (
                <>
                  <span>
                    Signed in{auth.account ? <> as <b>{auth.account}</b></> : null}
                  </span>
                  <button
                    type="button"
                    className="btn btn-danger-text"
                    disabled={busy || locked}
                    onClick={() => void act(hfLogout)}
                  >
                    Log out
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || locked}
                  onClick={() => void act(() => startHfLogin())}
                >
                  Log in to Hugging Face
                </button>
              )}
            </div>
          )}
          <div className="deploy-muted">
            {locked ? (
              <>
                Using the token in <code>{auth.forcedByVar}</code> from this app&apos;s
                environment — hf reads that ahead of its own store, so signing in here would
                change nothing until the variable is removed.
              </>
            ) : auth.signedIn ? (
              <>Model downloads and Hub search use this account.</>
            ) : (
              <>Not signed in — requests to the Hub go out anonymously.</>
            )}
          </div>
          {/* The last attempt's failure: denied, expired, or the network. Kept
              until the next attempt replaces it, so a login that failed while
              the user was authorizing in another tab can still say why. */}
          {auth.error && <ErrorBanner>{auth.error}</ErrorBanner>}
        </>
      )}
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

  // Requested tab lives in the URL (`?tab=indexing`) — bookmarkable.
  const requested = new URLSearchParams(location.search).get("tab");
  // `?tab=engines` never reaches here: `rewriteLegacyUrl` sends it to
  // /ai-models?tab=engines before this page renders, which is why an unknown
  // tab falling back to "render" is not the answer for that one — a bookmark
  // pointing at the engine picker should land ON the engine picker.
  const tab: PrefsTab = requested === "indexing" ? "indexing" : "render";
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
                present: needs no opt-in, and a user looking for "why is search
                finding/missing this" has nowhere else to go. */}
            <button
              type="button"
              className={"prefs-tab" + (tab === "indexing" ? " active" : "")}
              onClick={() => setTab("indexing")}
            >
              Indexing
            </button>
          </div>
          <div className="prefs-tabpanel">
            {tab === "render" && (
              <>
                <AppearanceSection />
                <ModelSection prefs={prefs} onChange={setPrefs} />
                <HuggingFaceSection />
                <CallLogSection prefs={prefs} onChange={setPrefs} />
                <AccessibilitySection prefs={prefs} onChange={setPrefs} />
              </>
            )}
            {tab === "indexing" && <IndexingPanel />}
          </div>
        </>
      )}
    </div>
  );
}
