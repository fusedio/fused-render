// Notion-style emoji picker popover for bookmark icons: search box, emoji
// grid grouped by category, and a Remove action that restores the default ★.
// Pure presentation — the caller owns positioning (anchor rect) and persists
// the chosen icon.
import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Empty, EmptyDescription, EmptyHeader } from "@platform/shadcn/ui/empty";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@platform/shadcn/ui/input-group";
import { SearchIcon } from "lucide-react";

interface Category {
  name: string;
  // [emoji, space-separated search keywords]
  emoji: [string, string][];
}

const CATEGORIES: Category[] = [
  {
    name: "Frequent",
    emoji: [
      ["⭐", "star favorite"],
      ["📌", "pin pushpin"],
      ["📁", "folder directory"],
      ["📄", "page document file"],
      ["📊", "chart bar graph analytics"],
      ["📈", "chart up trending growth"],
      ["🗺️", "map geo"],
      ["🌍", "globe earth world"],
      ["🏠", "home house"],
      ["🔥", "fire hot"],
      ["✅", "check done todo"],
      ["🚀", "rocket launch ship"],
      ["💡", "idea bulb light"],
      ["🔖", "bookmark tag"],
      ["🧪", "test experiment lab"],
      ["🐛", "bug debug"],
    ],
  },
  {
    name: "Work",
    emoji: [
      ["📅", "calendar date schedule"],
      ["🗂️", "dividers files organize"],
      ["🗃️", "card box archive"],
      ["📋", "clipboard list tasks"],
      ["📝", "memo note write"],
      ["✏️", "pencil edit"],
      ["📎", "paperclip attach"],
      ["🔍", "search magnify find"],
      ["🔒", "lock secure private"],
      ["🔑", "key access secret"],
      ["⚙️", "gear settings config"],
      ["🛠️", "tools hammer wrench build"],
      ["🔧", "wrench fix tool"],
      ["📦", "package box release"],
      ["🗄️", "cabinet database storage"],
      ["💾", "disk save database"],
      ["🖥️", "computer desktop server"],
      ["💻", "laptop code"],
      ["⌨️", "keyboard type"],
      ["🖨️", "printer print"],
      ["📤", "outbox export upload"],
      ["📥", "inbox import download"],
      ["✉️", "mail email envelope"],
      ["💼", "briefcase work business"],
    ],
  },
  {
    name: "Data & science",
    emoji: [
      ["📉", "chart down decline"],
      ["🧮", "abacus math calculate"],
      ["🔬", "microscope science research"],
      ["🔭", "telescope astronomy"],
      ["🧬", "dna genetics bio"],
      ["⚗️", "alembic chemistry"],
      ["🧲", "magnet attract"],
      ["📐", "ruler triangle measure"],
      ["🌡️", "thermometer temperature weather"],
      ["⚡", "zap lightning fast energy"],
      ["🛰️", "satellite space imagery"],
      ["📡", "antenna signal dish"],
      ["🤖", "robot ai bot"],
      ["🧠", "brain ml intelligence"],
    ],
  },
  {
    name: "Nature & places",
    emoji: [
      ["🌎", "globe americas world"],
      ["🌏", "globe asia world"],
      ["🗾", "map japan"],
      ["🏔️", "mountain peak terrain"],
      ["🌋", "volcano eruption"],
      ["🏖️", "beach coast"],
      ["🌊", "wave ocean water"],
      ["🌲", "tree evergreen forest"],
      ["🌱", "seedling plant grow"],
      ["🌸", "blossom flower"],
      ["☀️", "sun sunny weather"],
      ["🌙", "moon night"],
      ["☁️", "cloud weather"],
      ["🌧️", "rain weather"],
      ["❄️", "snow snowflake winter"],
      ["🌈", "rainbow color"],
      ["🏙️", "city skyline urban"],
      ["🏗️", "construction crane building"],
      ["🏭", "factory industry"],
      ["🛣️", "road highway"],
      ["✈️", "airplane flight travel"],
      ["🚗", "car auto vehicle"],
      ["🚂", "train locomotive rail"],
      ["🚢", "ship boat vessel"],
    ],
  },
  {
    name: "Symbols",
    emoji: [
      ["❤️", "heart love red"],
      ["🧡", "heart orange"],
      ["💚", "heart green"],
      ["💙", "heart blue"],
      ["💜", "heart purple"],
      ["🟥", "square red"],
      ["🟧", "square orange"],
      ["🟨", "square yellow"],
      ["🟩", "square green"],
      ["🟦", "square blue"],
      ["🟪", "square purple"],
      ["⬛", "square black"],
      ["🔴", "circle red dot"],
      ["🟠", "circle orange dot"],
      ["🟡", "circle yellow dot"],
      ["🟢", "circle green dot"],
      ["🔵", "circle blue dot"],
      ["🟣", "circle purple dot"],
      ["⚠️", "warning caution alert"],
      ["❗", "exclamation important"],
      ["❓", "question help"],
      ["🚫", "prohibited no ban"],
      ["♻️", "recycle refresh"],
      ["🔄", "arrows refresh sync"],
      ["➕", "plus add new"],
      ["🎯", "target dart goal"],
      ["🏁", "flag finish checkered"],
      ["🚩", "flag red marker"],
      ["🎉", "party celebrate tada"],
      ["💎", "gem diamond"],
      ["🏆", "trophy win award"],
      ["⏰", "alarm clock time"],
      ["⏳", "hourglass pending time"],
      ["🔔", "bell notification"],
      ["👀", "eyes watch look"],
      ["🎨", "art palette design"],
      ["🎵", "music note"],
      ["📷", "camera photo image"],
      ["🎥", "movie camera video"],
      ["🍕", "pizza food"],
      ["☕", "coffee cafe"],
      ["🐍", "snake python"],
      ["🦀", "crab rust"],
      ["🐳", "whale docker"],
      ["🐙", "octopus github"],
    ],
  },
];

