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
//
// Visual language: Flow (see .claude/skills/flow-design-language). Every
// section is a run of dense property rows — words left, control right — built
// from shell/prefs/SettingRow.tsx; controls are the shadcn primitives.
import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import {
  getPrefs,
  putCallsEnabled,
  putCallsParamsMode,
  putCallsRetentionDays,
  cancelHfLogin,
  getHfAuth,
  hfLogout,
  putCanvasesEnabled,
  putLanEnabled,
  getLanPairToken,
  getLanDevices,
  revokeLanDevice,
  revokeAllLanDevices,
  putDefaultModel,
  putReaderEnabled,
  startHfLogin,
} from "@platform/lib/api";
import qrcode from "qrcode-generator";
import { publishCanvasesEnabled } from "@apps/canvases/feature-flag";
import type { CallsParamsMode, HfAuth, LanDevice, Prefs } from "@platform/lib/api";
import { navigate, navigateUrl } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { THEME_PREF_LABELS, THEME_PREFS, useThemePref } from "@platform/lib/theme";
import type { ThemePref } from "@platform/lib/theme";
import { Button, buttonVariants } from "@platform/shadcn/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@platform/shadcn/ui/select";
import { Switch } from "@platform/shadcn/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { EntityList, EntityRow } from "@platform/ui/flow/EntityRow";
import { Muted, Page, PageBody, PageHeader } from "@platform/ui/flow/Typography";
import { IndexingPanel } from "@shell/Indexing";
import { Code, SettingRow, SettingRows, SettingsSection } from "@shell/prefs/SettingRow";

type PrefsTab = "render" | "ai" | "indexing" | "lan";
const TABS: readonly PrefsTab[] = ["render", "ai", "indexing", "lan"];

