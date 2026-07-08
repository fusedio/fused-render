// Notion-style emoji picker popover for bookmark icons: search box, emoji
// grid grouped by category, and a Remove action that restores the default ★.
// Pure presentation — the caller owns positioning (anchor rect) and persists
// the chosen icon.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

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
}

export default function IconPicker({ anchor, onPick, onRemove, onClose }: IconPickerProps) {
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onDocMouseDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

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
  }, [anchor]);

  const q = query.trim().toLowerCase();
  const sections = CATEGORIES.map((cat) => ({
    name: cat.name,
    emoji: q ? cat.emoji.filter(([, kw]) => kw.includes(q)) : cat.emoji,
  })).filter((cat) => cat.emoji.length > 0);

  return (
    <div className="icon-picker" ref={rootRef}>
      <div className="icon-picker-head">
        <input
          ref={inputRef}
          type="text"
          className="icon-picker-search"
          placeholder="Filter…"
          value={query}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
        />
        <button className="icon-picker-remove" title="Reset to default star" onClick={onRemove}>
          Remove
        </button>
      </div>
      <div className="icon-picker-body">
        {sections.length === 0 && <div className="icon-picker-empty">No match</div>}
        {sections.map((cat) => (
          <React.Fragment key={cat.name}>
            <div className="icon-picker-cat">{cat.name}</div>
            <div className="icon-picker-grid">
              {cat.emoji.map(([emoji, kw]) => (
                <button
                  key={emoji}
                  className="icon-picker-cell"
                  title={kw}
                  onClick={() => onPick(emoji)}
                >
                  {emoji}
                </button>
              ))}
            </div>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}
