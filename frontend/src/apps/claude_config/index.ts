// Claude Config app entry point.
//
// Unlike learn and sessions, this app is NOT html+py content in a mount: it is
// native React (ClaudeConfig.tsx) over a dedicated server bridge
// (POST /api/claude-config/{module}, see ./api.ts). It went native so it could
// use the shell's palette tokens, toast surface and modal chassis — an iframe'd
// html app carried its own Claude-branded dark theme and ignored the app's
// Light/Dark setting entirely.
//
// The shell gives it one sidebar route under the CLAUDE heading, gated on
// `useClaudeConfigAvailable` (./available.ts, checked from
// shell/ShellSidebar.tsx and dispatched in shell/App.tsx): `/claude-config`,
// the settings panel, whose "MD Files" section is the CLAUDE.md explorer
// (`?cctab=claudemd`; the old `/claude-md` page redirects there). It briefly
// also hung off the Preferences page as a tab; a settings page with a second
// settings app nested inside one of its tabs was one surface too many.
// Mutable state (settings.json, the git history, MCP registrations) is written
// by the server-side modules to ~/.claude — this app owns none of it.
//
// This barrel is the panel's own heavy surface (ClaudeConfig.tsx + its
// sections/bits/ansi dependencies) — shell/App.tsx lazy-loads it for the
// `/claude-config` route. The eager availability probe lives in ./available.ts
// instead, on purpose: see that file for why it must not go through here.
export { default as ClaudeConfig } from "./ClaudeConfig";
