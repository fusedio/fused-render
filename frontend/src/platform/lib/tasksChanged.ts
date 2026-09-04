// "A task just changed" — the platform half of a poke at the shell's shared
// tasks store (shell/tasksPulse.ts). An APP that creates a task (the Home
// hero's new-app composer: its prompt becomes a task on the new folder's
// index.html) cannot import that store — platform and apps never import up
// into shell — so it announces on the window instead, and App.tsx, which may
// import anything, forwards the event to `pokeTasks`. Same shape as the chat's
// CHAT_ACTIVITY_KEY storage stamp: a fact thrown over the wall, not a call.
//
// Fired ONCE, at creation. The row then appears as `upcoming` on the next
// paint and flips to `in_progress` on the store's own active-poll clock (10s)
// once the scheduler has spawned the turn; a second poke to catch that flip
// was considered and declined (owner, 2026-08-26) — one poke is enough.
export const TASKS_CHANGED_EVENT = "fused-render:tasks-changed";

export function announceTasksChanged(): void {
  window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
}

// "The desk just changed" — the same wall-throw for the sidebar's Projects
// table (shell/CurrentAppsSection). The explorer's "Open in project" button
// puts a folder on the desk (POST /api/current-apps/add) and then hops to
// its app page; the section refetches on this so the new row is there — on
// top, focused — the moment the page paints, not on the next task pulse.
export const CURRENT_APPS_CHANGED_EVENT = "fused-render:current-apps-changed";

export function announceCurrentAppsChanged(): void {
  window.dispatchEvent(new Event(CURRENT_APPS_CHANGED_EVENT));
}
