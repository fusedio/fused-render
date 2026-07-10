// Connectors page — the /view/_connectors sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/connectors.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
import { useEffect, useState } from "react";
import {
  createConnector,
  createRemote,
  deleteConnector,
  getConnectors,
  mountConnector,
  putConnectorAutomount,
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
        <label
          className="deploy-muted"
          style={{ fontSize: "0.85em", display: "flex", gap: 4, alignItems: "center" }}
          title="Mount automatically when the server starts"
        >
          <input
            type="checkbox"
            checked={conn.automount}
            disabled={busy}
            onChange={(e) => act(() => putConnectorAutomount(conn.id, e.target.checked))}
          />
          automount
        </label>
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
        A connector mounts an rclone remote as a local folder. Mount a specific{" "}
        <b>bucket/prefix</b>, not a whole bucket — narrow mounts browse and search much faster
        (every folder listed inside a mount is a remote API call).
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input
          placeholder="name (e.g. sensor-data)"
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
          placeholder="bucket/prefix (recommended)"
          style={{ minWidth: 220 }}
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
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      await createRemote(name, {
        access_key_id: accessKey,
        secret_access_key: secretKey,
        endpoint,
        region,
      });
      setName("");
      setAccessKey("");
      setSecretKey("");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Add rclone remote</h2>
      <p className="deploy-muted">
        A remote holds the credentials; connectors mount paths inside it. Keys are written
        straight into rclone's own config — fused-render never stores them. S3-compatible
        storage can be set up here; for <b>Google Drive</b> and other sign-in-based backends,
        run <code>rclone config</code> in a terminal instead, then the remote appears in the
        dropdown above on reload.
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input placeholder="remote name" value={name} onChange={(e) => setName(e.target.value)} />
        <input
          placeholder="endpoint (blank for AWS S3)"
          style={{ minWidth: 260 }}
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
        <button type="button" disabled={busy || !name} onClick={add}>
          {busy ? "Creating…" : "Create remote"}
        </button>
      </div>
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
        Remote storage mounted as local folders
        {state.rclone.version ? ` (${state.rclone.version})` : ""}. The <b>first</b> open of a
        large remote file downloads what it needs and can be slow; repeat opens are served from
        a local cache and are fast. Mounts stay up until you unmount them — including across
        restarts.
      </p>
      {state.connectors.length === 0 ? (
        <p className="deploy-muted">No connectors yet.</p>
      ) : (
        state.connectors.map((c) => <ConnectorRow key={c.id} conn={c} onChanged={reload} />)
      )}
      {state.rclone.available ? (
        <>
          <AddConnector remotes={state.rclone.remotes} onChanged={reload} />
          <AddRemote onChanged={reload} />
        </>
      ) : (
        <p className="deploy-muted">
          Connectors need{" "}
          <a href="https://rclone.org/install/" target="_blank" rel="noreferrer">
            rclone
          </a>{" "}
          (<code>brew install rclone</code> on macOS, your distro's package on Linux). Install
          it and reload this page.
        </p>
      )}
    </div>
  );
}
