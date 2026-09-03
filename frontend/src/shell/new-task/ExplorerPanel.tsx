// Browse: an explorer-shaped panel BESIDE the card, not inside it — the
// in-card picker was "too small to see anything" (Akshil, 2026-08-16). It
// renders in the dialog's side column with room to show folders AND files. A
// folder click descends; a file click IS the pick (a task can target a file);
// "Use this folder" picks where you stand.
import { useEffect, useRef, useState } from "react";
import { ChevronRightIcon, FileIcon, FolderIcon } from "lucide-react";
import { listDir } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { cn } from "@platform/lib/utils";
import { collapseCrumbs, crumbsOf, normPath } from "./paths";

// The row shape every list in this card uses — the explorer's folders and
// files, and the folder field's recents (FolderField). One class string, so the
// two lists read as one pattern.
export const PICKER_ROW =
  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus-visible:bg-accent disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:text-muted-foreground";

export function ExplorerPanel({
  start,
  onPick,
  onClose,
  startNaming,
  onName,
}: {
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
  // Fired alongside onPick when the picked path is a folder the user just
  // NAMED here rather than one they clicked — the caller answers it by showing
  // that the folder is about to be created.
  onName?: () => void;
  // Opened BY "+ New folder" rather than by Browse: the panel comes up with the
  // naming row already typing, so the button below Browse and the button inside
  // the panel are one flow and not two (Akshil, 2026-08-20).
  startNaming?: boolean;
}) {
  // A file target starts the panel in its PARENT — listing a file's "children"
  // is a guaranteed error banner.
  const [path, setPath] = useState(() => {
    const p = normPath(start).replace(/\/+$/, "");
    return p || "/";
  });
  const [rows, setRows] = useState<{ name: string; dir: boolean }[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Type-to-narrow, cleared on every navigation — a filter that survives into
  // the next folder reads as "this folder is empty".
  const [filter, setFilter] = useState("");
  // One free climb: the client cannot tell a file path from a folder path
  // until listDir refuses it, so the FIRST refusal walks to the parent
  // instead of showing the error banner — Browse on an already-picked file
  // must open where that file lives (Bugbot, PR #548). One only, so a
  // genuinely missing tree still errors instead of climbing to "/".
  const climbed = useRef(false);

  useEffect(() => {
    let stale = false;
    // The OLD listing stays up, dimmed, while the next one loads — blanking
    // it made the panel pump on every click (QA 2026-08-14).
    setLoading(true);
    setError(null);
    listDir(path).then(
      (r) => {
        if (stale) return;
        setRows(
          r.entries
            .filter((e) => !e.name.startsWith("."))
            .map((e) => ({ name: e.name, dir: e.is_dir }))
            // Folders first, then files, each alphabetical — the explorer's
            // own ordering, so the panel reads like the app it stands in for.
            .sort((a, b) =>
              a.dir !== b.dir ? (a.dir ? -1 : 1) : a.name.localeCompare(b.name),
            ),
        );
        setLoading(false);
      },
      (e: Error) => {
        if (stale) return;
        const cut = path.replace(/\/+$/, "").lastIndexOf("/");
        if (!climbed.current && cut >= 0) {
          climbed.current = true;
          const parent = path.replace(/\/+$/, "").slice(0, cut);
          // A drive root keeps its slash — bare "C:" reads as cwd-relative
          // elsewhere in the shell, not as the root (Bugbot, PR #548).
          setPath(/^[A-Za-z]:$/.test(parent) ? parent + "/" : parent || "/");
          return;
        }
        setError(e.message);
        setLoading(false);
      },
    );
    return () => {
      stale = true;
    };
  }, [path]);

  // "New folder" here NAMES one; it does not make one. The folder is created by
  // the save, exactly as it is for a name typed straight into the path field —
  // so backing out of the card leaves nothing behind on disk, and the picker
  // needs no write endpoint to offer the affordance.
  const [naming, setNaming] = useState(!!startNaming);
  const [newName, setNewName] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (naming) nameRef.current?.focus();
  }, [naming]);

  const go = (p: string) => {
    setPath(p);
    setFilter("");
    // A half-typed name belongs to the folder it was being typed in.
    setNaming(false);
    setNewName("");
  };

  const typedName = newName.trim();
  // Checked against the listing already on screen — the one place that knows
  // what is in this folder. A name that is taken is not an error to shout
  // about, it is a folder the user can just click.
  const nameTaken = rows?.some((r) => r.name === typedName) ?? false;
  const nameBad = typedName.includes("/") || typedName === "." || typedName === "..";
  const canCreate = typedName !== "" && !nameTaken && !nameBad;
  const confirmName = () => {
    if (!canCreate) return;
    onPick(path.replace(/\/+$/, "") + "/" + typedName);
    onName?.();
    onClose();
  };
  const crumbs = collapseCrumbs(crumbsOf(path));
  const shown = rows?.filter((r) =>
    r.name.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  // Escape dismisses the PANEL, not the dialog behind it — captured at the
  // document before the dialog's own Escape listener can see it. Which is also
  // why the naming row cannot handle its own Escape: this listener sees the key
  // first, so it has to know there is an inner thing to back out of and undo
  // that instead. Read through a ref because the listener is bound once.
  const namingOpen = useRef(false);
  namingOpen.current = naming;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        if (namingOpen.current) {
          setNaming(false);
          setNewName("");
          return;
        }
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      role="region"
      aria-label="Choose a folder or file"
      data-new-task-explorer
    >
      <div className="border-b border-border px-4 py-2.5 text-sm font-medium">
        Choose a folder or file
      </div>
      {/* The path as clickable crumbs: every ancestor is one tap away. Past
          four segments the middle collapses to one "…", which is NOT a
          control: there is no single folder it could stand for. */}
      <div
        className="flex min-w-0 flex-wrap items-center gap-0.5 px-3 pt-2 text-xs"
        aria-label="Current folder"
      >
        {crumbs.map((c, i) =>
          c === null ? (
            <span key="gap" className="px-1 text-muted-foreground">…</span>
          ) : (
            <span key={c.path} className="flex items-center">
              {/* The root crumb IS "/", so a separator in front of the first
                  real segment prints it twice — "//Users". A drive root ("C:")
                  still takes one. */}
              {i > 0 && !(i === 1 && crumbs[0]?.name === "/") && (
                <span className="px-0.5 text-muted-foreground">/</span>
              )}
              <button
                type="button"
                className="rounded-sm px-1 py-0.5 font-mono text-xs text-muted-foreground hover:bg-accent hover:text-foreground disabled:text-foreground disabled:hover:bg-transparent"
                disabled={i === crumbs.length - 1}
                title={c.path}
                onClick={() => go(c.path)}
              >
                {c.name}
              </button>
            </span>
          ),
        )}
      </div>
      <div className="px-3 py-2">
        <Input
          type="text"
          className="h-7 text-[0.8rem]"
          placeholder="Filter this folder"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div
        className={cn(
          "min-h-0 flex-1 overflow-y-auto px-2 pb-2 transition-opacity",
          loading && "opacity-60",
        )}
      >
        {/* At the TOP of the listing, where the folder it is about to join
            would sort — a row being typed, not a dialog over the panel. */}
        {naming && (
          <div className="flex items-center gap-2 px-2 py-1 [&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:text-muted-foreground">
            <FolderIcon />
            <Input
              ref={nameRef}
              type="text"
              className="h-7 text-[0.8rem]"
              placeholder="New folder name"
              aria-label="New folder name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  confirmName();
                }
              }}
            />
            <Button type="button" size="sm" disabled={!canCreate} onClick={confirmName}>
              Use
            </Button>
          </div>
        )}
        {naming && typedName !== "" && !canCreate && (
          <p className="px-2 py-1 text-xs text-destructive">
            {nameTaken
              ? `“${typedName}” is already in this folder`
              : "A folder name can't contain a slash"}
          </p>
        )}
        {error && <p className="px-2 py-1 text-xs text-destructive">{error}</p>}
        {!error && shown?.length === 0 && !loading && (
          <p className="px-2 py-1 text-xs text-muted-foreground">
            {filter ? "Nothing matches" : "Empty folder"}
          </p>
        )}
        {!error && shown?.map(({ name, dir }) => (
          <button
            key={name}
            type="button"
            className={cn(PICKER_ROW, !dir && "text-muted-foreground")}
            disabled={loading}
            title={name}
            onClick={() => {
              const full = path.replace(/\/+$/, "") + "/" + name;
              // A folder is a place to go; a file is an ANSWER — picking one
              // finishes the errand.
              if (dir) go(full);
              else {
                onPick(full);
                onClose();
              }
            }}
          >
            {dir ? <FolderIcon /> : <FileIcon />}
            <span className="min-w-0 flex-1 truncate">{name}</span>
            {dir && <ChevronRightIcon aria-hidden="true" />}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2 border-t border-border px-3 py-2">
        {/* Left of the pair, because it acts on the folder you are IN rather
            than on the errand — same side as the crumbs it reads off. Hidden
            while a name is being typed: the row above is the control then. */}
        {!error && !naming && (
          <Button type="button" variant="ghost" size="sm" className="mr-auto" onClick={() => setNaming(true)}>
            + New folder
          </Button>
        )}
        <Button type="button" variant="outline" size="sm" className="ml-auto" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          onClick={() => {
            onPick(path);
            onClose();
          }}
        >
          Use this folder
        </Button>
      </div>
    </div>
  );
}
