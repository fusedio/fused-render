// Notion-style icon picker popover: an Emoji tab (the whole Unicode set, from
// emojibase), an Icons tab (the whole lucide set, branded on pick as a black
// rounded square with the glyph in fused yellow), a filter box, a shuffle
// button that picks at random from the active tab, a Recent row per tab, and
// a Remove action that restores the caller's default glyph.
//
// Pure presentation — the caller owns positioning (anchor rect) and persists
// the pick. Both data sets load lazily on first open: `emojibase-data` and the
// vanilla `lucide` package are imported by nothing else in the shell, so they
// land in chunks of their own instead of the main bundle (a dynamic import of
// `lucide-react` would not — it is statically imported all over the shell).
import React, {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Search, Shuffle } from "lucide-react";

import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";

export type IconPickerTab = "emoji" | "icon";

/** What a pick hands back. An icon pick carries the finished branded svg so a
 *  caller that stores files (the Projects rows' icon.svg) writes it as is. */
export type IconPick =
  | { kind: "emoji"; emoji: string }
  | { kind: "icon"; name: string; svg: string };

interface IconPickerProps {
  /** Viewport rect of the glyph that opened the picker. */
  anchor: { top: number; left: number };
  onPick: (pick: IconPick) => void;
  onRemove: () => void;
  onClose: () => void;
  /** CSS selector for the glyphs that toggle this picker. A mousedown on
   *  one of them is left to the host's click handler (the toggle), everything
   *  else closes. Each host must scope it to its own glyphs: two sections
   *  sharing a loose selector leave each other's pickers open (Bugbot,
   *  2026-08-31). Defaults to the Bookmarks section's glyphs. */
  toggleSelector?: string;
  /** Which tabs to offer. Bookmarks store a single emoji string and can't take
   *  an svg, so they pass ["emoji"]; the Projects rows take both. */
  tabs?: IconPickerTab[];
}

// ---- brand -----------------------------------------------------------------

/** fused yellow, baked in: icon.svg is a static file with no theme context, and
 *  the light theme's `--accent` is a darker olive (tokens.css) that would read
 *  wrong on the black plate anyway. */
const BRAND_YELLOW = "#E5FF44";
const BRAND_BLACK = "#000000";

type IconNode = [tag: string, attrs: Record<string, string | number>][];

function escapeAttr(v: string | number): string {
  return String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/** A lucide icon on the brand plate as a standalone icon.svg document: a
 *  64-unit square, black rounded rect, the 24-unit glyph scaled 1.75× and
 *  centred with 11 units of margin, stroked in fused yellow. */
export function brandedIconSvg(node: IconNode): string {
  const inner = node
    .map(([tag, attrs]) => {
      const a = Object.entries(attrs)
        .filter(([k]) => k !== "key")
        .map(([k, v]) => ` ${k}="${escapeAttr(v)}"`)
        .join("");
      return `<${tag}${a}/>`;
    })
    .join("");
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    `<rect width="64" height="64" rx="14" fill="${BRAND_BLACK}"/>` +
    // 1.75: a 42-unit glyph with 11 of margin — at favicon size the plate
    // vanishes into a dark tab strip and only the glyph shows, so it has to
    // carry the icon on its own.
    '<g transform="translate(11 11) scale(1.75)" fill="none" ' +
    // 2.5 not lucide's 2: the row draws the file at 14px, where a 2-unit
    // stroke lands under a pixel and reads faint beside the emoji rows.
    `stroke="${BRAND_YELLOW}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">` +
    inner +
    "</g></svg>"
  );
}

// ---- data ------------------------------------------------------------------

interface Cell {
  /** Stable id: the emoji itself, or the icon's kebab name. */
  id: string;
  /** Lower-cased haystack the filter runs `includes` over. */
  search: string;
  /** Hover title. */
  title: string;
  node?: IconNode;
}

interface Section {
  name: string;
  cells: Cell[];
}

/** Keywords the emojibase tags don't carry — the old curated table's developer
 *  vocabulary, kept so "python" and "docker" still find their emoji. */
const EXTRA_KEYWORDS: Record<string, string> = {
  "🐍": "python",
  "🦀": "rust",
  "🐳": "docker",
  "🐙": "github",
  "🚀": "launch ship",
  "🐛": "debug",
  "🧪": "test lab",
  "📊": "analytics",
  "🗄️": "database",
  "💾": "database",
  "🖥️": "server",
  "💻": "code",
  "⚙️": "settings config",
  "📦": "release",
  "🔒": "private",
  "📤": "export",
  "📥": "import",
  "🌍": "world geo",
  "🗺️": "geo",
  "🛰️": "imagery",
  "🤖": "ai bot",
  "🧠": "ml intelligence",
  "✅": "done todo",
  "💡": "idea",
  "🔄": "refresh sync",
};

