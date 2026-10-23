// The compaction divider's label — `Context compacted · 167k → 16k tokens`.
// agent.py `_compact_label` writes the same text for the live notice; keep the
// two in step.

function tokenStr(n: number): string {
  return n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}

export function compactLabel(pre?: number, post?: number, trigger?: string): string {
  let label = "Context compacted";
  if (typeof pre === "number") {
    label += " · " + tokenStr(pre);
    if (typeof post === "number") label += " → " + tokenStr(post);
    label += " tokens";
  }
  if (trigger === "manual") label += " · manual";
  return label;
}
