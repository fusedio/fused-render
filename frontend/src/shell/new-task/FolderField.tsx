// The path is a combobox, Google-style: focusing it drops the last few folders
// the user scheduled against, with Browse and New folder as the list's last
// rows (Akshil, 2026-08-15). The list is anchored to the input and never takes
// focus from it (AnchoredPopup); blur closes it, but only when focus truly
// leaves the wrap — clicking a row moves focus INTO the list, and closing on
// that blur would eat the click.
import type { MutableRefObject } from "react";
import { FolderIcon, PlusIcon } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Input } from "@platform/shadcn/ui/input";
import { Separator } from "@platform/shadcn/ui/separator";
import { cn } from "@platform/lib/utils";
import { AnchoredPopup } from "./AnchoredPopup";
import { PICKER_ROW } from "./ExplorerPanel";
import { IconRow } from "./IconRow";
import { RECENTS_SHOWN } from "./paths";

export function FolderField({
  target,
  onChange,
  pathRef,
  pathError,
  pathErrorId,
  newFolder,
  newFolderId,
  recents,
  open,
  onOpen,
  onClose,
  suppressOpen,
  onBrowse,
  onNewFolder,
}: {
  target: string;
  onChange: (next: string) => void;
  pathRef: MutableRefObject<HTMLInputElement | null>;
  pathError: string | null;
  pathErrorId: string;
  // The name a typed path would create, shown as a ROW IN THE LIST beside the
  // folders that already exist (Akshil, 2026-08-20: "this UI should be in
  // dropdown") rather than as a note that pushed the form down as you typed.
  newFolder: string | null;
  newFolderId: string;
  recents: string[];
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  // Escape-from-a-row hands focus back to the field WITHOUT reopening the list
  // it just dismissed — the input's onFocus otherwise undoes the close in the
  // same tick.
  suppressOpen: MutableRefObject<boolean>;
  onBrowse: () => void;
  onNewFolder: () => void;
}) {
  return (
    <IconRow icon={<FolderIcon />}>
      <div
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onClose();
        }}
        // On the WRAP, not the input: a row reached by Tab is focusable too,
        // and Escape from it must dismiss the list, not bubble to the dialog's
        // close handler (Bugbot, PR #541).
        onKeyDown={(e) => {
          if (e.key === "Escape" && open) {
            e.stopPropagation();
            onClose();
            if (document.activeElement !== pathRef.current) {
              suppressOpen.current = true;
              pathRef.current?.focus();
            }
          }
        }}
      >
        <Input
          ref={pathRef}
          type="text"
          className={cn("h-7 font-mono text-xs", pathError && "border-destructive")}
          aria-invalid={pathError !== null}
          // The new-folder row only exists while the list is open, so it is
          // only pointed at while it is there — a describedby aimed at a node
          // that is not in the document says nothing at all.
          aria-describedby={
            pathError ? pathErrorId : newFolder && open ? newFolderId : undefined
          }
          placeholder="Add folder or file"
          role="combobox"
          aria-expanded={open}
          value={target}
          onFocus={() => {
            if (suppressOpen.current) {
              suppressOpen.current = false;
              return;
            }
            onOpen();
          }}
          onClick={onOpen}
          onChange={(e) => onChange(e.target.value)}
        />
        <AnchoredPopup open={open} onClose={onClose} anchor={pathRef} matchWidth>
          {/* What the typed path IS, answered where the other answers about
              folders are — first row, in the same row shape. A BUTTON like
              every row around it: picking it picks the path the field already
              holds, so the click's whole job is to close the list. */}
          {!pathError && newFolder && (
            <button
              type="button"
              id={newFolderId}
              className={cn(PICKER_ROW, "items-start")}
              onClick={onClose}
            >
              <FolderIcon className="mt-0.5" />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="flex items-center gap-2">
                  <span className="truncate font-mono text-xs" title={newFolder}>
                    {newFolder}
                  </span>
                  <Badge variant="secondary">New folder</Badge>
                </span>
                <span className="text-xs text-muted-foreground">
                  Created when the task is saved
                </span>
              </span>
            </button>
          )}
          {recents.slice(0, RECENTS_SHOWN).map((p) => (
            <button
              key={p}
              type="button"
              className={PICKER_ROW}
              onClick={() => {
                onChange(p);
                onClose();
              }}
            >
              <FolderIcon />
              <span className="truncate font-mono text-xs" title={p}>{p}</span>
            </button>
          ))}
          <Separator className="my-1" />
          <button type="button" className={PICKER_ROW} onClick={() => { onClose(); onBrowse(); }}>
            {/* The empty icon column, so the verb's label starts on the same
                edge as every folder above it. */}
            <span className="size-3.5 shrink-0" aria-hidden="true" />
            Browse…
          </button>
          {/* The second verb, under Browse (Akshil, 2026-08-20): the way in for
              someone who does not yet know a typed name is allowed. Opens the
              SAME panel Browse does, already naming. */}
          <button type="button" className={PICKER_ROW} onClick={() => { onClose(); onNewFolder(); }}>
            <PlusIcon />
            New folder
          </button>
        </AnchoredPopup>
      </div>
      {pathError && (
        <p id={pathErrorId} className="mt-1 text-xs text-destructive" role="alert">
          {pathError}
        </p>
      )}
    </IconRow>
  );
}
