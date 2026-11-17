// Which of the content pane's mounted mode frames survive a finished swap
// (Preview's held-frame swap, A1; SPEC §53.3 NV-13). Pure, so the rule has a
// test of its own rather than living only inside Preview's effects.
//
// The incoming mode always stays. Every other frame is retired — its document
// is thrown away and a later switch back loads it again — except the canvas
// node view while its mode is still on offer: its open is the heaviest in the
// shell (sync/start, a token, a cross-origin workbench that boots and runs the
// node), so it is kept mounted, hidden, for as long as the file stays open.
//
// Order is preserved: the list is append-only and never reordered, because
// React moving an iframe reloads it.
export function retainedFrames(
  frames: readonly string[],
  activeMode: string,
  nodeMode: string,
  nodeOffered: boolean,
): string[] {
  return frames.filter((m) => m === activeMode || (m === nodeMode && nodeOffered));
}
