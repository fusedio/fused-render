// THE LEFT SIDEBAR'S BOTS AND TASKS GLYPHS (GlobalSidebar), in one place so
// the Claude rail's Tasks and Bots doors (ClaudeRail, behind the rail flag)
// wear the very same marks rather than redrawn lookalikes — and the Git
// door's, the sidebar's Git template glyph.
import { ListTodo } from "lucide-react";

// Bot head (lucide `bot`) — the Bots page (/bots): browser bots, each driving
// its own Chrome window.
export const BOTS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 8V4H8" />
    <rect width="16" height="12" x="4" y="8" rx="2" />
    <path d="M2 14h2" />
    <path d="M20 14h2" />
    <path d="M15 13v2" />
    <path d="M9 13v2" />
  </svg>
);

// Specialists (docs/specialists): lucide `graduation-cap` — an expert on one
// subject. Drawn inline like the Bots head so the rail and the row match.
export const SPECIALISTS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z" />
    <path d="M22 10v6" />
    <path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5" />
  </svg>
);

// A to-do list (lucide ListTodo, sized like the hand-drawn icons around it):
// Tasks is the page about work, and the app page's Tasks tab wears the same
// glyph (shell/AppPage.tsx) so the two read as one thing. Was a clock while the
// page was "Scheduled".
export const TASKS_ICON = <ListTodo size={16} strokeWidth={2} aria-hidden="true" />;

// The Git template's own glyph (fused_render/templates/git/icon.svg), inline
// at the rail's 16px: the rail's Git door wears the mark the sidebar's Git
// entry does (owner, 2026-11-10).
export const GIT_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M6 6v12" />
    <path d="M18 9a9 9 0 0 1-9 9" />
    <circle cx="6" cy="4" r="2.6" />
    <circle cx="18" cy="6" r="2.6" />
    <circle cx="6" cy="20" r="2.6" />
  </svg>
);