// The one section on this page that is deliberately NOT server-backed. Every
// other control here round-trips /api/prefs (shell/prefs.py); Appearance is
// per-browser-profile localStorage["fused-render:theme"] by decision — SPEC §30
// AP-1 / D134 — so a browser tab and the desktop window can legitimately hold
// different choices, and there is no server store to keep in sync. Writes are
// synchronous, hence no busy/locked/error plumbing.
function AppearanceSection() {
  const [pref, setPref] = useThemePref();
  const id = useId();
  return (
    <SettingsSection
      title="Appearance"
      description="Stored in this browser profile, so each browser and the desktop window remember their own choice. Applies immediately."
    >
      <SettingRows>
        <SettingRow
          label="Theme"
          controlId={id}
          description={
            pref === "system"
              ? "Follows your desktop appearance, including a scheduled day/night switch."
              : `Always ${pref}, whatever your desktop is set to.`
          }
        >
          <ToggleGroup
            id={id}
            aria-label="Theme"
            variant="outline"
            size="sm"
            spacing={0}
            value={[pref]}
            // A click on the active item yields [] — keep the current choice.
            onValueChange={(v) => {
              const next = v[0] as ThemePref | undefined;
              if (next) setPref(next);
            }}
          >
            {THEME_PREFS.map((p) => (
              <ToggleGroupItem key={p} value={p} aria-label={THEME_PREF_LABELS[p]}>
                {THEME_PREF_LABELS[p]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </SettingRow>
      </SettingRows>
    </SettingsSection>
  );
}

// A server-backed on/off pref: one Switch, local busy/error, a PUT that
// returns the full Prefs which the parent re-renders from.
function SwitchRow({
  label,
  description,
  note,
  checked,
  disabled,
  onToggle,
}: {
  label: ReactNode;
  description?: ReactNode;
  note?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  onToggle: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onToggle();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <SettingRow
      label={label}
      description={description}
      controlId={id}
      note={
        <>
          {note}
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </>
      }
    >
      <Switch id={id} checked={checked} disabled={busy || disabled} onCheckedChange={() => void toggle()} />
    </SettingRow>
  );
}

function AccessibilitySection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  return (
    <SettingsSection title="Accessibility">
      <SettingRows>
        <SwitchRow
          label="Reader (listen to files)"
          description="Adds a Reader mode to text files and PDFs that reads them aloud."
          checked={prefs.reader.enabled}
          onToggle={async () => onChange(await putReaderEnabled(!prefs.reader.enabled))}
        />
      </SettingRows>
    </SettingsSection>
  );
}

// Canvases: off by default, and this is the only place it can be turned on
// (D427). One section rather than a tab — a tab for one switch is a tab a
// reader opens once — and it sits on this tab because "which of this app's
// features do I want" is the question this tab already answers twice
// (Reader above, call recording below).
function CanvasesSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  return (
    <SettingsSection
      title="Canvases"
      description="Canvases are Fused Workbench canvases opened locally: a listing of the canvases on your account and a per-canvas workspace with the live workbench embedded, editing the same UDFs. Off by default — turn it on and it appears in the sidebar (once you are signed in to Fused) and in this Settings menu."
    >
      <SettingRows>
        <SwitchRow
          label="Show Canvases"
          description="In the sidebar and the Settings menu."
          checked={prefs.canvases.enabled}
          onToggle={async () => {
            const next = await putCanvasesEnabled(!prefs.canvases.enabled);
            onChange(next);
            // The sidebar is mounted beside this page and reads the same flag from
            // its own store; hand it the fresh answer so the row and the Settings
            // entry appear (or go) with the switch rather than on the next
            // navigation. See @apps/canvases/feature-flag for why it is a publish and
            // not a poll.
            publishCanvasesEnabled(next.canvases.enabled);
          }}
        />
      </SettingRows>
    </SettingsSection>
  );
}

// Local-network sharing (lan.py): off by default, this is the only place it
// turns on. Same one-switch section shape as Canvases above. While the
// listener is up it shows the QR code a phone scans to pair (the ONLY way in —
// no PIN, no approval dialog), and the devices that have, with revoke.
function LanSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [error, setError] = useState<string | null>(null);
  const lan = prefs.lan;
  const enabled = lan?.enabled ?? false;
  const running = enabled && !!lan?.running;
  const [devices, setDevices] = useState<LanDevice[]>(lan?.devices ?? []);

  // While the QR is on screen, watch for the phone to pair: the list grows,
  // and the spent code is replaced with a fresh one (tokens are single-use).
  useEffect(() => {
    if (!running) return;
    let alive = true;
    const tick = async () => {
      try {
        const { devices: next } = await getLanDevices();
        if (!alive) return;
        setDevices((prev) => (prev.length === next.length && prev.every((d, i) => d.id === next[i].id) ? prev : next));
      } catch {
        /* the next tick retries */
      }
    };
    const id = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [running]);

  const revoke = async (id: string | null) => {
    setError(null);
    try {
      const { devices: next } = id ? await revokeLanDevice(id) : await revokeAllLanDevices();
      setDevices(next);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <SettingsSection
      title="Share on local network"
      description={
        <>
          Open your apps — everything under <Code>~/Fused</Code> and every linked folder — from a phone on
          the same Wi-Fi. Only devices you pair by scanning the code below get in; a paired device can open
          and run those apps and read or change their files, and nothing else on this computer is reachable.
          Plain http: on iPhone the live microphone and clipboard paste stay off, and on an open
          (password-less) network the pairing cookie travels in the clear.
        </>
      }
    >
      <SettingRows>
        <SwitchRow
          label="Share my apps"
          description="On this network."
          checked={enabled}
          onToggle={async () => {
            const next = await putLanEnabled(!enabled);
            onChange(next);
            setDevices(next.lan?.devices ?? []);
          }}
        />
      </SettingRows>
      {running && lan.url && <LanPairing url={lan.url} deviceCount={devices.length} />}
      {running && <LanDevices devices={devices} onRevoke={revoke} />}
      {enabled && !lan?.running && <ErrorBanner>{lan?.error ? `Not sharing: ${lan.error}` : "Starting…"}</ErrorBanner>}
      {/* The listener can be up while a piece of it failed — zeroconf missing
          (no render.fused.local name), no network address, or the https
          listener down. Those must not hide behind a working QR. */}
      {enabled && lan?.running && lan.error && <ErrorBanner>{`Sharing, but: ${lan.error}`}</ErrorBanner>}
      {enabled && lan?.running && !lan.error && lan.tls_error && (
        <ErrorBanner>{`The app's https listener is down (browsers unaffected): ${lan.tls_error}`}</ErrorBanner>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingsSection>
  );
}

// The QR code: a pairing URL good for five minutes and ONE device. A new one
// replaces it the moment a pairing lands (the device count changes — the
// section polls it) and when the old one runs out.
function LanPairing({ url, deviceCount }: { url: string; deviceCount: number }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [ipUrl, setIpUrl] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setNonce((n) => n + 1);
  }, [deviceCount]);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;
    getLanPairToken()
      .then((t) => {
        if (!alive) return;
        const qr = qrcode(0, "M");
        qr.addData(t.url);
        qr.make();
        setSvg(qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true }));
        setIpUrl(t.ip_url);
        // Rotate just before the server forgets this one.
        timer = window.setTimeout(() => alive && setNonce((n) => n + 1), Math.max(5, t.ttl_s - 5) * 1000);
      })
      .catch(() => alive && setSvg(null));
    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [nonce]);

  return (
    <div className="flex items-start gap-5 border border-border rounded-lg bg-card p-4">
      {/* A QR is read by a camera: always dark-on-white, in BOTH themes, with a
          white quiet zone around it — the one deliberate raw colour on this
          page (the token sheet has no "paper" semantic). */}
      <div
        className="shrink-0 size-42 p-2.5 bg-white rounded-md [&_svg]:block [&_svg]:size-full"
        aria-label="Pairing QR code"
        dangerouslySetInnerHTML={{ __html: svg ?? "" }}
      />
      <div className="min-w-0 flex-1 space-y-2.5 text-sm">
        <p>
          <b>Scan from the Fused Render app</b> (or the iPhone's Camera app — not the Control Center scanner,
          whose in-app browser can't pair Safari). Each code pairs one device; a new code appears right
          after, and every five minutes. A paired phone then opens{" "}
          <a href={url} target="_blank" rel="noreferrer" className="underline underline-offset-3 hover:text-foreground">
            {url}
          </a>
          .
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => setNonce((n) => n + 1)}>
          New code
        </Button>
        {ipUrl && (
          <Muted className="text-xs">
            If the phone can't resolve the name, open this once instead: <Code>{ipUrl}</Code>
          </Muted>
        )}
      </div>
    </div>
  );
}

