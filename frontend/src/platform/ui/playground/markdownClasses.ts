// The model's markdown, as class strings rather than components.
//
// `markdown.tsx` builds its own `<p>` / `<h3>` / `<ul>` / `<code>` / `<a>`
// nodes — it is a renderer, not a layout — so the styling has to reach them
// from their container. That makes the migration a className-only swap
// (`"pg-md"` → `markdownClass`) and leaves the parser untouched.
//
// The mono stack is spelled out rather than taken from Tailwind's `font-mono`:
// that default carries seven fallbacks this app does not name, and a code block
// that picks a different face than the rest of the shell is a visible
// difference, not a tidier class.
const MONO = "[font-family:ui-monospace,SFMono-Regular,Menlo,monospace]";

/** The rendered reply's container. Every descendant rule lives here. */
export const markdownClass = [
  // The card owns the padding; the first and last blocks must not add to it.
  "[&>*:first-child]:mt-0",
  "[&>*:last-child]:mb-0",
  // `whitespace-pre-wrap`, because a model's own line breaks inside a paragraph
  // are content.
  "[&_p]:mx-0 [&_p]:mt-0 [&_p]:mb-2.5 [&_p]:whitespace-pre-wrap",
  "[&_h3]:mx-0 [&_h3]:mt-3.5 [&_h3]:mb-1.5 [&_h3]:text-[15px] [&_h3]:leading-[1.3]",
  "[&_h4]:mx-0 [&_h4]:mt-3.5 [&_h4]:mb-1.5 [&_h4]:text-[14px] [&_h4]:leading-[1.3]",
  "[&_h5]:mx-0 [&_h5]:mt-3.5 [&_h5]:mb-1.5 [&_h5]:text-[13px] [&_h5]:leading-[1.3]",
  "[&_ul]:mx-0 [&_ul]:mt-0 [&_ul]:mb-2.5 [&_ul]:pl-[22px]",
  "[&_ol]:mx-0 [&_ol]:mt-0 [&_ol]:mb-2.5 [&_ol]:pl-[22px]",
  "[&_li]:mx-0 [&_li]:my-0.5",
  `[&_code]:${MONO} [&_code]:rounded-[4px] [&_code]:bg-[rgba(var(--tint),0.08)] [&_code]:px-[5px] [&_code]:py-px [&_code]:text-[12px]`,
  "[&_a]:text-[var(--accent-soft)]",
].join(" ");

/** A fenced block: its own bordered surface on the page's ground, so it reads
 *  as a quoted file rather than as part of the sentence above it. */
export const markdownCodeClass = [
  "mx-0 mt-0 mb-2.5 overflow-hidden rounded-[8px] border border-solid border-[var(--border)] bg-[var(--bg)]",
  `[&_pre]:m-0 [&_pre]:overflow-x-auto [&_pre]:px-3 [&_pre]:py-2.5 [&_pre]:text-[12px] [&_pre]:leading-[1.55] [&_pre]:${MONO}`,
].join(" ");

/** The language name and the Copy button. The button is bare — a bordered
 *  control in a 20px strip would out-weigh the code under it. */
export const markdownCodeHeadClass = [
  "flex items-center justify-between border-b border-[var(--border)] bg-[rgba(var(--tint),0.03)] px-2.5 py-1 text-[11px] text-[var(--fg-muted)]",
  "[&_button]:cursor-pointer [&_button]:border-none [&_button]:bg-transparent [&_button]:p-0 [&_button]:text-[11px] [&_button]:text-[var(--fg-muted)]",
  "[&_button]:[font-family:inherit] [&_button]:[font-style:inherit] [&_button]:[font-weight:inherit] [&_button]:[font-variant:inherit] [&_button]:[line-height:inherit]",
  "[&_button:hover]:text-[var(--fg)]",
].join(" ");
