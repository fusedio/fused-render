// PROTOTYPE — Connectors page (/view/_connectors sentinel, sidebar footer).
// Throwaway UI over shell/connectors_prototype.py: manage rclone-backed
// remote mounts (GDrive / S3-compatible) that appear as local paths. Once
// mounted, "Open" navigates into the mountpoint and every existing view
// (Listing/Preview/readers/tile servers) works untouched — that's the
// feasibility question this page exists to answer. Delete when answered.
import { useEffect, useState } from "react";
import {
  createConnector,
  createRemote,
  deleteConnector,
  getConnectors,
  mountConnector,
  unmountConnector,
} from "../lib/api";
import type { Connector, ConnectorsResult } from "../lib/api";
import { navigate } from "../lib/router";

function ConnectorRow({
  conn,
  onChanged,
}: {
  conn: Connector;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="prefs-section" style={{ paddingTop: 12, paddingBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span
          title={conn.mounted ? "Mounted" : "Not mounted"}
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: conn.mounted ? "#3fb950" : "#8b949e",
            flexShrink: 0,
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <b>{conn.name}</b>{" "}
          <code className="deploy-muted" style={{ fontSize: "0.85em" }}>
            {conn.remote}
          </code>
          <div className="deploy-muted" style={{ fontSize: "0.8em" }}>
            {conn.mountpoint}
          </div>
        </div>
        {conn.mounted ? (
          <>
            <button type="button" disabled={busy} onClick={() => navigate(conn.mountpoint)}>
              Open
            </button>
            <button type="button" disabled={busy} onClick={() => act(() => unmountConnector(conn.id))}>
              Unmount
            </button>
          </>
        ) : (
          <button type="button" disabled={busy} onClick={() => act(() => mountConnector(conn.id))}>
            Mount
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => act(() => deleteConnector(conn.id))}>
          Delete
        </button>
      </div>
      {error && <div className="deploy-error">{error}</div>}
    </div>
  );
}

function AddConnector({
  remotes,
  onChanged,
}: {
  remotes: string[];
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [subpath, setSubpath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      await createConnector(name, remote + subpath);
      setName("");
      setSubpath("");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Add connector</h2>
      <p className="deploy-muted">
        A connector mounts an rclone remote as a local folder. Pick a configured
        remote (and optionally a bucket/folder inside it), give it a name, and
        it appears under <code>~/.fused-render/mounts/</code>.
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input
          placeholder="name (e.g. my-drive)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select value={remote} onChange={(e) => setRemote(e.target.value)}>
          <option value="">— remote —</option>
          {remotes.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <input
          placeholder="bucket/prefix (optional)"
          value={subpath}
          onChange={(e) => setSubpath(e.target.value)}
        />
        <button type="button" disabled={busy || !name || !remote} onClick={add}>
          {busy ? "Mounting…" : "Add & mount"}
        </button>
      </div>
      {error && <div className="deploy-error">{error}</div>}
    </section>
  );
}

function AddRemote({ onChanged }: { onChanged: () => void }) {
  const [kind, setKind] = useState<"s3" | "drive">("s3");
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const add = async () => {
    setBusy(true);
    setError(null);
    if (kind === "drive") {
      setNotice("A browser tab should open for Google sign-in — approve it there.");
    }
    try {
      await createRemote(
        name,
        kind,
        kind === "s3"
          ? {
              access_key_id: accessKey,
              secret_access_key: secretKey,
              endpoint,
              region,
            }
          : undefined
      );
      setName("");
      setAccessKey("");
      setSecretKey("");
      setNotice(null);
      onChanged();
    } catch (e) {
      setNotice(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Add rclone remote</h2>
      <p className="deploy-muted">
        Credentials live in rclone's own config, never in fused-render. S3 keys
        are written straight through; Google Drive runs rclone's OAuth flow in
        your browser.
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select value={kind} onChange={(e) => setKind(e.target.value as "s3" | "drive")}>
          <option value="s3">S3-compatible</option>
          <option value="drive">Google Drive</option>
        </select>
        <input placeholder="remote name" value={name} onChange={(e) => setName(e.target.value)} />
        {kind === "s3" && (
          <>
            <input
              placeholder="endpoint (e.g. https://…r2.cloudflarestorage.com)"
              style={{ minWidth: 280 }}
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
            />
            <input placeholder="region (optional)" value={region} onChange={(e) => setRegion(e.target.value)} />
            <input placeholder="access key id" value={accessKey} onChange={(e) => setAccessKey(e.target.value)} />
            <input
              placeholder="secret access key"
              type="password"
              value={secretKey}
              onChange={(e) => setSecretKey(e.target.value)}
            />
          </>
        )}
        <button type="button" disabled={busy || !name} onClick={add}>
          {busy ? (kind === "drive" ? "Waiting for OAuth…" : "Creating…") : "Create remote"}
        </button>
      </div>
      {notice && <div className="deploy-muted">{notice}</div>}
      {error && <div className="deploy-error">{error}</div>}
    </section>
  );
}

export default function Connectors() {
  const [state, setState] = useState<ConnectorsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = () => {
    getConnectors().then(setState, (e: Error) => setLoadError(e.message));
  };
  useEffect(reload, []);

  if (loadError) {
    return <div className="status-message error">Failed to load connectors: {loadError}</div>;
  }
  if (!state) {
    return <div className="status-message">Loading…</div>;
  }

  return (
    <div className="prefs-page">
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        <b>PROTOTYPE.</b> Remote storage mounted as local folders via rclone
        {state.rclone.version ? ` (${state.rclone.version})` : ""}. Everything
        downstream — previews, readers, tile servers — sees ordinary local paths.
      </p>
      {state.connectors.length === 0 ? (
        <p className="deploy-muted">No connectors yet.</p>
      ) : (
        state.connectors.map((c) => <ConnectorRow key={c.id} conn={c} onChanged={reload} />)
      )}
      {state.rclone.available && (
        <>
          <AddConnector remotes={state.rclone.remotes} onChanged={reload} />
          <AddRemote onChanged={reload} />
        </>
      )}
      {!state.rclone.available && (
        <p className="deploy-muted">
          For S3-compatible and other object storage, install{" "}
          <a href="https://rclone.org/install/" target="_blank" rel="noreferrer">rclone</a>{" "}
          (<code>brew install rclone</code>) and reload — mount forms appear here.
        </p>
      )}
    </div>
  );
}
