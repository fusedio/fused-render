// The tabs of the Claude Config page: Claude Code's own configuration under
// ~/.claude, drawn with the settings page's rows and cards.
import McpSection from "@apps/claude_config/sections/McpSection";
import MemorySection from "@apps/claude_config/sections/MemorySection";
import PluginsSection from "@apps/claude_config/sections/PluginsSection";
import SkillsSection from "@apps/claude_config/sections/SkillsSection";

type HeadProps = { title: string; blurb: string };

// -- the tabs ------------------------------------------------------------------

export function ClaudePluginsTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <PluginsSection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}

export function ClaudeMemoryTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <MemorySection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}

export function ClaudeSkillsTab({
  filter,
  onOpenPluginsDiscover,
}: {
  filter: string;
  onOpenPluginsDiscover: () => void;
}) {
  return <SkillsSection filter={filter} onOpenPluginsDiscover={onOpenPluginsDiscover} />;
}

export function ClaudeMcpTab({ filter, title, blurb }: HeadProps & { filter: string }) {
  return (
    <div className="settings-block">
      <McpSection filter={filter} title={title} blurb={blurb} />
    </div>
  );
}
