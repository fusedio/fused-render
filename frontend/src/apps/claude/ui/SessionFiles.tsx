// "Files (N)": what this session wrote or edited, one press from the preview
// overlay. Collapsed to a chip by default (the Tasks panel's idiom), hidden
// when there is nothing. Styled like the artifact chips beside it.
import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@platform/lib/utils";

import "../styles/sched.css";
import { useFilePreview } from "./filePreview";

function split(path: string): { name: string; dir: string } {
  const clean = path.replace(/\/+$/, "");
  const cut = clean.lastIndexOf("/");
  return cut < 0
    ? { name: clean, dir: "" }
    : { name: clean.slice(cut + 1) || clean, dir: clean.slice(0, cut) };
}

export function SessionFiles({ files }: { files: readonly string[] }) {
  const [open, setOpen] = useState(false);
  const openPreview = useFilePreview();
  if (!files.length) return null;
  return (
    <section className={cn("c-files", open && "is-open")} aria-label="Files changed in this chat">
      <button
        type="button"
        className="c-files-head art-chip"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronRight className="c-files-chev" size={12} aria-hidden="true" />
        <span className="art-chip-lbl">Files ({files.length})</span>
      </button>
      {open ? (
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
                  onClick={() => openPreview?.(p)}
                >
                  <span className="c-files-name">{name}</span>
                  {dir ? <span className="c-files-dir">{dir}</span> : null}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
