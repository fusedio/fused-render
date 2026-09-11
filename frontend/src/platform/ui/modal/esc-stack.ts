// WHICH MODAL OWNS THE NEXT Esc.
//
// `Modal` closes on a document-level keydown listener (see Modal.tsx for why it
// is on the document and not the dialog subtree), and every mounted modal has
// one of its own. So a dialog nested INSIDE another — the chat's "what was
// sent" receipt, or its kebab's delete confirm, both of which open inside
// `TaskPeek`, itself a `Modal` — closed itself and the peek around it on one
// press (Bugbot, PR #1061, High). `T` binds Esc in the capture phase and
// `stopPropagation`s so the layers peel one per press (T:11044-11052); a stack
// is that rule expressed ONCE for every caller of the chassis rather than once
// per nested dialog, and it needs no phase games.
//
// Its own module so it can be tested without a DOM: `Modal` is portal-based, so
// mounting one takes a real document, and the rule this holds is pure.
const stack: object[] = [];

/** Register a modal as open. The token is any stable object identity. */
export function pushModal(token: object): void {
  stack.push(token);
}

/** Unregister it. BY IDENTITY, not by position: a parent can unmount while a
 *  child is still up (a caller closing the outer dialog directly), and the
 *  remaining tokens must still name the right layers. */
export function popModal(token: object): void {
  const i = stack.indexOf(token);
  if (i >= 0) stack.splice(i, 1);
}

/** Is this the innermost open modal — the one a press belongs to? A modal that
 *  never registered (or one registered while the stack is empty) answers true,
 *  so a lone dialog behaves exactly as it did before the stack existed. */
export function isTopmost(token: object): boolean {
  return stack.length === 0 || stack[stack.length - 1] === token;
}

/** Test-only: the depth, for asserting that mounts and unmounts balance. */
export function openModalCount(): number {
  return stack.length;
}