// emojibase group ids: 2 is "component" (skin swatches, hair) — not pickable
// icons. Regional indicators carry no group at all.
const COMPONENT_GROUP = 2;

let emojiCache: Promise<Section[]> | null = null;
function loadEmoji(): Promise<Section[]> {
  if (!emojiCache) {
    emojiCache = Promise.all([
      import("emojibase-data/en/compact.json"),
      import("emojibase-data/en/messages.json"),
    ]).then(([compact, messages]) => {
      type Compact = { group?: number; label: string; order: number; tags?: string[]; unicode: string };
      const list = (compact.default as Compact[])
        .filter((e) => e.group !== undefined && e.group !== COMPONENT_GROUP)
        .sort((a, b) => a.order - b.order);
      const groups = (messages.default as { groups: { key: string; message: string; order: number }[] })
        .groups;
      const byGroup = new Map<number, Cell[]>();
      for (const e of list) {
        const extra = EXTRA_KEYWORDS[e.unicode] ?? "";
        const cell: Cell = {
          id: e.unicode,
          title: e.label,
          search: [e.label, ...(e.tags ?? []), extra].join(" ").toLowerCase(),
        };
        const arr = byGroup.get(e.group!) ?? [];
        arr.push(cell);
        byGroup.set(e.group!, arr);
      }
      return groups
        .slice()
        .sort((a, b) => a.order - b.order)
        .filter((g) => byGroup.has(g.order))
        .map((g) => ({
          name: g.message.replace(/^\w/, (c) => c.toUpperCase()),
          cells: byGroup.get(g.order)!,
        }));
    });
  }
  return emojiCache;
}

let iconCache: Promise<Section[]> | null = null;
function loadIcons(): Promise<Section[]> {
  if (!iconCache) {
    iconCache = import("lucide").then((m) => {
      // `icons` includes aliases re-exporting the same node array — keep the
      // first (canonical) name per array.
      const seen = new Set<IconNode>();
      const cells: Cell[] = [];
      for (const [pascal, node] of Object.entries(m.icons as Record<string, IconNode>)) {
        if (seen.has(node)) continue;
        seen.add(node);
        const words = pascal
          .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
          .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
          .toLowerCase();
        cells.push({ id: words.replace(/ /g, "-"), title: words, search: words, node });
      }
      cells.sort((a, b) => a.id.localeCompare(b.id));
      return [{ name: "Icons", cells }];
    });
  }
  return iconCache;
}

function useSections(tab: IconPickerTab): Section[] | null {
  const [state, setState] = useState<Partial<Record<IconPickerTab, Section[]>>>({});
  useEffect(() => {
    if (state[tab]) return;
    let live = true;
    (tab === "emoji" ? loadEmoji() : loadIcons()).then((sections) => {
      if (live) setState((cur) => ({ ...cur, [tab]: sections }));
    });
    return () => {
      live = false;
    };
  }, [tab, state]);
  return state[tab] ?? null;
}

// ---- recent ----------------------------------------------------------------

const RECENT_MAX = 16;
const recentKey = (tab: IconPickerTab) => `fused-render:icon-picker-recent:${tab}`;

