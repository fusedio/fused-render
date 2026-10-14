// "Files (N)": what this session wrote or edited, one press from the preview
// overlay. A pill that lives in the dock row beside the Agents & shells bar;
// the list is a popover anchored to it (opens upward, right-aligned), so an
// expanded list never reflows the row. Hidden when there is nothing.
import { useCallback, useState } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@platform/lib/utils";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@platform/shadcn/ui/popover";

import "../styles/sched.css";
import { useFilePreview } from "./filePreview";
import { useDismissOnWindow } from "./useDismissOnWindow";

function split(path: string): { name: string; dir: string } {
  const clean = path.replace(/\/+$/, "");
  const cut = clean.lastIndexOf("/");
  return cut < 0
    ? { name: clean, dir: "" }
    : { name: clean.slice(cut + 1) || clean, dir: clean.slice(0, cut) };
}

/** The popover's body. Split out so it renders without a portal in tests. */
export function SessionFilesList({
  files,
  onPicked,
}: {
  files: readonly string[];
  onPicked?: () => void;
}) {
  const openPreview = useFilePreview();
  return (
    <ul className="c-files-list">
      {files.map((p) => {
        const { name, dir } = split(p);
        return (
          <li key={p}>
            <button
              type="button"
              className="c-files-row"
              title={p}
              disabled={!openPreview}
              onClick={() => {
                openPreview?.(p);
                onPicked?.();
              }}
            >
              <span className="c-files-name">{name}</span>
              {dir ? <span className="c-files-dir">{dir}</span> : null}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function SessionFiles({ files }: { files: readonly string[] }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  // A click inside the preview iframe never reaches this document.
  useDismissOnWindow(open, close);
  if (!files.length) return null;
  return (
    <section className={cn("c-files", open && "is-open")} aria-label="Files changed in this chat">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <button type="button" className="c-files-head art-chip" aria-expanded={open}>
              <ChevronRight className="c-files-chev" size={12} aria-hidden="true" />
              <span className="art-chip-lbl">Files ({files.length})</span>
            </button>
          }
        />
        <PopoverContent
          side="top"
          align="end"
          sideOffset={6}
          aria-label="Files changed in this chat"
          className="c-overlay c-files-pop w-auto min-w-0 flex-col gap-0 rounded-[10px] bg-[var(--c-panel)] p-1 text-[var(--c-fg)] shadow-none ring-0"
        >
          <SessionFilesList files={files} onPicked={close} />
        </PopoverContent>
      </Popover>
    </section>
  );
}
