// Pure helpers for the expert pane (components/ExpertPane.tsx, PaneParts.tsx): a short "how long ago", and a
// unified diff of memory.md read as rows of a document (added, removed, a removed + added pair, unchanged context).
// No fetching, no React.

/** A time in the past, short ("now", "2m ago", "3h ago", "4d ago"; older than a week as the date `older` gives). */
export function agoShort(ts: number, nowMs: number | undefined, older: (ts: number, nowMs?: number) => string): string {
  if (!ts) return "";
  const sec = Math.max(0, (nowMs ?? Date.now()) / 1000 - ts);
  if (sec < 60) return "now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 7 * 86400) return `${Math.floor(sec / 86400)}d ago`;
  return older(ts, nowMs);
}

/** One row of a memory diff read as a document: an added, removed or unchanged line (its markdown source, without the
 *  diff's one-character prefix); a removed line answered by an added one (a fact's count bump); or a gap between hunks. */
export type DiffRow =
  | { tone: "add" | "del" | "ctx"; text: string }
  | { tone: "pair"; del: string; add: string }
  | { tone: "gap" };

/** A fact's text without its `- [count · date]` stamp, to tell a bumped fact from a different one. */
const factOf = (l: string): string => l.replace(/^\s*[-*+]\s+\[[^\]]*\]\s*/, "").trim();

/** A unified diff of memory.md as document rows. The noise goes: everything before the first `@@` (the `---` / `+++`
 *  file headers, `diff` / `index` lines), each `@@` hunk header (a later one becomes a gap), `\ No newline…`.
 *  Inside a hunk the first character decides: `+` added, `-` removed, the rest context (so a removed `---` line is a
 *  line, not a header). A run of removed lines answered by a run of added lines pairs up: one and one always; otherwise
 *  each removed fact with the first added line holding the same fact (only its `[count · date]` stamp moved).
 *  Added or removed blank lines are left out; an empty diff, or one that only changed blank lines, is no rows. */
export function diffDoc(diff: string | null | undefined): DiffRow[] {
  const text = (diff || "").replace(/\r/g, "").replace(/\n$/, "");
  if (!text.trim()) return [];
  let lines = text.split("\n");
  const firstHunk = lines.findIndex((l) => l.startsWith("@@"));
  if (firstHunk >= 0) lines = lines.slice(firstHunk);
  else while (lines.length && /^(---|\+\+\+|diff |index )/.test(lines[0])) lines = lines.slice(1);
  const flat: DiffRow[] = [];
  let hunks = 0;
  for (const l of lines) {
    if (l.startsWith("@@")) { if (hunks++ && flat.length) flat.push({ tone: "gap" }); continue; }
    if (l.startsWith("\\")) continue;
    // A blank line added or removed is no fact: its row would be an empty coloured strip, so it is left out.
    if (l.startsWith("+")) { if (l.slice(1).trim()) flat.push({ tone: "add", text: l.slice(1) }); }
    else if (l.startsWith("-")) { if (l.slice(1).trim()) flat.push({ tone: "del", text: l.slice(1) }); }
    else flat.push({ tone: "ctx", text: l.startsWith(" ") ? l.slice(1) : l });
  }
  // Pair a removed run with the added run right after it (see above).
  const out: DiffRow[] = [];
  for (let i = 0; i < flat.length;) {
    if (flat[i].tone !== "del") { out.push(flat[i++]); continue; }
    let j = i;
    while (j < flat.length && flat[j].tone === "del") j++;
    let k = j;
    while (k < flat.length && flat[k].tone === "add") k++;
    const dels = flat.slice(i, j) as { tone: "del"; text: string }[], adds = flat.slice(j, k) as { tone: "add"; text: string }[];
    if (dels.length === 1 && adds.length === 1) out.push({ tone: "pair", del: dels[0].text, add: adds[0].text });
    else {
      // Each removed fact meets the first unused added line with the same fact; the rest keep their order after the pairs.
      const used = new Set<number>(), lone: DiffRow[] = [];
      for (const d of dels) {
        const f = factOf(d.text), n = f ? adds.findIndex((a, x) => !used.has(x) && factOf(a.text) === f) : -1;
        if (n >= 0) { used.add(n); out.push({ tone: "pair", del: d.text, add: adds[n].text }); } else lone.push(d);
      }
      out.push(...lone, ...adds.filter((_, x) => !used.has(x)));
    }
    i = k;
  }
  return out.some((r) => r.tone !== "ctx" && r.tone !== "gap") ? out : [];  // only blank lines changed: nothing to show
}
