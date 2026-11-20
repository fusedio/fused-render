// The responder lives with the shots it answers with (apps/claude/shots), so
// the explorer's file view can mount it too for a node's own page (SPEC
// §53.8); the explorer may not import apps/canvases. Re-exported here for
// the canvas page and its tests.
export * from "@apps/claude/shots/canvas-page-shot-responder";