function readRecent(tab: IconPickerTab): string[] {
  try {
    const raw = localStorage.getItem(recentKey(tab));
    const v = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function pushRecent(tab: IconPickerTab, id: string) {
  try {
    const next = [id, ...readRecent(tab).filter((x) => x !== id)].slice(0, RECENT_MAX);
    localStorage.setItem(recentKey(tab), JSON.stringify(next));
  } catch {
    // Storage full or blocked: Recent is a convenience, the pick still lands.
  }
}

// ---- component -------------------------------------------------------------

const GRID_COLS = 8;
const CHUNK_ROWS = 8;
const TAB_LABEL: Record<IconPickerTab, string> = { emoji: "Emoji", icon: "Icons" };

function chunk<T>(list: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

/** One lucide glyph drawn inline from its node data, on currentColor. */
function LucideGlyph({ node, className }: { node: IconNode; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {node.map(([tag, attrs], i) => React.createElement(tag, { ...attrs, key: i }))}
    </svg>
  );
}

export default function IconPicker({
  anchor,
  onPick,
  onRemove,
  onClose,
  toggleSelector = ".bookmark-glyph:not(.folder-glyph):not(.current-app-glyph)",
  tabs = ["emoji", "icon"],
}: IconPickerProps) {
  const [tab, setTab] = useState<IconPickerTab>(tabs[0] ?? "emoji");
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const baseId = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const restoreRef = useRef<Element | null>(null);
  const sections = useSections(tab);

  // Capture the opener on mount and restore focus to it on unmount (Esc or a
  // pick), so focus never drops to <body> when the autofocused search unmounts.
  useEffect(() => {
    restoreRef.current = document.activeElement;
    return () => {
      (restoreRef.current as HTMLElement | null)?.focus?.();
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
    const onDocMouseDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (rootRef.current && rootRef.current.contains(target)) return;
      // Clicks on this picker's own trigger glyphs are the toggle — let the
      // host's click handler decide (closing here would make it reopen the
      // picker immediately after). Anything else — another section's glyphs
      // included — closes.
      if (target.closest(toggleSelector)) return;
      onClose();
    };
    // Capture phase + stopPropagation so Escape closes only the picker — a
    // host Modal's document-level (bubble) Esc handler must never see it.
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    // The popover is position:fixed against a one-shot anchor rect; any
    // scroll outside it would detach it from its glyph, so close instead.
    const onScroll = (e: Event) => {
      if (rootRef.current && rootRef.current.contains(e.target as Node)) return;
      onClose();
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose, toggleSelector]);

  // Keep the popover on-screen: it opens below the glyph, flips above when it
  // would overflow the bottom edge. Query, tab and data arrival all change the
  // height, so reposition on each.
  const loaded = sections !== null;
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    let top = anchor.top + 20;
    if (top + el.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, anchor.top - el.offsetHeight - 6);
    }
    el.style.top = `${top}px`;
    el.style.left = `${Math.min(anchor.left, window.innerWidth - el.offsetWidth - 8)}px`;
  }, [anchor, query, tab, loaded]);

  const all = useMemo(() => (sections ?? []).flatMap((s) => s.cells), [sections]);
  const byId = useMemo(() => new Map(all.map((c) => [c.id, c])), [all]);

  const q = query.trim().toLowerCase();
  const visible = useMemo<Section[]>(() => {
    if (!sections) return [];
    if (q) {
      const hits = all.filter((c) => c.search.includes(q));
      return hits.length ? [{ name: "Results", cells: hits }] : [];
    }
    const recent = readRecent(tab)
      .map((id) => byId.get(id))
      .filter((c): c is Cell => !!c);
    return recent.length ? [{ name: "Recent", cells: recent }, ...sections] : sections;
  }, [sections, all, byId, q, tab]);

  // Flat order of the visible grid, for arrow-key navigation. `active` indexes
  // into this list; the search input keeps focus and exposes the highlighted
  // cell via aria-activedescendant. Sections start each grid row fresh, but a
  // single flat ±GRID_COLS Up/Down is predictable enough across them.
  const flat = useMemo(() => visible.flatMap((s) => s.cells), [visible]);
  const activeIdx = Math.min(active, Math.max(0, flat.length - 1));
  const cellId = (i: number) => `${baseId}-cell-${i}`;

  const pick = useCallback(
    (cell: Cell) => {
      pushRecent(tab, cell.id);
      if (cell.node) onPick({ kind: "icon", name: cell.id, svg: brandedIconSvg(cell.node) });
      else onPick({ kind: "emoji", emoji: cell.id });
    },
    [tab, onPick],
  );

  // Random draws from the whole tab, not the filtered view: "surprise me"
  // shouldn't depend on what happens to be typed in the box.
  const random = () => {
    if (all.length === 0) return;
    pick(all[Math.floor(Math.random() * all.length)]);
  };

  const moveActive = (delta: number) => {
    if (flat.length === 0) return;
    const next = Math.max(0, Math.min(flat.length - 1, activeIdx + delta));
    setActive(next);
    document.getElementById(cellId(next))?.scrollIntoView({ block: "nearest" });
  };

  const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        moveActive(1);
        break;
      case "ArrowLeft":
        e.preventDefault();
        moveActive(-1);
        break;
      case "ArrowDown":
        e.preventDefault();
        moveActive(GRID_COLS);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveActive(-GRID_COLS);
        break;
      case "Enter":
        e.preventDefault();
        if (flat[activeIdx]) pick(flat[activeIdx]);
        break;
      // Escape is handled by the document-level listener (closes the popover).
    }
  };

  const switchTab = (next: IconPickerTab) => {
    setTab(next);
    setQuery("");
    setActive(0);
    inputRef.current?.focus();
  };

  // Track the flat position while rendering the grouped sections.
  let flatIdx = 0;

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label="Choose icon"
      // data-slot: the shell re-declares Tailwind's shadow tokens on [data-slot]
      // elements only (tokens.css stores bare colours in --shadow-*).
      data-slot="icon-picker"
      className="fixed z-[1001] flex w-[292px] flex-col gap-2 rounded-xl border border-border bg-popover p-2 text-popover-foreground shadow-md"
    >
      <div className="flex items-center justify-between gap-2 border-b border-border pb-1">
        {tabs.length > 1 ? (
          <Tabs value={tab} onValueChange={(v) => switchTab(v as IconPickerTab)}>
            <TabsList variant="line" className="h-7">
              {tabs.map((t) => (
                <TabsTrigger key={t} value={t} className="px-2 text-[13px]">
                  {TAB_LABEL[t]}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        ) : (
          // A one-tab picker (Bookmarks) shows the name as a plain heading —
          // a single underlined tab would suggest a choice that isn't there.
          <div className="px-2 text-[13px] font-medium">{TAB_LABEL[tab]}</div>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground"
          title="Reset to the default glyph"
          onClick={onRemove}
        >
          Remove
        </Button>
      </div>

      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <Search
            className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            ref={inputRef}
            type="text"
            className="h-7 pl-7 text-[13px] md:text-[13px]"
            placeholder="Filter…"
            aria-label={`Filter ${TAB_LABEL[tab].toLowerCase()}`}
            role="combobox"
            aria-expanded="true"
            aria-controls={`${baseId}-grid`}
            aria-activedescendant={flat.length > 0 ? cellId(activeIdx) : undefined}
            value={query}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onSearchKeyDown}
          />
        </div>
        <Button
          variant="outline"
          size="icon-sm"
          title={`Random ${tab === "icon" ? "icon" : "emoji"}`}
          aria-label={`Random ${tab === "icon" ? "icon" : "emoji"}`}
          disabled={!loaded}
          onClick={random}
        >
          <Shuffle />
        </Button>
      </div>

      <div
        id={`${baseId}-grid`}
        role="listbox"
        aria-label={TAB_LABEL[tab]}
        className="max-h-[288px] overflow-y-auto"
      >
        {!loaded && <div className="px-1 py-3 text-xs text-muted-foreground">Loading…</div>}
        {loaded && visible.length === 0 && (
          <div className="px-1 py-3 text-xs text-muted-foreground">No match</div>
        )}
        {visible.flatMap((section) =>
          // Each section is cut into blocks of CHUNK_ROWS grid rows, each with
          // its own content-visibility: the browser then skips laying out the
          // blocks below the fold. One block per section would skip nothing —
          // the Icons tab is a single ~1800-cell section whose top edge is
          // always on screen.
          chunk(section.cells, GRID_COLS * CHUNK_ROWS).map((cells, c) => (
          <div
            key={`${section.name}-${c}`}
            className="[content-visibility:auto] [contain-intrinsic-size:auto_270px]"
          >
            {c === 0 && (
              <div className="px-1 pt-2 pb-1 text-[11px] font-medium text-muted-foreground">
                {section.name}
              </div>
            )}
            <div className="grid grid-cols-8 gap-0.5">
              {cells.map((cell) => {
                const i = flatIdx++;
                const isActive = i === activeIdx;
                return (
                  <button
                    key={cell.id}
                    id={cellId(i)}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    tabIndex={-1}
                    data-slot="icon-picker-cell"
                    title={cell.title}
                    onClick={() => pick(cell)}
                    className={cn(
                      "flex size-8 cursor-pointer items-center justify-center rounded-md border-0 bg-transparent p-0 text-[19px] leading-none text-foreground hover:bg-muted",
                      isActive && "bg-muted ring-2 ring-ring ring-inset",
                    )}
                  >
                    {cell.node ? <LucideGlyph node={cell.node} className="size-[18px]" /> : cell.id}
                  </button>
                );
              })}
            </div>
          </div>
          )),
        )}
      </div>
    </div>
  );
}
