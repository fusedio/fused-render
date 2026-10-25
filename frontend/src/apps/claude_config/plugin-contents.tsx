// What one installed plugin contributes — its skills, commands, agents, hooks
// and MCP servers, read off disk — shown under the plugin's expanded row.
import { useCallback } from "react";
import { urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "./api";
import type { PluginComponent, PluginContents } from "./api";
import { useModuleData } from "./bits";

// The five kinds of thing a plugin can put in a session, in the order a reader
// cares about them: what it can DO for you first (skills, commands, agents),
// then what it does on its own (hooks, MCP servers). A group with nothing in it
// renders nothing at all — an empty "Commands 0" heading is a row of chrome
// saying a plugin does not have something, which is most plugins for most
// kinds.
const GROUPS: { key: keyof PluginContents; label: string }[] = [
  { key: "skills", label: "Skills" },
  { key: "commands", label: "Commands" },
  { key: "agents", label: "Agents" },
  { key: "hooks", label: "Hooks" },
  { key: "mcpServers", label: "MCP servers" },
];

// What one installed plugin actually contributes, under its expanded row.
//
// Mounted ONLY when the row is open (ListRow renders `details` behind `open`),
// which is what makes the per-plugin read affordable: expanding one plugin
// walks one plugin's tree, and a page you never expand reads nothing at all.
// Hence a component with its own fetch rather than data threaded down from the
// section — the mount IS the trigger.
export function PluginContentsPanel({ id }: { id: string }) {
  const load = useCallback(() => cc.plugins.contents(id), [id]);
  const { data, error } = useModuleData(load);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <p>Reading plugin files…</p>;
  if (!data.ok) return <p>{data.error || "Could not read this plugin."}</p>;

  const groups = GROUPS.map((g) => ({
    ...g,
    items: (data[g.key] as PluginComponent[] | undefined) ?? [],
  })).filter((g) => g.items.length > 0);

  return (
    <div className="cc-pcontents">
      {data.description && <p className="cc-pblurb">{data.description}</p>}
      {groups.length === 0 && (
        <p className="cc-pblurb">
          This plugin ships no skills, commands, agents, hooks or MCP servers.
        </p>
      )}
      {groups.map((g) => (
        <section className="cc-pgroup" key={String(g.key)}>
          <h4 className="cc-pgroup-title">
            {g.label}
            <span className="cc-count">{g.items.length}</span>
          </h4>
          {/* The panel is one grid and every level down to the anchor passes
              its two tracks along (see .cc-pcontents), which is what puts
              every description on a single left edge. Laid out per-entry, each
              description started wherever its name happened to end — a ragged
              left edge down thirteen rows was the loudest thing in here. */}
          <ul className="cc-pitems">
            {g.items.map((it) => (
              // A real anchor, not a click handler: the entry IS a file, so it
              // gets the file's affordances for free — middle-click,
              // cmd-click, a copyable target in the context menu. A new tab
              // because this page holds unsaved-ish state (an open filter, a
              // half-typed marketplace form) that navigating away would lose.
              <li key={it.path + it.name}>
                <a
                  className="cc-pitem"
                  href={urlForFsPath(it.path)}
                  target="_blank"
                  rel="noopener"
                  title={it.description ? `${it.description}\n\n${it.path}` : it.path}
                >
                  <span className="cc-pitem-name">{it.name}</span>
                  <span className="cc-pitem-desc">{it.description}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {/* Where the files ARE — metadata about the plugin, not one of the
          things it gives you, so it sits below a rule rather than becoming a
          sixth row of whatever group happened to be last. */}
      {data.root && (
        <a
          className="cc-pfiles"
          href={urlForFsPath(data.root)}
          target="_blank"
          rel="noopener"
          title={data.root}
        >
          <span className="cc-pfiles-label">Files</span>
          <span className="cc-pfiles-path">{data.root}</span>
        </a>
      )}
      {/* The server's per-read walk budget ran out (plugins.py's
          _WalkBudget) — an unusually large plugin, not a plugin that
          genuinely ships this little. Said plainly rather than left for the
          groups above to quietly under-report. */}
      {data.truncated && (
        <p className="cc-pblurb cc-ptruncated">
          This plugin ships more than shown here — the list was cut off to keep this panel fast.
        </p>
      )}
    </div>
  );
}
