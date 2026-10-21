// THE AGENT'S MARK, by backend id (docs/COPILOT_BACKEND.md D18). Claude draws
// the real Claude mark (`ClaudeMark`); Copilot draws a glyph of OUR OWN — a
// rounded square carrying a chevron pair — and NEVER the GitHub Copilot logo
// or the Octocat, which are GitHub's to draw and not ours.
//
// Same contract as `ClaudeMark`: `currentColor`, `em`-sized off the caller's
// type so it drops into every seat the ✻ once had, `aria-hidden` because the
// agent's word is always beside it — or in `title`, when `word` is asked for.
import { ClaudeMark } from "./ClaudeMark";
import { agentLabel } from "@platform/lib/agent-backends";

export interface AgentMarkProps {
  /** "claude" | "copilot" — anything else, or nothing, is Claude. */
  backend?: string | null;
  className?: string;
  /** Multiple of the caller's font-size (see `ClaudeMark`). */
  size?: number;
  /** Draw the agent's name after the glyph. */
  word?: boolean;
}

/** The Copilot glyph: a 512-box, rounded square outline, two chevrons. */
function CopilotGlyph({ className, size = 1 }: { className?: string; size?: number }) {
  return (
    <svg
      className={className ? `c-claudemark c-agentmark-copilot ${className}` : "c-claudemark c-agentmark-copilot"}
      viewBox="0 0 512 512"
      width={`${size}em`}
      height={`${size}em`}
      fill="none"
      stroke="currentColor"
      strokeWidth={46}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="40" y="40" width="432" height="432" rx="96" />
      <path d="M150 190l82 66-82 66M270 190l82 66-82 66" />
    </svg>
  );
}

export function AgentMark({ backend, className, size = 1, word = false }: AgentMarkProps) {
  const id = backend || "claude";
  const glyph = id === "copilot"
    ? <CopilotGlyph className={className} size={size} />
    : <ClaudeMark className={className} size={size} />;
  if (!word) return glyph;
  return (
    <span className="c-agentmark-word">
      {glyph}
      <span>{agentLabel(id)}</span>
    </span>
  );
}
