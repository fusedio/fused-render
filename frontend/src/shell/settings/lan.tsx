// Phone & network: sharing your apps with phones on the same Wi-Fi (lan.py).
// Off by default and this is the only place it turns on. While the listener
// is up the row shows the QR code a phone scans to pair — the ONLY way in: no
// PIN, no approval dialog — and the devices that have, each revocable.
import { useEffect, useState } from "react";
import qrcode from "qrcode-generator";
import { getLanDevices, getLanPairToken, putLanEnabled, revokeAllLanDevices, revokeLanDevice, type LanDevice } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { RowSwitch, SettingRow, type PrefControlProps } from "./SettingRow";

export function LanControl({ prefs, onChange }: PrefControlProps) {
  // What the click asked for, held until the PUT answers. Turning sharing on
  // binds the listener, issues a certificate and announces two mDNS names
  // before the response comes back — seconds in which a switch that sat still
  // read as a dead page.
  const [pending, setPending] = useState<boolean | null>(null);
  const busy = pending !== null;
  const [error, setError] = useState<string | null>(null);
  const lan = prefs.lan;
  const enabled = lan?.enabled ?? false;
  const running = enabled && !!lan?.running;
  // Enough of the listener is up to pair a device: the http one (browsers and
  // the app) or, on its own, the https one (the app alone).
  const pairable = enabled && ((running && !!lan?.url) || !!lan?.https_url);
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

  // While the QR is on screen, watch for the phone to pair: the list grows,
  // and the spent code is replaced with a fresh one (tokens are single-use).
  useEffect(() => {
    if (!pairable) return;
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
  }, [pairable]);

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
    <SettingRow
      id="lan"
      status={
        busy ? (
          <span className="lan-working" role="status">
            <span className="lan-spinner" aria-hidden="true" />
            {pending
              ? "Starting the listener and announcing this computer on the Wi-Fi…"
              : "Stopping and taking this computer off the Wi-Fi…"}
          </span>
        ) : enabled ? (
          "Plain http: on iPhone the live microphone and clipboard paste stay off, and on an open (password-less) network the pairing cookie travels in the clear."
        ) : undefined
      }
      // The switch follows the click, not the round trip.
      control={<RowSwitch id="lan" checked={pending ?? enabled} disabled={busy} onChange={(v) => void toggle(v)} />}
    >
      {/* A code as long as SOMETHING is listening: with http down but https
          up, a browser cannot get in but the iPhone app still pairs. */}
      {pairable && <LanPairing url={lan!.url ?? lan!.https_url!} deviceCount={devices.length} />}
      {pairable && <LanDevices devices={devices} onRevoke={revoke} />}
      {enabled && !lan?.running && lan?.https_url && (
        <ErrorBanner>
          {`Browsers can't reach this computer${lan.error ? `: ${lan.error}` : " (the http listener is down)"}. ` +
            "The iPhone app can still pair with the code above."}
        </ErrorBanner>
      )}
      {enabled && !lan?.running && !lan?.https_url && (
        <ErrorBanner>{lan?.error ? `Not sharing: ${lan.error}` : "Starting…"}</ErrorBanner>
      )}
      {/* Up while a piece of it failed (no zeroconf, no address, https down):
          that must not hide behind a working QR. */}
      {enabled && lan?.running && lan.error && <ErrorBanner>{`Sharing, but: ${lan.error}`}</ErrorBanner>}
      {enabled && lan?.running && !lan.error && lan.tls_error && (
        <ErrorBanner>{`The app's https listener is down (browsers unaffected): ${lan.tls_error}`}</ErrorBanner>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// The QR code: a pairing URL good for five minutes and ONE device. A new one
// replaces it the moment a pairing lands (the device count changes) and when
// the old one runs out.
function LanPairing({ url, deviceCount }: { url: string; deviceCount: number }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [ipUrl, setIpUrl] = useState<string | null>(null);
  // Why there is no code, when there is none: the mint can answer 503
  // (nothing is listening), which is worth reading.
  const [problem, setProblem] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setNonce((n) => n + 1);
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
        setIpUrl(t.ip_url);
        // Rotate just before the server forgets this one.
        timer = window.setTimeout(() => alive && setNonce((n) => n + 1), Math.max(5, t.ttl_s - 5) * 1000);
      })
      .catch((e) => {
        if (!alive) return;
        setSvg(null);
        setProblem((e as Error).message || "No pairing code — the listener did not answer.");
      });
    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [nonce]);

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
          <b>Scan from the Fused Render app</b> (or the iPhone's Camera app — not the Control Center scanner, whose
          in-app browser can't pair Safari). Each code pairs one device; a new code appears right after, and every
          five minutes. A paired phone then opens{" "}
          <a href={url} target="_blank" rel="noreferrer">
            {url}
          </a>
          .
        </p>
        <Button variant="outline" size="sm" onClick={() => setNonce((n) => n + 1)}>
          New code
        </Button>
        {ipUrl && (
          <p className="settings-row-desc mt-2">
            If the phone can't resolve the name, open this once instead: <code className="break-all">{ipUrl}</code>
          </p>
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
  if (!devices.length) return <p className="settings-row-desc">No paired devices yet.</p>;
  return (
    <div className="lan-devices">
      <div className="lan-devices-head">
        <b>Paired devices</b>
        <Button variant="outline" size="sm" onClick={() => onRevoke(null)}>
          Forget all
        </Button>
      </div>
      <ul>
        {devices.map((d) => (
          <li key={d.id}>
            <span className="lan-device-name">{d.name}</span>
            <span className="deploy-muted">
              paired {agoLabel(d.paired_at)} · seen {agoLabel(d.last_seen)}
            </span>
            <Button variant="outline" size="sm" onClick={() => onRevoke(d.id)}>
              Revoke
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