interface IconPickerProps {
  anchor: { top: number; left: number }; // viewport coords of the glyph
  onPick: (icon: string) => void;
  onRemove: () => void;
  onClose: () => void;
  /** Selector for THIS picker's own trigger glyphs — an outside mousedown on
   *  one of them is left to the host's click handler (the toggle), everything
   *  else closes. Each host must scope it to its own glyphs: two sections
   *  sharing a loose selector leave each other's pickers open (Bugbot,
   *  2026-08-31). Defaults to the Bookmarks section's glyphs. */
  toggleSelector?: string;
}

const GRID_COLS = 8;

export default function IconPicker({
  anchor,
  onPick,
  onRemove,
  onClose,
  toggleSelector = ".bookmark-glyph:not(.folder-glyph):not(.current-app-glyph)",
}: IconPickerProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const baseId = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<Element | null>(null);

  // Capture the opener on mount and restore focus to it on unmount (Esc or a
  // pick), so focus never drops to <body> when the autofocused search unmounts.
  useEffect(() => {
    restoreRef.current = document.activeElement;
    return () => {
      (restoreRef.current as HTMLElement | null)?.focus?.();
    };
  }, []);

  useEffect(() => {
    // Found by query rather than a ref: shadcn's Input is a plain function
    // component and React 18 does not forward `ref` through it.
    rootRef.current?.querySelector("input")?.focus();
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
  // would overflow the bottom edge.
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    let top = anchor.top + 20;
    if (top + el.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, anchor.top - el.offsetHeight - 6);
    }
    el.style.top = `${top}px`;
    el.style.left = `${Math.min(anchor.left, window.innerWidth - el.offsetWidth - 8)}px`;
    // query changes the popover height (filtered grid), so reposition on it too.
  }, [anchor, query]);

  const q = query.trim().toLowerCase();
  const sections = CATEGORIES.map((cat) => ({
    name: cat.name,
    emoji: q ? cat.emoji.filter(([, kw]) => kw.includes(q)) : cat.emoji,
  })).filter((cat) => cat.emoji.length > 0);

  // Flat order of the visible grid, for arrow-key navigation. `active` indexes
  // into this list; the search input keeps focus and exposes the highlighted
  // cell via aria-activedescendant. Sections start each grid row fresh, but a
  // single flat ±GRID_COLS Up/Down is predictable enough across them.
  const flat = sections.flatMap((cat) => cat.emoji.map(([emoji]) => emoji));
  const activeIdx = Math.min(active, Math.max(0, flat.length - 1));
  const cellId = (i: number) => `${baseId}-cell-${i}`;

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
        if (flat[activeIdx]) onPick(flat[activeIdx]);
        break;
      // Escape is handled by the document-level listener (closes the popover).
    }
  };

  // Track the flat position while rendering the grouped sections.
  let flatIdx = 0;

  return (
    // `.icon-picker` stays on the root: sidebar.css owns its `position: fixed`
    // plate and z-order (the useLayoutEffect above writes top/left against
    // it), and the picker floats over a host Modal that also positions itself.
    <div className="icon-picker" ref={rootRef} role="dialog" aria-label="Choose icon">
      <div className="mb-1.5 flex items-center gap-1.5">
        <InputGroup className="h-7">
          <InputGroupAddon>
            <SearchIcon />
          </InputGroupAddon>
          <InputGroupInput
            type="text"
            className="h-7 text-xs"
            placeholder="Filter…"
            aria-label="Filter icons"
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
        </InputGroup>
        <Button variant="ghost" size="sm" title="Reset to default star" onClick={onRemove}>
          Remove
        </Button>
      </div>
      <div
        className="max-h-[260px] overflow-y-auto"
        id={`${baseId}-grid`}
        role="listbox"
        aria-label="Icons"
      >
        {sections.length === 0 && (
          <Empty className="p-3">
            <EmptyHeader>
              <EmptyDescription>No match</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {sections.map((cat) => (
          <React.Fragment key={cat.name}>
            <div className="px-1 pt-1.5 pb-1 text-xs tracking-wide uppercase text-muted-foreground">
              {cat.name}
            </div>
            <div className="grid grid-cols-8">
              {cat.emoji.map(([emoji, kw]) => {
                const i = flatIdx++;
                return (
                  <Button
                    key={emoji}
                    id={cellId(i)}
                    role="option"
                    aria-selected={i === activeIdx}
                    tabIndex={-1}
                    variant="ghost"
                    size="icon-sm"
                    className={cn("text-base", i === activeIdx && "bg-muted ring-2 ring-ring ring-inset")}
                    title={kw}
                    onClick={() => onPick(emoji)}
                  >
                    {emoji}
                  </Button>
                );
              })}
            </div>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}
