// MCP section: the global MCP servers, listed and acted on through the
// `claude mcp` CLI — this feature never reads or writes ~/.claude.json itself,
// the CLI owns that file. Which is also why nothing here is version-controlled
// and no action refreshes the git badge.
//
// Authentication is a fire-and-forget hand-off: the CLI opens a browser and
// blocks well past any request timeout, so the module spawns it detached and the
// UI says so, then re-lists when the user confirms they're done.
import { useCallback, useId, useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import type { StatusBucket } from "@platform/ui/status-colors";
import * as cc from "../api";
import type { CliResult, McpKind, McpServer, McpStatus } from "../api";
import {
  Card,
  CardActions,
  CardSub,
  CardTitle,
  Code,
  DisclosureButton,
  Empty,
  ErrorNote,
  Group,
  List,
  ListRow,
  ListSkeleton,
  SKELETON_ROWS,
  SectionToolbar,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";

// Status → label + colour bucket. connected is healthy, needs-auth waits on
// the user, failed is broken, pending is in flight, unknown is neutral.
const STATUS: Record<McpStatus, { label: string; bucket: StatusBucket }> = {
  connected: { label: "connected", bucket: "green" },
  "needs-auth": { label: "needs auth", bucket: "orange" },
  failed: { label: "failed", bucket: "red" },
  pending: { label: "pending approval", bucket: "yellow" },
  unknown: { label: "unknown", bucket: "neutral" },
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
              Registers a user-scoped MCP server via <Code>claude mcp add-json</Code>.
            </CardSub>
            <CardActions>
              <Input
                className="w-48"
                aria-label="Server name"
                placeholder="name"
                value={name}
                disabled={busy}
                onChange={(e) => setName(e.target.value)}
              />
              <Input
                className="flex-1 min-w-64 font-mono md:text-xs"
                aria-label="Server definition (JSON)"
                placeholder='{"type":"stdio","command":"my-mcp","args":[]}'
                value={json}
                disabled={busy}
                onChange={(e) => setJson(e.target.value)}
              />
              <Button disabled={busy} onClick={add}>
                Add
              </Button>
            </CardActions>
          </Card>
        </div>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {/* The module's own refusal (the CLI missing, or a non-zero list) is a
          200 with {ok:false} — it is the section's whole content when it
          happens, so it reads as an empty state rather than an error. */}
      {data && !data.ok && <Empty>{data.error || "Could not list MCP servers."}</Empty>}
      {!data && !error && <ListSkeleton rows={SKELETON_ROWS} label="Loading MCP servers" />}
      {data?.ok && servers.length === 0 && (
        <Empty
          action={
            <Button size="sm" onClick={() => setAdding(true)}>
              <Plus />
              Add a server
            </Button>
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
            <Group key={kind} title={heading}>
              <List>
                {list.map((s) => {
                  const st = STATUS[s.status] ?? STATUS.unknown;
                  // `canAuth` says the transport COULD hold a session, not that
                  // there is anything to do: a connected server with no auth
                  // state offers neither button.
                  const showLogin = s.canAuth && s.needsAuth;
                  const showLogout = s.canAuth && s.connected;
                  return (
                    <ListRow
                      key={s.name}
                      name={s.name}
                      pills={<StatusBadge bucket={st.bucket}>{st.label}</StatusBadge>}
                      secondary={s.endpoint}
                      secondaryTitle={s.endpoint}
                      secondaryMono
                      // The endpoint is the long part and the row cuts it, so the
                      // panel restates it whole — plus the transport and kind.
                      details={
                        <PropertyList className="max-w-xl">
                          <PropertyRow label="Endpoint">
                            <span className="font-mono text-xs">{s.endpoint || "—"}</span>
                          </PropertyRow>
                          <PropertyRow label="Transport">{s.transport}</PropertyRow>
                          <PropertyRow label="Status">{st.label}</PropertyRow>
                          {/* Why, in the CLI's own words. This is the whole
                              reason a failed row is worth expanding. */}
                          {s.statusDetail && (
                            <PropertyRow label="Detail" className="[&>dd]:flex-1 [&>dd]:whitespace-normal [&>dd]:text-left">
                              {s.statusDetail}
                            </PropertyRow>
                          )}
                          <PropertyRow label="Registered by">{KIND_LABEL[s.kind]}</PropertyRow>
                        </PropertyList>
                      }
                      actions={
                        <>
                          {showLogin && (
                            <Button variant="outline" size="sm" onClick={() => login(s)}>
                              Authenticate
                            </Button>
                          )}
                          {showLogout && (
                            <Button variant="outline" size="sm" onClick={() => logout(s)}>
                              Log out
                            </Button>
                          )}
                          {s.removable && (
                            <Button variant="destructive" size="sm" onClick={() => remove(s)}>
                              Remove
                            </Button>
                          )}
                        </>
                      }
                    />
                  );
                })}
              </List>
            </Group>
          );
        })}
    </>
  );
}
