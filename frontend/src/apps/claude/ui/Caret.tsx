// The typewriter's caret: `span.cursor` "▋" (T:15063-15066), whose blink is
// pure CSS (`.cursor { animation: blink 1s step-end infinite }`, T:3235-3236 —
// ported into styles/transcript.css).
//
// A SIBLING of the prose element, never inside it: `T` does `bodyEl.after(cur)`
// because the body's innerHTML is rewritten on every frame, so a caret inside it
// would be destroyed and rebuilt ~25 times a second. Here the same rule falls out
// of `MarkdownView` owning its own subtree — the caret has to live outside it.
//
// `aria-hidden`: it is decoration for a reply a screen reader is already being
// handed as text.
export function Caret() {
  return (
    <span className="cursor" aria-hidden="true">
      {"▋"}
    </span>
  );
}

export default Caret;
