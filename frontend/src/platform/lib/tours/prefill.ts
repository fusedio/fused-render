// The one DOM helper a tour step's `onEnter` needs: putting a sample prompt in
// an app's own text box. Its own module rather than part of the runtime because
// the tours (ai.ts) are imported BY the registry, which is imported by the
// runtime — reaching back into index.ts from a tour would be a cycle.

/** Type `text` into the input at `selector` the way a person would.
 *
 *  A controlled <textarea> has React holding its value: assigning `el.value`
 *  moves the pixels but not the state, so the next render puts the old value
 *  back and the Run button — disabled on an empty prompt — stays dead. Writing
 *  through the PROTOTYPE's native value setter and then dispatching a bubbling
 *  "input" event is exactly what React's synthetic onChange is listening for,
 *  so state and DOM end up agreeing.
 *
 *  Only ever fills an EMPTY box: a tour must never eat a draft the user is
 *  already writing (replaying the tour mid-prompt is the ordinary way here). */
export function prefillInput(selector: string, text: string): void {
  const el = document.querySelector<HTMLTextAreaElement | HTMLInputElement>(selector);
  if (!el || el.value.trim() !== "") return;
  const proto =
    el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setValue = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (!setValue) return;
  setValue.call(el, text);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  // Focused with the caret at the end: the step says "edit it or send it", and
  // both start from there.
  el.focus();
  el.setSelectionRange?.(text.length, text.length);
}
