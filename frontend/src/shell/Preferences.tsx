// Preferences page (SPEC §20) — the `/view/_prefs` sentinel route, entered
// from the sidebar's bottom-left gear. Its tabs (D125), the count deliberately
// not stated here since it has been wrong three times:
//   Render preferences — Appearance, Call log (capture/redaction/retention for
//     fused_render/calls.py), Accessibility, and Canvases (the feature switch
//     the shell's Canvases entry points read — D427). Always present; the
//     default (clean URL). No Tour button — the tour still runs itself on a
//     first visit (App.tsx's maybeAutoStartTour); it is onboarding, not a
//     preference. The app's OWN log is not here either: it is disposable
//     temp-dir output (D68) reached from the desktop tray's "Open app logs", and a
//     second "Logs" heading next to the Call log section only ever read as the
//     call log's own settings.
//   AI — Default model (which Claude model the chat and fused.ai reach for when
//     nothing else has said) and Hugging Face (signing in to the Hub for model
//     downloads — and NOT a preference: the token belongs to huggingface_hub,
//     which stores it, so that section talks to /api/hf/* and holds no state of
//     this page's). Both moved off the Render tab (D403): neither is about
//     rendering, which is the same reason inference engines left this page
//     entirely, and a reader looking for either was reading past four sections
//     that answer a different question. Grouped rather than each given a tab
//     because they are one question asked twice — which model, and with whose
//     credentials.
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
  putCanvasesEnabled,
  putDefaultModel,
  putReaderEnabled,
  startHfLogin,
} from "@platform/lib/api";
import { publishCanvasesEnabled } from "@apps/canvases/feature-flag";
import type { CallsParamsMode, HfAuth, Prefs } from "@platform/lib/api";
import { navigate, navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { RadioGroup, RadioGroupItem } from "@platform/shadcn/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { useThemePref } from "@platform/lib/theme";
import { IndexingPanel } from "@shell/Indexing";

type PrefsTab = "render" | "ai" | "indexing";

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
      <p className="text-xs text-muted-foreground">
        Light or dark for this app. Stored in this browser profile, so each browser and the
        desktop window remember their own choice. Applies immediately.
      </p>
      <RadioGroup
        value={pref}
        onValueChange={(v) => setPref(v as typeof pref)}
        aria-label="Appearance"
      >
        <label className="prefs-radio">
          <RadioGroupItem value="system" />
          <span>
            <b>System</b> — follows your desktop appearance, including a scheduled day/night
            switch.
          </span>
        </label>
        <label className="prefs-radio">
          <RadioGroupItem value="light" />
          <span>
            <b>Light</b> — always light, whatever your desktop is set to.
          </span>
        </label>
        <label className="prefs-radio">
          <RadioGroupItem value="dark" />
          <span>
            <b>Dark</b> — always dark, whatever your desktop is set to.
          </span>
        </label>
      </RadioGroup>
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
        <Checkbox checked={enabled} disabled={busy} onCheckedChange={toggle} />
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

// Canvases: off by default, and this is the only place it can be turned on
// (D427). One section rather than a tab — a tab for one checkbox is a tab a
// reader opens once — and it sits on this tab because "which of this app's
// features do I want" is the question this tab already answers twice
// (Reader above, call recording below).
function CanvasesSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.canvases.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await putCanvasesEnabled(!enabled);
      onChange(next);
      // The sidebar is mounted beside this page and reads the same flag from
      // its own store; hand it the fresh answer so the row and the Settings
      // entry appear (or go) with the checkbox rather than on the next
      // navigation. See @apps/canvases/feature-flag for why it is a publish and
      // not a poll.
      publishCanvasesEnabled(next.canvases.enabled);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Canvases</h2>
      <p className="text-xs text-muted-foreground">
        Canvases are Fused Workbench canvases opened locally: a listing of the canvases on your
        account and a per-canvas workspace with the live workbench embedded, editing the same
        UDFs. Off by default — turn it on and it appears in the sidebar (once you are signed in to
        Fused) and in this Settings menu.
      </p>
      <label className="prefs-radio">
        <Checkbox checked={enabled} disabled={busy} onCheckedChange={toggle} />
        <span>
          <b>Show Canvases</b> in the sidebar and the Settings menu.
        </span>
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
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
      <p className="text-xs text-muted-foreground">
        Which Claude model this app reaches for when nothing else has said. It preselects the
        chat's model chip and picks the model behind <code>fused.ai</code>. A model chosen in a
        chat, or one a page passes to <code>fused.ai</code> itself, still wins — this only
        answers when nobody asked. <b>Automatic</b> leaves each to its own default.
      </p>
      <div className="prefs-field">
        <label>
          Model{" "}
          <Select
            value={prefs.model.default}
            disabled={busy}
            onValueChange={async (v) => {
              const next = v as Prefs["model"]["default"];
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
            <SelectTrigger>
              <SelectValue>
                {(v: string) => MODEL_LABELS[v] ?? v}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {prefs.model.choices.map((m) => (
                <SelectItem key={m} value={m}>
                  {MODEL_LABELS[m] ?? m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

// Signing in to Hugging Face (server/routers/hf_auth.py, D402).
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
      <p className="text-xs text-muted-foreground">
        Sign in to download AI models. Without an account the Hub serves this machine
        anonymously, meaning a lower rate limit, slower downloads, and no access to gated or
        private repos. Signing in hands the token to <code>huggingface_hub</code>, which stores
        it the same way <code>hf auth login</code> does.
      </p>
      {!auth && !error && <SkeletonLines rows={2} label="Loading Hugging Face status" />}
      {auth && (
        <>
          {auth.pending ? (
            <div className="prefs-field">
              <p>
                <Button
                  render={
                    <a
                      href={auth.pending.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    />
                  }
                >
                  Authorize on huggingface.co
                </Button>
              </p>
              {/* The code is shown as well as embedded in that link: the link
                  carries it, but the Hub asks for confirmation, and somebody who
                  opened the page in a different browser needs to type it. */}
              <p className="text-xs text-muted-foreground">
                Waiting for you to authorize. If asked for a code, enter{" "}
                <code>{auth.pending.userCode}</code>. This code expires in{" "}
                {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
              </p>
              <div className="prefs-actions">
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy}
                  onClick={() => void act(cancelHfLogin)}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="prefs-actions">
              {auth.signedIn ? (
                <>
                  <span>
                    Signed in{auth.account ? <> as <b>{auth.account}</b></> : null}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    className="text-destructive"
                    disabled={busy || locked}
                    onClick={() => void act(hfLogout)}
                  >
                    Log out
                  </Button>
                </>
              ) : (
                <Button
                  type="button"
                  disabled={busy || locked}
                  onClick={() => void act(() => startHfLogin())}
                >
                  Log in to Hugging Face
                </Button>
              )}
            </div>
          )}
          {/* No sentence under EITHER ordinary state, because the controls
              already are the state: "Signed in as X" beside a Log out button
              says it, and so does a bare Log in button — and the paragraph
              above already says what anonymous costs, so repeating it here was
              the same fact twice, shorter. The one case that needs words is the
              one no control can show: a variable overriding hf's store, where
              the button is present and would change nothing. */}
          {locked && (
            <div className="text-xs text-muted-foreground">
              Using the token in <code>{auth.forcedByVar}</code> from this app&apos;s
              environment — hf reads that ahead of its own store, so signing in here would
              change nothing until the variable is removed.
            </div>
          )}
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
      <p className="text-xs text-muted-foreground">
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
        <Checkbox
          checked={calls.enabled}
          disabled={busy || enabledLocked}
          onCheckedChange={() => apply(() => putCallsEnabled(!calls.enabled))}
        />
        <span>
          <b>Record API calls</b> made by pages rendered in this app.
        </span>
      </label>
      <div className="text-xs text-muted-foreground">
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
          <Select
            value={calls.params}
            disabled={busy || !calls.effective_enabled}
            onValueChange={(v) => apply(() => putCallsParamsMode(v as CallsParamsMode))}
          >
            <SelectTrigger>
              <SelectValue>
                {(v: string) =>
                  ({ full: "Record values", keys: "Record names only", off: "Record nothing" })[v] ?? v
                }
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="full">Record values</SelectItem>
              <SelectItem value="keys">Record names only</SelectItem>
              <SelectItem value="off">Record nothing</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <p className="text-xs text-muted-foreground">
          A run's parameters are usually the whole repro, so they are recorded by default — they
          are already visible in the URL. Switch to names-only if a page passes a secret as a
          parameter.
        </p>
      </div>
      <div className="prefs-field">
        <label>
          Keep for{" "}
          <Select
            value={String(calls.retention_days)}
            disabled={busy || !calls.effective_enabled || retentionLocked}
            onValueChange={(v) => apply(() => putCallsRetentionDays(Number(v)))}
          >
            <SelectTrigger>
              <SelectValue>
                {(v: string) => (v === "0" ? "Until the size cap" : describeRetention(Number(v)))}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="1">1 day</SelectItem>
              <SelectItem value="7">7 days</SelectItem>
              <SelectItem value="14">14 days</SelectItem>
              <SelectItem value="90">90 days</SelectItem>
              <SelectItem value="0">Until the size cap</SelectItem>
            </SelectContent>
          </Select>
        </label>
        {retentionLocked && (
          <p className="text-xs text-muted-foreground">
            Currently keeping <b>{describeRetention(calls.effective_retention_days)}</b> — locked by{" "}
            <code>FUSED_RENDER_CALLS_RETENTION_DAYS={calls.retention_forced_by}</code> for this
            process; the choice above applies once the variable is removed.
          </p>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
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
      <Button
        type="button"
        variant="outline"
        disabled={!calls.dir_exists}
        title={calls.dir_exists ? undefined : "No calls have been recorded yet"}
        onClick={() => navigate(calls.dir, { isDir: true })}
      >
        Browse call logs
      </Button>
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
  const tab: PrefsTab =
    requested === "indexing" ? "indexing" : requested === "ai" ? "ai" : "render";
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
          {/* Tab state lives in the URL (`?tab=`) — the Tabs component is
              controlled and setTab navigates, never local state. */}
          <Tabs value={tab} onValueChange={(v) => setTab(v as PrefsTab)}>
            <TabsList>
              <TabsTrigger value="render">Render preferences</TabsTrigger>
              {/* AI — which model, and with whose credentials (D403). Named for
                  the subject rather than for the two controls in it, so adding a
                  third does not rename the tab. */}
              <TabsTrigger value="ai">AI</TabsTrigger>
              {/* Indexing — the file index behind the explorer's search. The TAB
                  is always present — a user looking for "why is search
                  finding/missing this" has nowhere else to go — even though
                  indexing itself now has an opt-out toggle inside it
                  (`indexing_enabled`): the panel is where that answer lives,
                  on or off. */}
              <TabsTrigger value="indexing">Indexing</TabsTrigger>
            </TabsList>
          </Tabs>
          <div className="prefs-tabpanel">
            {tab === "render" && (
              <>
                <AppearanceSection />
                <CallLogSection prefs={prefs} onChange={setPrefs} />
                <AccessibilitySection prefs={prefs} onChange={setPrefs} />
                <CanvasesSection prefs={prefs} onChange={setPrefs} />
              </>
            )}
            {tab === "ai" && (
              <>
                <ModelSection prefs={prefs} onChange={setPrefs} />
                <HuggingFaceSection />
              </>
            )}
            {tab === "indexing" && <IndexingPanel prefs={prefs} onChange={setPrefs} />}
          </div>
        </>
      )}
    </div>
  );
}
