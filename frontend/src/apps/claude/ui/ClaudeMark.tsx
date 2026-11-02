// Moved to platform/ui/ClaudeMark.tsx so platform and shell surfaces can
// draw the mark too — platform may not import apps. This re-export keeps every
// chat-side import spelling working unchanged.
export { ClaudeMark, CLAUDE_MARK_PATH, type ClaudeMarkProps } from "@platform/ui/ClaudeMark";