function agoLabel(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function LanDevices({ devices, onRevoke }: { devices: LanDevice[]; onRevoke: (id: string | null) => void }) {
  if (!devices.length) {
    return <Muted>No paired devices yet.</Muted>;
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">Paired devices</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => onRevoke(null)}>
          Forget all
        </Button>
      </div>
      <EntityList>
        {devices.map((d) => (
          <EntityRow
            key={d.id}
            title={d.name}
            meta={`paired ${agoLabel(d.paired_at)} · seen ${agoLabel(d.last_seen)}`}
            trailing={
              <Button type="button" variant="ghost" size="sm" onClick={() => onRevoke(d.id)}>
                Revoke
              </Button>
            }
          />
        ))}
      </EntityList>
    </div>
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

// A right-hand Select for a settings row. `items` gives base-ui the labels so
// the trigger shows "7 days", not "7". Values are strings; the empty string is
// a legitimate choice here ("Automatic"), so it is passed through as-is.
function RowSelect({
  id,
  value,
  items,
  disabled,
  onChange,
  className,
}: {
  id?: string;
  value: string;
  items: { value: string; label: string }[];
  disabled?: boolean;
  onChange: (v: string) => void;
  className?: string;
}) {
  return (
    <Select items={items} value={value} disabled={disabled} onValueChange={(v) => v != null && onChange(v)}>
      <SelectTrigger id={id} size="sm" className={cn("min-w-40", className)}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {items.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function ModelSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  return (
    <SettingsSection
      title="Default model"
      description={
        <>
          Which Claude model this app reaches for when nothing else has said. It preselects the chat's model
          chip and picks the model behind <Code>fused.ai</Code>. A model chosen in a chat, or one a page passes
          to <Code>fused.ai</Code> itself, still wins — this only answers when nobody asked. <b>Automatic</b>{" "}
          leaves each to its own default.
        </>
      }
    >
      <SettingRows>
        <SettingRow label="Model" controlId={id} note={error && <ErrorBanner>{error}</ErrorBanner>}>
          <RowSelect
            id={id}
            value={prefs.model.default}
            disabled={busy}
            items={prefs.model.choices.map((m) => ({ value: m, label: MODEL_LABELS[m] ?? m }))}
            onChange={async (v) => {
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
          />
        </SettingRow>
      </SettingRows>
    </SettingsSection>
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
    <SettingsSection
      title="Hugging Face"
      description={
        <>
          Sign in to download AI models. Without an account the Hub serves this machine anonymously, meaning
          a lower rate limit, slower downloads, and no access to gated or private repos. Signing in hands the
          token to <Code>huggingface_hub</Code>, which stores it the same way <Code>hf auth login</Code> does.
        </>
      }
    >
      {!auth && !error && <SkeletonLines rows={2} label="Loading Hugging Face status" />}
      {auth && (
        <SettingRows>
          {auth.pending ? (
            <SettingRow
              label="Waiting for you to authorize"
              description={
                <>
                  {/* The code is shown as well as embedded in that link: the link
                      carries it, but the Hub asks for confirmation, and somebody who
                      opened the page in a different browser needs to type it. */}
                  If asked for a code, enter <Code>{auth.pending.userCode}</Code>. This code expires in{" "}
                  {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
                </>
              }
              note={auth.error && <ErrorBanner>{auth.error}</ErrorBanner>}
            >
              <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void act(cancelHfLogin)}>
                Cancel
              </Button>
              <a
                className={buttonVariants({ size: "sm" })}
                href={auth.pending.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Authorize on huggingface.co
              </a>
            </SettingRow>
          ) : (
            <SettingRow
              label={
                auth.signedIn ? (
                  <>
                    Signed in{auth.account ? <> as <b>{auth.account}</b></> : null}
                  </>
                ) : (
                  "Not signed in"
                )
              }
              /* No sentence under EITHER ordinary state, because the controls
                 already are the state: "Signed in as X" beside a Log out button
                 says it, and so does a bare Log in button — and the paragraph
                 above already says what anonymous costs, so repeating it here was
                 the same fact twice, shorter. The one case that needs words is the
                 one no control can show: a variable overriding hf's store, where
                 the button is present and would change nothing. */
              description={
                locked && (
                  <>
                    Using the token in <Code>{auth.forcedByVar}</Code> from this app&apos;s environment — hf
                    reads that ahead of its own store, so signing in here would change nothing until the
                    variable is removed.
                  </>
                )
              }
              /* The last attempt's failure: denied, expired, or the network. Kept
                 until the next attempt replaces it, so a login that failed while
                 the user was authorizing in another tab can still say why. */
              note={auth.error && <ErrorBanner>{auth.error}</ErrorBanner>}
            >
              {auth.signedIn ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="text-destructive"
                  disabled={busy || locked}
                  onClick={() => void act(hfLogout)}
                >
                  Log out
                </Button>
              ) : (
                <Button type="button" size="sm" disabled={busy || locked} onClick={() => void act(() => startHfLogin())}>
                  Log in to Hugging Face
                </Button>
              )}
            </SettingRow>
          )}
        </SettingRows>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingsSection>
  );
}

// The retention window as the "Currently keeping ..." line says it, matching
// the select's own option labels so the forced value reads like a choice.
function describeRetention(days: number): string {
  if (days === 0) return "until the size cap";
  return days === 1 ? "1 day" : `${days} days`;
}

const PARAMS_ITEMS: { value: CallsParamsMode; label: string }[] = [
  { value: "full", label: "Record values" },
  { value: "keys", label: "Record names only" },
  { value: "off", label: "Record nothing" },
];

const RETENTION_ITEMS = [
  { value: "1", label: "1 day" },
  { value: "7", label: "7 days" },
  { value: "14", label: "14 days" },
  { value: "90", label: "90 days" },
  { value: "0", label: "Until the size cap" },
];

function CallLogSection({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const calls = prefs.calls;
  const ids = { enabled: useId(), params: useId(), retention: useId() };
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
    <SettingsSection
      title="Call log"
      description={
        <>
          Records every API call your pages make — each <Code>runPython</Code>, <Code>readFile</Code>,{" "}
          <Code>stat</Code> and <Code>writeFile</Code>, with its duration, result size, output and any
          traceback. A page with recorded calls gains a <b>Calls</b> view mode showing charts and a per-target
          breakdown; <Code>fused-render calls</Code> reads the same log from a terminal.
        </>
      }
    >
      <SettingRows>
        {/* The switch shows the STORED pref and the note under it shows what
            is actually in force, exactly as the Engine section does: the control
            reflects the choice you made (and what a PUT round-trips), the line
            reports reality. They diverge whenever FUSED_RENDER_CALLS wins, and
            the control is disabled then so the discrepancy can't be acted on. */}
        <SettingRow
          label="Record API calls"
          description="Made by pages rendered in this app."
          controlId={ids.enabled}
          note={
            <>
              Currently <b>{calls.effective_enabled ? "recording" : "not recording"}</b>
              {enabledLocked && (
                <>
                  {" "}
                  — locked by <Code>FUSED_RENDER_CALLS={calls.enabled_forced_by}</Code> for this process; the
                  switch applies once the variable is removed.
                </>
              )}
            </>
          }
        >
          <Switch
            id={ids.enabled}
            checked={calls.enabled}
            disabled={busy || enabledLocked}
            onCheckedChange={() => void apply(() => putCallsEnabled(!calls.enabled))}
          />
        </SettingRow>
        {/* Gated on what is actually recording, not on the stored pref —
            otherwise an env-forced off state leaves these live, and an
            env-forced on state greys them out while calls are landing. */}
        <SettingRow
          label="Parameters"
          controlId={ids.params}
          description="A run's parameters are usually the whole repro, so they are recorded by default — they are already visible in the URL. Switch to names-only if a page passes a secret as a parameter."
        >
          <RowSelect
            id={ids.params}
            value={calls.params}
            items={PARAMS_ITEMS}
            disabled={busy || !calls.effective_enabled}
            onChange={(v) => void apply(() => putCallsParamsMode(v as CallsParamsMode))}
          />
        </SettingRow>
        <SettingRow
          label="Keep for"
          controlId={ids.retention}
          note={
            retentionLocked && (
              <>
                Currently keeping <b>{describeRetention(calls.effective_retention_days)}</b> — locked by{" "}
                <Code>FUSED_RENDER_CALLS_RETENTION_DAYS={calls.retention_forced_by}</Code> for this process; the
                choice applies once the variable is removed.
              </>
            )
          }
        >
          <RowSelect
            id={ids.retention}
            value={String(calls.retention_days)}
            items={RETENTION_ITEMS}
            disabled={busy || !calls.effective_enabled || retentionLocked}
            onChange={(v) => void apply(() => putCallsRetentionDays(Number(v)))}
          />
        </SettingRow>
        <SettingRow
          label="Stored at"
          description={
            <>
              <Code>{calls.dir}</Code>
              {calls.dir_exists ? "" : " — no calls recorded yet, so the folder does not exist."}
            </>
          }
        >
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
            size="sm"
            disabled={!calls.dir_exists}
            title={calls.dir_exists ? undefined : "No calls have been recorded yet"}
            onClick={() => navigate(calls.dir, { isDir: true })}
          >
            Browse call logs
          </Button>
        </SettingRow>
      </SettingRows>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingsSection>
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
  const tab: PrefsTab = (TABS as readonly string[]).includes(requested ?? "") ? (requested as PrefsTab) : "render";
  const setTab = (next: PrefsTab) => {
    const params = new URLSearchParams(location.search);
    if (next === "render") params.delete("tab");
    else params.set("tab", next);
    const search = params.toString();
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };

  return (
    <Page>
      {/* Page names itself — the topbar that used to carry "Preferences" is
          gone (settings pages render chrome-free). */}
      <PageHeader title="Preferences" />
      <PageBody className="max-w-3xl">
        {error && <ErrorBanner>{error}</ErrorBanner>}
        {!prefs && !error && <SkeletonLines rows={4} label="Loading preferences" />}
        {prefs && (
          <Tabs value={tab} onValueChange={(v) => setTab(v as PrefsTab)}>
            <TabsList variant="line" className="w-full justify-start border-b border-border pb-1.5">
              <TabsTrigger value="render" className="flex-none">
                Render preferences
              </TabsTrigger>
              {/* AI — which model, and with whose credentials (D403). Named for
                  the subject rather than for the two controls in it, so adding a
                  third does not rename the tab. */}
              <TabsTrigger value="ai" className="flex-none">
                AI
              </TabsTrigger>
              {/* Indexing — the file index behind the explorer's search. The TAB
                  is always present — a user looking for "why is search
                  finding/missing this" has nowhere else to go — even though
                  indexing itself now has an opt-out toggle inside it
                  (`indexing_enabled`): the panel is where that answer lives,
                  on or off. */}
              <TabsTrigger value="indexing" className="flex-none">
                Indexing
              </TabsTrigger>
              {/* Render local network — sharing apps with phones on the Wi-Fi
                  (lan.py): the switch, the pairing QR and the paired devices.
                  Its own tab because pairing is a task you come here to DO with
                  a phone in hand, not a setting you glance at. */}
              <TabsTrigger value="lan" className="flex-none">
                Render local network
              </TabsTrigger>
            </TabsList>
            <TabsContent value="render" className="space-y-8 pt-4">
              <AppearanceSection />
              <CallLogSection prefs={prefs} onChange={setPrefs} />
              <AccessibilitySection prefs={prefs} onChange={setPrefs} />
              <CanvasesSection prefs={prefs} onChange={setPrefs} />
            </TabsContent>
            <TabsContent value="lan" className="space-y-8 pt-4">
              <LanSection prefs={prefs} onChange={setPrefs} />
            </TabsContent>
            <TabsContent value="ai" className="space-y-8 pt-4">
              <ModelSection prefs={prefs} onChange={setPrefs} />
              <HuggingFaceSection />
            </TabsContent>
            <TabsContent value="indexing" className="space-y-8 pt-4">
              <IndexingPanel prefs={prefs} onChange={setPrefs} />
            </TabsContent>
          </Tabs>
        )}
      </PageBody>
    </Page>
  );
}
