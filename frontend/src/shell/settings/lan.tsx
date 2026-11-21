// Phone access: sharing your apps with the Fused Render app on a phone on the
// same Wi-Fi (lan.py). Off by default and this is the only place it turns on.
// While the listener is up the row shows the QR code the app scans to pair —
// the ONLY way in: no PIN, no approval dialog — the devices that have, each
// revocable, and whatever the computer itself can see that would keep a phone
// from reaching it (lan_doctor.py: no address, a VPN, the firewall, the local
// name not resolving, a rename since pairing). While a code is on screen and
// no phone arrives, a short checklist unfolds: that is the one moment the
// computer knows a phone is trying and failing.
import { useEffect, useState } from "react";
import qrcode from "qrcode-generator";
import {
  getLanDevices,
  getLanPairToken,
  getPrefs,
  putLanEnabled,
  putLanRemote,
  revokeAllLanDevices,
  revokeLanDevice,
  setLanDeviceRemote,
  type LanDevice,
  type LanWarning,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { RowSwitch, SettingRow, type PrefControlProps } from "./SettingRow";

/** How long a code may sit on screen with no pairing before the checklist
 *  unfolds. Long enough to find the phone and open the app; short enough
 *  that someone stuck gets a hint before giving up. */
export const PAIRING_HINT_AFTER_MS = 45_000;

export function LanControl({ prefs, onChange }: PrefControlProps) {
  // What the click asked for, held until the PUT answers. Turning sharing on
  // binds the listener and issues a certificate before the response comes
  // back — a second or two in which a switch that sat still read as a dead page.
  const [pending, setPending] = useState<boolean | null>(null);
  const busy = pending !== null;
  const [error, setError] = useState<string | null>(null);
  const lan = prefs.lan;
  const enabled = lan?.enabled ?? false;
  const running = enabled && !!lan?.running;
  const pairable = running && !!lan?.url;
  const [devices, setDevices] = useState<LanDevice[]>(lan?.devices ?? []);
  // The page may mount on a cached snapshot and receive the fresh GET after;
  // the list follows whatever prefs the page now holds.
  const seeded = lan?.devices;
  useEffect(() => {
    if (seeded) setDevices(seeded);
  }, [seeded]);

  const toggle = async (want: boolean) => {
    if (busy) return;
    setPending(want);
    setError(null);
    try {
      const next = await putLanEnabled(want);
      onChange(next);
      setDevices(next.lan?.devices ?? []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(null);
    }
  };

  // While the QR is on screen, watch for the phone to pair: the list grows
  // (and each row's last-seen moves), and the spent code is replaced with a
  // fresh one (tokens are single-use). Every fourth tick re-reads prefs too,
  // so the computer-side warnings and the name follow a firewall or network
  // fix without reopening the panel.
  useEffect(() => {
    if (!pairable) return;
    let alive = true;
    let ticks = 0;
    const tick = async () => {
      ticks += 1;
      try {
        if (ticks % 4 === 0) {
          const next = await getPrefs();
          if (!alive) return;
          onChange(next);
          setDevices(next.lan?.devices ?? []);
          return;
        }
        const { devices: next } = await getLanDevices();
        if (!alive) return;
        setDevices(next);
      } catch {
        /* the next tick retries */
      }
    };
    const id = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [pairable, onChange]);

  const revoke = async (id: string | null) => {
    setError(null);
    try {
      const { devices: next } = id ? await revokeLanDevice(id) : await revokeAllLanDevices();
      setDevices(next);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setAway = async (id: string, remote: boolean) => {
    setError(null);
    try {
      const { devices: next } = await setLanDeviceRemote(id, remote);
      setDevices(next);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const awayOn = enabled && !!lan?.remote_enabled;

  const warnings = (enabled && lan?.warnings) || [];

  return (
    <SettingRow
      id="lan"
      status={
        busy ? (
          <span className="lan-working" role="status">
            <span className="lan-spinner" aria-hidden="true" />
            {pending ? "Starting the listener…" : "Stopping…"}
          </span>
        ) : enabled && lan?.hostname ? (
          `Reachable as ${lan.hostname}; the phone keeps that name through Wi-Fi changes and reconnects on its own.`
        ) : undefined
      }
      // The switch follows the click, not the round trip.
      control={<RowSwitch id="lan" checked={pending ?? enabled} disabled={busy} onChange={(v) => void toggle(v)} />}
    >
      {pairable && <LanPairing url={lan!.url!} deviceCount={devices.length} warnings={warnings} />}
      {pairable && <LanDevices devices={devices} onRevoke={revoke} away={awayOn} onAway={setAway} />}
      {enabled && !running && <ErrorBanner>{lan?.error ? `Not sharing: ${lan.error}` : "Starting…"}</ErrorBanner>}
      {/* Up while a piece of it is off (no address): that must not hide
          behind a working QR. */}
      {running && lan?.error && <ErrorBanner>{`Sharing, but: ${lan.error}`}</ErrorBanner>}
      {running && <LanWarnings warnings={warnings} />}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// What the computer can see on its own side (lan_doctor.py), one banner each.
function LanWarnings({ warnings }: { warnings: LanWarning[] }) {
  if (!warnings.length) return null;
  return (
    <div className="lan-warnings">
      {warnings.map((w) => (
        <ErrorBanner key={w.code}>{w.message}</ErrorBanner>
      ))}
    </div>
  );
}

// The QR code: a pairing URL good for five minutes and ONE device. A new one
// replaces it the moment a pairing lands (the device count changes) and when
// the old one runs out.
function LanPairing({ url, deviceCount, warnings }: { url: string; deviceCount: number; warnings: LanWarning[] }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  // Why there is no code, when there is none: the mint can answer 503
  // (nothing is listening), which is worth reading.
  const [problem, setProblem] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  // A code has been on screen this long with no phone arriving: unfold the
  // checklist (reset by every pairing, i.e. every device-count change).
  const [stuck, setStuck] = useState(false);

  useEffect(() => {
    setNonce((n) => n + 1);
    setStuck(false);
  }, [deviceCount]);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;
    setProblem(null);
    getLanPairToken()
      .then((t) => {
        if (!alive) return;
        const qr = qrcode(0, "M");
        qr.addData(t.url);
        qr.make();
        setSvg(qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true }));
        setLink(t.url);
        // Rotate just before the server forgets this one.
        timer = window.setTimeout(() => alive && setNonce((n) => n + 1), Math.max(5, t.ttl_s - 5) * 1000);
      })
      .catch((e) => {
        if (!alive) return;
        setSvg(null);
        setLink(null);
        setProblem((e as Error).message || "No pairing code — the listener did not answer.");
      });
    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [nonce]);

  useEffect(() => {
    const id = window.setTimeout(() => setStuck(true), PAIRING_HINT_AFTER_MS);
    return () => window.clearTimeout(id);
  }, [deviceCount]);

  return (
    <div className="lan-pair">
      <div className="lan-pair-qr" aria-label="Pairing QR code" aria-busy={!svg && !problem}>
        {svg ? (
          <span dangerouslySetInnerHTML={{ __html: svg }} />
        ) : problem ? null : (
          <span className="lan-spinner lan-spinner-qr" aria-hidden="true" />
        )}
      </div>
      <div className="lan-pair-text">
        {problem && <ErrorBanner>{problem}</ErrorBanner>}
        <p>
          <b>Scan from the Fused Render app</b> on the phone (Pair, then point the camera at this code). Each code
          pairs one phone; a new code appears right after, and every five minutes. The phone then opens{" "}
          <code className="break-all">{url}</code>.
        </p>
        <Button variant="outline" size="sm" onClick={() => setNonce((n) => n + 1)}>
          New code
        </Button>
        {link && (
          <p className="settings-row-desc mt-2">
            Or paste this link into the app's Pair screen: <code className="break-all">{link}</code>
          </p>
        )}
        {stuck && <PairingChecklist warnings={warnings} />}
      </div>
    </div>
  );
}

// Shown after PAIRING_HINT_AFTER_MS with no pairing: the phone-side causes the
// computer cannot see, plus a pointer at its own warnings when it has any.
function PairingChecklist({ warnings }: { warnings: LanWarning[] }) {
  return (
    <div className="lan-checklist settings-row-desc mt-2" role="status">
      <b>Phone not getting through?</b>
      <ul>
        <li>Both on the same Wi-Fi (guest and hotel networks often keep devices from seeing each other).</li>
        <li>On the phone: Settings → Fused Render → Local Network must be on.</li>
        <li>The app shows its own reason under the top bar when it cannot reach this computer.</li>
        {warnings.length > 0 && <li>This computer reports {warnings.length === 1 ? "a problem" : "problems"} of its own, listed below.</li>}
      </ul>
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

/** A phone seen within this window is getting through right now. */
const REACHABLE_WITHIN_S = 15 * 60;

const VIA_LABEL: Record<string, string> = { lan: "on Wi-Fi", tailscale: "via Tailscale", relay: "via relay" };

function LanDevices({
  devices,
  onRevoke,
  away,
  onAway,
}: {
  devices: LanDevice[];
  onRevoke: (id: string | null) => void;
  away: boolean;
  onAway: (id: string, remote: boolean) => void;
}) {
  if (!devices.length) return <p className="settings-row-desc">No paired phones yet.</p>;
  return (
    <div className="lan-devices">
      <div className="lan-devices-head">
        <b>Paired phones</b>
        <Button variant="outline" size="sm" onClick={() => onRevoke(null)}>
          Forget all
        </Button>
      </div>
      <ul>
        {devices.map((d) => {
          const reachable = Date.now() / 1000 - d.last_seen < REACHABLE_WITHIN_S;
          return (
            <li key={d.id}>
              <span className="lan-device-name">{d.name}</span>
              <span className="deploy-muted">
                {reachable ? `reachable, seen ${agoLabel(d.last_seen)}` : `last seen ${agoLabel(d.last_seen)}`}
                {d.last_via && d.last_via !== "lan" ? ` ${VIA_LABEL[d.last_via] ?? ""}` : ""} · paired{" "}
                {agoLabel(d.paired_at)}
              </span>
              {away && (
                // Per phone: limit one to the Wi-Fi while the others roam.
                <label className="lan-device-away" title="Let this phone in away from the Wi-Fi">
                  <Switch
                    className="settings-switch"
                    checked={d.remote !== false}
                    onCheckedChange={(v) => onAway(d.id, !!v)}
                    aria-label={`Away access for ${d.name}`}
                  />
                  Away
                </label>
              )}
              <Button variant="outline" size="sm" onClick={() => onRevoke(d.id)}>
                Revoke
              </Button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Away access (tunnel/, docs/REMOTE_ACCESS.md): a child of Phone access. On,
// paired phones reach this computer off the Wi-Fi through the relay (the
// computer dials out; the relay passes the phone's pinned TLS through blind)
// and, when the computer is on a tailnet, over Tailscale. The relay setting
// is `host[:port][#sha256]` — what the relay prints at startup.
export function LanRemoteControl({ prefs, onChange }: PrefControlProps) {
  const lan = prefs.lan;
  const enabled = !!lan?.remote_enabled;
  const remote = lan?.remote;
  const [pending, setPending] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState(lan?.relay_setting ?? "");
  const stored = lan?.relay_setting ?? "";
  useEffect(() => setDraft(stored), [stored]);

  const put = async (body: { lan_remote_enabled?: boolean; lan_relay?: string }) => {
    setError(null);
    try {
      onChange(await putLanRemote(body));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const toggle = async (want: boolean) => {
    setPending(want);
    await put({ lan_remote_enabled: want });
    setPending(null);
  };

  // The agent connects a moment after the switch: follow it until it settles.
  const relay = remote?.relay;
  const settling = enabled && !!relay && !relay.connected && !relay.error;
  useEffect(() => {
    if (!settling) return;
    let alive = true;
    const id = window.setInterval(async () => {
      try {
        const next = await getPrefs();
        if (alive) onChange(next);
      } catch {
        /* the next tick retries */
      }
    }, 1500);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [settling, onChange]);

  const ts = remote?.tailscale;
  const status = !enabled
    ? undefined
    : relay?.connected
      ? `Reachable from anywhere through the relay as ${relay.host}${ts?.host ? `, and on Tailscale as ${ts.host}` : ""}.`
      : ts?.host
        ? `Reachable on your Tailscale network as ${ts.host}.`
        : relay
          ? "Connecting to the relay…"
          : "No relay set and no Tailscale network found — add a relay below.";

  return (
    <SettingRow
      id="lan-remote"
      status={status}
      control={<RowSwitch id="lan-remote" checked={pending ?? enabled} disabled={pending !== null} onChange={(v) => void toggle(v)} />}
    >
      {enabled && (
        <div className="lan-remote">
          <form
            className="lan-remote-relay"
            onSubmit={(e) => {
              e.preventDefault();
              void put({ lan_relay: draft.trim() });
            }}
          >
            <label htmlFor="lan-relay-input">Relay</label>
            <Input
              id="lan-relay-input"
              value={remote?.relay_from_env ? (remote.relay_setting ?? "") : draft}
              disabled={!!remote?.relay_from_env}
              title={remote?.relay_from_env ? "Set by FUSED_RENDER_RELAY" : undefined}
              placeholder="relay.example.com:8443"
              spellCheck={false}
              autoCapitalize="off"
              onChange={(e) => setDraft(e.target.value)}
            />
            <Button type="submit" variant="outline" size="sm" disabled={!!remote?.relay_from_env || draft.trim() === stored}>
              Save
            </Button>
          </form>
          <p className="settings-row-desc">
            Phones pair on the Wi-Fi first (scan the code above); after that they connect from anywhere. Traffic stays
            encrypted end to end with this computer's own certificate — the relay sees only which computer a phone is
            asking for. A self-hosted relay prints the exact setting to paste here when it starts.
          </p>
          {relay?.error && <ErrorBanner>{`Relay: ${relay.error}`}</ErrorBanner>}
          {remote?.error && <ErrorBanner>{remote.error}</ErrorBanner>}
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}
