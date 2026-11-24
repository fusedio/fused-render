/** Whitespace collapsed, trimmed, and cut with an ellipsis past `max`. */
export function oneLine(text: string, max = Infinity): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1).trimEnd() + "…" : flat;
}

/** The cap for a tool's summary inside a one-line caption. */
export const SUMMARY_LINE_MAX = 80;
