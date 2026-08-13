// MCP section: the global MCP servers, listed and acted on through the
// `claude mcp` CLI — this feature never reads or writes ~/.claude.json itself,
// the CLI owns that file. Which is also why nothing here is version-controlled
// and no action refreshes the git badge.
//
// Authentication is a fire-and-forget hand-off: the CLI opens a browser and
// blocks well past any request timeout, so the module spawns it detached and the
// UI says so, then re-lists when the user confirms they're done.
import { useCallback, useId, useState } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { CliResult, McpKind, McpServer, McpStatus } from "../api";
import {
  Card,
  CardActions,
  CardSub,
  CardTitle,
  DisclosureButton,
  Empty,
  Icon,
  ListRow,
  Pill,
  SKELETON_ROWS,
  SectionToolbar,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { PillTone } from "../bits";

const STATUS: Record<McpStatus, { label: string; tone: PillTone }> = {
  connected: { label: "connected", tone: "on" },
  "needs-auth": { label: "needs auth", tone: "ro" },
  failed: { label: "failed", tone: "err" },
  pending: { label: "pending approval", tone: "neutral" },
  unknown: { label: "unknown", tone: "neutral" },
};

const GROUPS: { kind: McpKind; heading: string }[] = [
  { kind: "user", heading: "Your servers" },
  { kind: "connector", heading: "claude.ai connectors" },
  { kind: "plugin", heading: "Plugin-provided (read-only)" },
];

// Where a server came from, spelled out for the expanded row — the group
// heading says it too, but a row you opened should stand on its own.
const KIND_LABEL: Record<McpKind, string> = {
  user: "you, at user scope",
  connector: "a claude.ai connector",
  plugin: "a plugin",
};

// The CLI's stderr is the useful detail for a failed add/logout/remove; the
// module's own `error` is the fallback.
function cliError(res: CliResult, fallback: string): string {
  return res.stderr || res.error || fallback;
}

export default function McpSection() {
  const load = useCallback(() => cc.mcp.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();
  const [name, setName] = useState("");
  const [json, setJson] = useState("");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const formId = useId();

  const add = async () => {
    const n = name.trim();
    const spec = json.trim();
    if (!n || !spec) {
      toastErr("name and JSON definition required");
      return;
    }
    setBusy(true);
    try {
      const res = await cc.mcp.add(n, spec);
      if (!res.ok) {
        toastErr(cliError(res, "Add failed"));
        return;
      }
      toastOk(`Added ${n}`);
      setName("");
      setJson("");
      setAdding(false);
      reload();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const login = async (server: McpServer) => {
    const res = await guard(cc.mcp.login(server.name));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Could not launch login");
      return;
    }
    await ask<boolean>({
      title: `Authenticating "${server.name}"`,
      note: "A browser window should open to complete OAuth. Once you've approved access, click Done to update the status.",
      buttons: [{ label: "Done — Refresh", value: true, primary: true }],
    });
    reload();
  };

  const logout = async (server: McpServer) => {
    const res = await guard(cc.mcp.logout(server.name));
    if (!res) return;
    if (!res.ok) {
      toastErr(cliError(res, "Logout failed"));
      return;
    }
    toastOk(`Logged out ${server.name}`);
    reload();
  };

  const remove = async (server: McpServer) => {
    const ok = await ask<boolean>({
      title: `Remove MCP server "${server.name}"?`,
      note: "Removed through the claude CLI, at user scope.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Remove", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.mcp.remove(server.name));
    if (!res) return;
    if (!res.ok) {
      toastErr(cliError(res, "Remove failed"));
      return;
    }
    toastOk(`Removed ${server.name}`);
    reload();
  };

  const servers = data?.servers ?? [];
  const connected = servers.filter((s) => s.connected).length;

  return (
    <>
      {modal}
      <SectionToolbar
        summary={
          data?.ok ? `${servers.length} server(s) · ${connected} connected` : "…"
        }
        onRefresh={reload}
      >
        <DisclosureButton
          open={adding}
          controls={formId}
          label="Add server"
          onToggle={() => setAdding((v) => !v)}
        />
      </SectionToolbar>
      {adding && (
        <div id={formId}>
          <Card>
            <CardTitle>Add a server</CardTitle>
            <CardSub>
              Registers a user-scoped MCP server via{" "}
              <span className="cc-mono">claude mcp add-json</span>.
            </CardSub>
            <CardActions>
          <input
            className="field-control"
            aria-label="Server name"
            placeholder="name"
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="field-control cc-grow"
            aria-label="Server definition (JSON)"
            placeholder='{"type":"stdio","command":"my-mcp","args":[]}'
            value={json}
            disabled={busy}
            onChange={(e) => setJson(e.target.value)}
          />
          {/* The `Refresh` button that used to sit here is gone: re-listing the
              servers has nothing to do with adding one, and it now lives in the
              toolbar's refresh slot like every other tab's. */}
          <button type="button" className="btn btn-primary" disabled={busy} onClick={add}>
            Add
          </button>
            </CardActions>
          </Card>
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {/* The module's own refusal (the CLI missing, or a non-zero list) is a
          200 with {ok:false} — it is the section's whole content when it
          happens, so it reads as an empty state rather than a banner. */}
      {data && !data.ok && <Empty>{data.error || "Could not list MCP servers."}</Empty>}
      {!data && !error && <SkeletonLines rows={SKELETON_ROWS} label="Loading MCP servers" />}
      {data?.ok && servers.length === 0 && (
        <Empty
          action={
            <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>
              <Icon name="plus" />
              Add a server
            </button>
          }
        >
          No MCP servers configured. Add one to connect Claude to an external tool.
        </Empty>
      )}
      {data?.ok &&
        GROUPS.map(({ kind, heading }) => {
          const list = servers.filter((s) => s.kind === kind);
          if (!list.length) return null;
          return (
            <div className="cc-group" key={kind}>
              <h3 className="cc-group-title">{heading}</h3>
              {list.map((s) => {
                const st = STATUS[s.status] ?? STATUS.unknown;
                // `canAuth` says the transport COULD hold a session, not that
                // there is anything to do: a connected stdio-less server with
                // no auth state offers neither button, and the row must then
                // have no action bar at all rather than an empty one.
                const showLogin = s.canAuth && s.needsAuth;
                const showLogout = s.canAuth && s.connected;
                return (
                  <ListRow
                    key={s.name}
                    name={s.name}
                    pills={<Pill tone={st.tone}>{st.label}</Pill>}
                    secondary={s.endpoint}
                    secondaryTitle={s.endpoint}
                    secondaryMono
                    // The endpoint is the long part and the row cuts it, so the
                    // panel restates it whole — plus the transport and kind,
                    // which used to be pills competing with the status for the
                    // one thing on this tab you actually read.
                    details={
                      <dl className="cc-lrow-dl">
                        <dt className="cc-lrow-dt">Endpoint</dt>
                        <dd className="cc-lrow-dd cc-mono">{s.endpoint || "—"}</dd>
                        <dt className="cc-lrow-dt">Transport</dt>
                        <dd className="cc-lrow-dd">{s.transport}</dd>
                        <dt className="cc-lrow-dt">Status</dt>
                        <dd className="cc-lrow-dd">{st.label}</dd>
                        <dt className="cc-lrow-dt">Registered by</dt>
                        <dd className="cc-lrow-dd">{KIND_LABEL[s.kind]}</dd>
                      </dl>
                    }
                    actions={
                      <>
                        {showLogin && (
                          <button type="button" className="btn" onClick={() => login(s)}>
                            Authenticate
                          </button>
                        )}
                        {showLogout && (
                          <button type="button" className="btn" onClick={() => logout(s)}>
                            Log out
                          </button>
                        )}
                        {s.removable && (
                          <button
                            type="button"
                            className="btn btn-danger"
                            onClick={() => remove(s)}
                          >
                            Remove
                          </button>
                        )}
                      </>
                    }
                  />
                );
              })}
            </div>
          );
        })}
    </>
  );
}
