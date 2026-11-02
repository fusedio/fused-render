// Path-search combobox for the Memory tab's project filter. Typing only narrows
// the dropdown; the groups below change when an option is SELECTED.
import { useId, useMemo, useState } from "react";
import { FolderIcon, XIcon } from "lucide-react";
import { Input } from "@platform/shadcn/ui/input";

export type PathOption = { id: string; title: string; count: number; confirmed: boolean };

/** The /Users/<name> or /home/<name> prefix shared by the confirmed paths, or "". */
export function deriveHome(paths: string[]): string {
  const tally = new Map<string, number>();
  for (const p of paths) {
    const m = /^(\/Users\/[^/]+|\/home\/[^/]+)(\/|$)/.exec(p);
    if (m) tally.set(m[1], (tally.get(m[1]) ?? 0) + 1);
  }
  let best = "";
  let n = 0;
  for (const [h, c] of tally) if (c > n) [best, n] = [h, c];
  return best;
}

export function abbreviateHome(path: string, home: string): string {
  return home && (path === home || path.startsWith(home + "/")) ? "~" + path.slice(home.length) : path;
}

const tokensOf = (q: string) => q.toLowerCase().split(/\s+/).filter(Boolean);

/** Lower is better; null = no match. Every token must be a substring of the path. */
function score(path: string, tokens: string[]): number | null {
  const lower = path.toLowerCase();
  if (!tokens.every((t) => lower.includes(t))) return null;
  if (!tokens.length) return 0;
  const segs = lower.split("/").filter(Boolean);
  const last = segs[segs.length - 1] ?? "";
  const q = tokens.join(" ");
  if (last.startsWith(tokens[0]) && tokens.every((t) => last.includes(t))) return 0;
  if (tokens.every((t) => last.includes(t))) return 1;
  if (segs.some((s) => s.startsWith(tokens[0]))) return 2;
  return q ? 3 : 0;
}

/** Filter + rank options. Matches against the full path and its "~" form. */
export function rankPaths<T extends { title: string }>(items: T[], query: string, home = ""): T[] {
  const tokens = tokensOf(query);
  if (!tokens.length) return [...items];
  const scored: { it: T; s: number }[] = [];
  for (const it of items) {
    const a = score(it.title, tokens);
    const b = home ? score(abbreviateHome(it.title, home), tokens) : null;
    const s = a === null ? b : b === null ? a : Math.min(a, b);
    if (s !== null) scored.push({ it, s });
  }
  scored.sort((x, y) => x.s - y.s || x.it.title.length - y.it.title.length || x.it.title.localeCompare(y.it.title));
  return scored.map((x) => x.it);
}

function Highlighted({ text, tokens }: { text: string; tokens: string[] }) {
  const lower = text.toLowerCase();
  const mark = new Array<boolean>(text.length).fill(false);
  for (const t of tokens) {
    for (let i = lower.indexOf(t); i >= 0; i = lower.indexOf(t, i + t.length)) {
      for (let j = i; j < i + t.length; j++) mark[j] = true;
    }
  }
  const out: React.ReactNode[] = [];
  for (let i = 0; i < text.length; ) {
    let j = i;
    while (j < text.length && mark[j] === mark[i]) j++;
    const s = text.slice(i, j);
    out.push(mark[i] ? <mark className="cc-pathmark" key={i}>{s}</mark> : s);
    i = j;
  }
  return <>{out}</>;
}

export function ProjectPathSearch({
  options,
  value,
  onChange,
}: {
  options: PathOption[];
  value: string;
  onChange: (id: string) => void;
}) {
  const uid = useId();
  const listId = `${uid}-list`;
  // null = not editing: the input shows the selected path.
  const [query, setQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);

  const home = useMemo(() => deriveHome(options.filter((o) => o.confirmed).map((o) => o.title)), [options]);
  const selected = options.find((o) => o.id === value);
  const q = query ?? "";
  const matches = useMemo(() => rankPaths(options, q, home), [options, q, home]);
  const tokens = tokensOf(q);
  const shown = open && matches.length > 0;

  const select = (o: PathOption) => {
    onChange(o.id);
    setQuery(null);
    setOpen(false);
    setActive(-1);
  };
  const clear = () => {
    onChange("");
    setQuery(null);
    setActive(-1);
  };
  const optId = (i: number) => `${uid}-opt-${i}`;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      const n = matches.length;
      if (!n) return;
      setActive((a) => (e.key === "ArrowDown" ? (a + 1) % n : (a - 1 + n) % n));
    } else if (e.key === "Enter") {
      const pick = active >= 0 ? matches[active] : matches.length === 1 ? matches[0] : undefined;
      if (pick && open) {
        e.preventDefault();
        select(pick);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      } else if (query || value) {
        e.preventDefault();
        clear();
      }
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className="cc-pathsearch relative" onBlur={(e) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
        setOpen(false);
        setQuery(null);
      }
    }}>
      <FolderIcon className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input
        role="combobox"
        aria-label="Filter by project path"
        aria-expanded={shown}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={shown && active >= 0 ? optId(active) : undefined}
        autoComplete="off"
        spellCheck={false}
        placeholder="Filter by project path…"
        className="cc-mono w-[320px] pl-7 pr-7"
        value={query ?? (selected ? abbreviateHome(selected.title, home) : "")}
        title={selected?.title}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setActive(-1);
        }}
        onKeyDown={onKeyDown}
      />
      {(selected || query) && (
        <button
          type="button"
          className="cc-iconbtn absolute right-1 top-1/2 -translate-y-1/2"
          aria-label="Clear project filter"
          title="Clear"
          onMouseDown={(e) => e.preventDefault()}
          onClick={clear}
        >
          <XIcon className="size-3.5" />
        </button>
      )}
      {shown && (
        <ul id={listId} role="listbox" aria-label="Projects" className="cc-pathlist">
          {matches.map((o, i) => (
            <li
              key={o.id}
              id={optId(i)}
              role="option"
              aria-selected={o.id === value}
              data-active={i === active || undefined}
              className="cc-pathopt"
              title={o.title}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => select(o)}
            >
              <span className="cc-mono cc-pathopt-title">
                <Highlighted text={abbreviateHome(o.title, home)} tokens={tokens} />
              </span>
              {!o.confirmed && <span className="cc-pathopt-tag">folder not found</span>}
              <span className="cc-pathopt-count">{o.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
