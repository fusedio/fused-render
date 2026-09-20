// The Text stage's "Tools" rail field: add/remove the MCP servers a chat
// turn is handed as `mcpServers` (server/ai.py). Deliberately small next to
// `apps/claude_config/sections/McpSection.tsx`'s own list — no login/auth
// flow (a playground server is reached directly, never through Claude
// Code's own OAuth), no status polling (nothing here is a standing
// connection; `mcp_client.py` opens and closes one per chat turn) — the one
// thing this panel owns is the definition list itself, name + a JSON blob,
// the same shape `McpSection`'s own "Add a server" form asks for.
import { useId, useState } from "react";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { RailField, RailReset } from "./controls";
import { mcpServerProblem, type McpServerConfig } from "./mcpTools";

export function McpToolsPanel({
  servers,
  onChange,
}: {
  servers: McpServerConfig[];
  onChange: (servers: McpServerConfig[]) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [json, setJson] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const formId = useId();

  const add = () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setProblem("a server needs a name");
      return;
    }
    if (servers.some((s) => s.name === trimmedName)) {
      setProblem(`"${trimmedName}" is already in the list`);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(json);
    } catch {
      setProblem("not valid JSON");
      return;
    }
    const issue = mcpServerProblem(parsed);
    if (issue) {
      setProblem(issue);
      return;
    }
    onChange([...servers, { ...(parsed as object), name: trimmedName } as McpServerConfig]);
    setName("");
    setJson("");
    setProblem(null);
    setAdding(false);
  };

  const remove = (target: string) => onChange(servers.filter((s) => s.name !== target));

  return (
    <RailField
      label="Tools (MCP servers)"
      action={
        <RailReset onClick={() => setAdding((v) => !v)}>
          {adding ? "cancel" : "add a server"}
        </RailReset>
      }
      hint="MCP servers this model may call as tools. Best-effort on a local model — only tool-trained families (Qwen, Hermes and similar) reliably call one; Claude always can."
    >
      {servers.length > 0 && (
        <ul className="pg-mcp-list">
          {servers.map((s) => (
            <li key={s.name} className="pg-mcp-row">
              <span className="pg-mcp-name">{s.name}</span>
              <span className="pg-mcp-type">{s.type}</span>
              <button
                type="button"
                className="pg-ghost-btn"
                title={`Remove ${s.name}`}
                aria-label={`Remove ${s.name}`}
                onClick={() => remove(s.name)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <div id={formId} className="pg-mcp-add">
          <Input
            aria-label="Server name"
            placeholder="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Textarea
            className="min-h-0 resize-y text-xs leading-normal"
            rows={3}
            aria-label="Server definition (JSON)"
            placeholder='{"type":"stdio","command":"my-mcp","args":[]}'
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
          {problem && <p className="pg-error text-xs">{problem}</p>}
          <button type="button" className="btn btn-primary" onClick={add}>
            Add
          </button>
        </div>
      )}
    </RailField>
  );
}
