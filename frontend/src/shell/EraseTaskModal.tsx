// LIFTED to `platform/ui/EraseTaskModal.tsx` (Akshil, 2026-09-08: "Delete task:
// reuse the Tasks page modal, not a new design").
//
// The Claude chat's kebab needs this exact dialog — same words, same behaviour
// — and an app may not import from `shell` (check-boundaries). Its two
// dependencies were already platform-only (`eraseTask`, the shared `Modal`
// chassis), so the component moved down a layer and this file is the Tasks
// page's door onto it: three call sites here keep importing `./EraseTaskModal`.
export { EraseTaskModal } from "@platform/ui/EraseTaskModal";
