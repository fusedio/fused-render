// THE CLAUDE MARK, inline (`templates/claude/icon.svg`, verbatim path data).
//
// Every "this is Claude" glyph in this view used to be an orange `✻` — a
// six-pointed asterisk that is a *typographic* stand-in for the mark, not the
// mark (Akshil, 2026-09-08: "replace with the proper Claude icon everywhere").
// One component so the home title, the header, the assistant avatar and the
// working line all draw the SAME shape at the same optical weight, and so a
// change to the artwork is one edit rather than four.
//
// `currentColor`, not a baked fill: the mark is accent-coloured in the header
// and the title (via `.c-spark`), and inherits the row's ink in a transcript
// avatar. `aria-hidden` on every copy — the word "Claude" is always beside it,
// and a second announcement of the same fact is noise.
//
// Sized in `em` off the caller's font-size, which is what makes it a drop-in
// for the character it replaces: the `✻` took its size from the type around it,
// and a fixed pixel box would have needed a per-site override.

/** The one path in `fused_render/templates/claude/icon.svg`, 512×512 viewBox. */
export const CLAUDE_MARK_PATH =
  "M100.4 340.5l100.7-56.5 1.7-4.9-1.7-2.7-4.9 0-16.8-1-57.5-1.6-49.9-2.1-48.3-2.6-12.2-2.6-11.4-15 1.2-7.5 10.2-6.9 14.7 1.3c18.9 1.3 45.9 3.1 81 5.6l35.2 2.1 52.2 5.4 8.3 0 1.2-3.4-2.8-2.1-2.2-2.1-50.3-34.1-54.4-36-28.5-20.7-15.4-10.5-7.8-9.8-3.4-21.5 14-15.4 18.8 1.3 4.8 1.3 19 14.7 40.7 31.5 53.1 39.1 7.8 6.5 3.1-2.2 .4-1.6-3.5-5.8-28.9-52.2-30.8-53.1-13.7-22-3.6-13.2c-1.3-5.4-2.2-10-2.2-15.5l15.9-21.6 8.8-2.8 21.2 2.8 8.9 7.8 13.2 30.2 21.4 47.5 33.2 64.6 9.7 19.2 5.2 17.8 1.9 5.4 3.4 0 0-3.1 2.7-36.4 5-44.7 4.9-57.5 1.7-16.2 8-19.4 15.9-10.5 12.4 5.9 10.2 14.7-1.4 9.5-6.1 39.5-11.9 61.9-7.8 41.5 4.5 0 5.2-5.2 21-27.8 35.2-44.1 15.5-17.5 18.1-19.3 11.6-9.2 22 0 16.2 24.1-7.3 24.9-22.7 28.7-18.8 24.4-27 36.3-16.8 29 1.6 2.3 4-.4 60.9-13 32.9-5.9 39.3-6.7 17.8 8.3 1.9 8.4-7 17.2-42 10.4-49.2 9.8-73.3 17.3-.9 .7 1 1.3 33 3.1 14.1 .8 34.6 0 64.4 4.8 16.8 11.1 10.1 13.6-1.7 10.4-25.9 13.2c-15.5-3.7-54.4-12.9-116.6-27.7l-28-7-3.9 0 0 2.3 23.3 22.8 42.7 38.6 53.5 49.8 2.7 12.3-6.9 9.7-7.3-1-47-35.4-18.1-15.9-41.1-34.6-2.7 0 0 3.6 9.5 13.9 50 75.2 2.6 23-3.6 7.5-13 4.5-14.2-2.6-29.3-41.1-30.2-46.3-24.4-41.5-3 1.7-14.4 154.8-6.7 7.9-15.5 5.9-13-9.8-6.9-15.9 6.9-31.5 8.3-41.1 6.7-32.7 6.1-40.6 3.6-13.5-.2-.9-3 .4-30.6 42-46.5 62.9-36.8 39.4-8.8 3.5-15.3-7.9 1.4-14.1 8.5-12.6 50.9-64.8 30.7-40.2 19.8-23.2-.1-3.4-1.2 0-135.3 87.8-24.1 3.1-10.4-9.7 1.3-15.9 4.9-5.2 40.7-28-.1 .1 0 .1z";

export interface ClaudeMarkProps {
  /** Extra class, for the seat's own colour and margin (`.c-spark`). */
  className?: string;
  /** Multiple of the caller's font-size. 1 renders the mark at the cap height
   *  of the type it sits in, which is what the `✻` did. */
  size?: number;
}

export function ClaudeMark({ className, size = 1 }: ClaudeMarkProps) {
  return (
    <svg
      className={className ? `c-claudemark ${className}` : "c-claudemark"}
      viewBox="0 0 512 512"
      width={`${size}em`}
      height={`${size}em`}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d={CLAUDE_MARK_PATH} />
    </svg>
  );
}
