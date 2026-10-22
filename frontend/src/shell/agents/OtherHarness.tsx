// The Codex and Copilot tabs of the Agents page: the instruction file and the
// settings file as plain text editors, and the MCP server list as JSON
// definitions. The files live in the harness's own home (CODEX_HOME,
// COPILOT_HOME); the server validates TOML/JSON on save.
import { useCallback, useEffect, useState } from "react";
import * as cc from "@apps/claude_config/api";
import type { FileKind, HarnessMcpServer } from "@apps/claude_config/api";
import { Empty, List, ListRow, SectionToolbar, guard, toastErr, toastOk } from "@apps/claude_config/bits";
import { FileEditor } from "@apps/claude_config/FileEditor";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

export function HarnessFileTab({ harness, kind }: { harness: string; kind: FileKind }) {
  const [path, setPath] = useState("");
  const load = useCallback(async () => {
    const res = await cc.harnessFiles.get(harness, kind);
    if (!res.ok) throw new Error(res.error || "Could not read the file");
    setPath(res.path);
    return res.text;
  }, [harness, kind]);
  const save = useCallback(
    async (text: string) => {
      const res = await cc.harnessFiles.put(harness, kind, text);
      return res.ok ? null : res.error || "Save failed";
    },
    [harness, kind],
  );
  return (
    <div className="settings-block">
      {path && <p className="cc-lrow-sub cc-mono">{path}</p>}
      <FileEditor key={`${harness}:${kind}`} label={kind} load={load} save={save} rows={24} />
    </div>
  );
}

export function HarnessMcpTab({ harness, filter }: { harness: string; filter: string }) {
  const [servers, setServers] = useState<HarnessMcpServer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [json, setJson] = useState("");

  const reload = useCallback(() => {
    setError(null);
    cc.harnessMcp.list(harness).then(
      (res) => (res.ok ? setServers(res.servers ?? []) : setError(res.error || "Could not list servers")),
      (e) => setError((e as Error).message),
    );
  }, [harness]);
  useEffect(() => {
    setServers(null);
    reload();
  }, [reload]);

  const close = () => {
    setAdding(false);
    setEditing(null);
    setName("");
    setJson("");
  };

  const submit = async () => {
    const res = await guard(
      editing ? cc.harnessMcp.edit(harness, name.trim(), json) : cc.harnessMcp.add(harness, name.trim(), json),
    );
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Save failed");
      return;
    }
    toastOk(`${editing ? "Updated" : "Added"} ${name.trim()}`);
    close();
    reload();
  };

  const remove = async (n: string) => {
    if (!window.confirm(`Remove MCP server "${n}"?`)) return;
    const res = await guard(cc.harnessMcp.remove(harness, n));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Remove failed");
      return;
    }
    toastOk(`Removed ${n}`);
    reload();
  };

  const needle = filter.trim().toLowerCase();
  const shown = (servers ?? []).filter((s) => !needle || `${s.name} ${s.endpoint}`.toLowerCase().includes(needle));

  return (
    <div className="settings-block">
      <SectionToolbar summary={servers ? `${shown.length} server(s)` : "…"} onRefresh={reload}>
        <Button size="sm" variant="outline" onClick={() => (adding ? close() : setAdding(true))}>
          {adding ? "Cancel" : "Add server"}
        </Button>
      </SectionToolbar>
      {adding && (
        <div className="cc-fileeditor">
          <Input
            aria-label="Server name"
            placeholder="name"
            value={name}
            disabled={!!editing}
            onChange={(e) => setName(e.target.value)}
            className="max-w-64"
          />
          <Textarea
            aria-label="Server definition (JSON)"
            className="cc-fileeditor-text"
            rows={8}
            placeholder='{"command":"my-mcp","args":[]}'
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
          <div className="cc-fileeditor-bar">
            <Button size="sm" disabled={!name.trim() || !json.trim()} onClick={() => void submit()}>
              {editing ? "Save" : "Add"}
            </Button>
          </div>
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {servers && !shown.length && <Empty>{needle ? "No servers match." : "No MCP servers configured."}</Empty>}
      {shown.length > 0 && (
        <List>
          {shown.map((s) => (
            <ListRow
              key={s.name}
              name={s.name}
              secondary={s.endpoint}
              secondaryTitle={s.endpoint}
              secondaryMono
              actions={
                <>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      setName(s.name);
                      setJson(JSON.stringify(s.spec, null, 2));
                      setEditing(s.name);
                      setAdding(true);
                    }}
                  >
                    Edit
                  </button>
                  <button type="button" className="btn btn-danger" onClick={() => void remove(s.name)}>
                    Remove
                  </button>
                </>
              }
            />
          ))}
        </List>
      )}
    </div>
  );
}
