// Mode menu for pane/tab chrome (Panel's pane bar, Tabs' active tab). This
// module owns the DATA half — statting the pane's live location, resolving
// condition.py gates, and rewriting the pane-local `_mode` (same
// default-deletes rule as Preview's setMode, PT-9) before handing the new
// query to the caller, which reloads its iframe imperatively (crumb-click
// discipline — no React re-render may touch a live iframe).
//
// The PRESENTATION half depends on where it lands, hence `variant`:
//
//   "bar" (Panel's pane bar) renders the shared ModeMenu — the same icon-chip
//         + name + caret control the title bar and the preview pane carry.
//   "tab" (Tabs' active tab) keeps the icon-only span trigger: that trigger
//         lives INSIDE the tab's <button>, where a nested <button> would be
//         invalid HTML and a labelled control would not fit anyway.
import { useEffect, useState } from "react";
import { resolveConditions, statPath, type TemplateEntry } from "@platform/lib/api";
import { Spinner } from "@platform/shadcn/ui/spinner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import {
  isModePending,
  visibleModes,
  defaultMode,
  effectiveActive,
} from "@platform/lib/mode-visibility";
import { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import { ModeMenu } from "@apps/explorer/BarMenu";

// Split a pane query at its raw `_layout=(...)` span (kept byte-identical —
// it may contain literal `&`), so the head is plain params URLSearchParams
// can edit. Same discipline as Tabs' composeFolderTabsUrl.
function splitAtLayout(query: string): [string, string] {
  const s = (query || "").replace(/^\?/, "");
  const i = s.indexOf("_layout=(");
  if (i === -1) return [s, ""];
  return [s.slice(0, i).replace(/&$/, ""), s.slice(i)];
}

interface PaneModeMenuProps {
  path: string;
  query: string;
  // Receives the pane's new query (leading "?" or empty); the caller writes
  // iframe.src = embedSrc(path, query) itself — it owns the iframe ref.
  onNavigate: (query: string) => void;
  // Which trigger to render (see the module comment). Defaults to "tab", the
  // constrained surface.
  variant?: "bar" | "tab";
}

export default function PaneModeMenu({ path, query, onNavigate, variant = "tab" }: PaneModeMenuProps) {
  const [templates, setTemplates] = useState<TemplateEntry[]>([]);
  // Deferred condition.py verdicts (CT-12): null while any gated entry is
  // unresolved. The resolveConditions call is shared with Preview's (one
  // in-flight request per path), so this costs no extra gate evaluation.
  const [conditions, setConditions] = useState<Record<string, boolean> | null>(null);
  const [open, setOpen] = useState(false);

  // Re-stat on every path change (pane navigation) so the menu tracks the
  // live location's modes. Sentinel paths (/_panel, /_tab) and stat errors
  // yield no templates — the menu hides itself below.
  useEffect(() => {
    let stale = false;
    setTemplates([]);
    setConditions(null);
    setOpen(false);
    statPath(path)
      .then((s) => {
        // Same defensive sentinel filter as Preview's dispatch (PT-12): keep
        // resolved templates and the known sentinels (`_render`, `_listing`),
        // so a directory pane's menu offers the listing beside zarr/custom views.
        if (stale) return;
        const filtered = s.templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode));
        setTemplates(filtered);
        if (!filtered.some((t) => t.conditional)) {
          setConditions({});
          return;
        }
        resolveConditions(path)
          .then((r) => {
            if (!stale) setConditions(r.conditions);
          })
          .catch(() => {
            // No verdicts. lib/mode-visibility keeps verdict-less gated
            // entries VISIBLE — a failed probe must not silently empty this
            // menu (a menu of one hides itself entirely).
            if (!stale) setConditions({});
          });
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [path]);

  // Close on window blur — a click landing in any iframe never reaches this
  // document (so base-ui's outside-press detection can't see it), but it does
  // blur the shell window.
  useEffect(() => {
    if (!open) return;
    const onBlur = () => setOpen(false);
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, [open]);

  // Visibility/pending policy lives in lib/mode-visibility, shared with
  // Preview, ListingPreviewPane and Open With so every surface offers the
  // same mode set for the same path. The default (and the trigger's
  // fallback) is the first UNCONDITIONAL entry — a gated template is never
  // the default while a normal one exists (CT-12).
  const activeMode = new URLSearchParams(splitAtLayout(query)[0]).get("_mode");
  const isPending = (t: TemplateEntry) => isModePending(t, conditions);
  const visible = visibleModes(templates, conditions);
  if (visible.length < 2) return null;

  const defaultEntry = defaultMode(visible) as TemplateEntry;
  // A pending entry can't be the trigger's label (it has no icon yet), so it
  // falls back to the default too — otherwise this is the shared resolution.
  const requested = effectiveActive(visible, activeMode);
  const active = requested && !isPending(requested) ? requested : defaultEntry;

  const applyMode = (mode: string) => {
    if (mode === active.mode) return;
    const [head, tail] = splitAtLayout(query);
    const params = new URLSearchParams(head);
    // Selecting the default mode DELETES _mode (clean URLs, PT-9).
    if (mode === defaultEntry.mode) params.delete("_mode");
    else params.set("_mode", mode);
    const qs = params.toString();
    const q = qs + (tail ? (qs ? "&" : "") + tail : "");
    onNavigate(q ? "?" + q : "");
  };

  // Pane bar: the shared control. Its own open/close handling lives in
  // BarMenu, so the local `open` state stays unused on this branch.
  if (variant === "bar") {
    return (
      <ModeMenu
        entries={visible.map((t) => ({
          mode: t.mode,
          icon: templateModeIcon(t),
          pending: isPending(t),
        }))}
        active={active.mode}
        onSelect={applyMode}
      />
    );
  }

  // Tab variant: the trigger is a SPAN, not a button — it lives INSIDE the
  // tab's own <button>, where a nested <button> would be invalid HTML. The
  // stopPropagation keeps the open click from also activating the tab.
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        render={
          <span
            role="button"
            tabIndex={-1}
            className="inline-flex size-5 cursor-default items-center justify-center rounded-sm hover:bg-muted"
            title={"Mode: " + modeTitle(active.mode)}
            onClick={(e) => e.stopPropagation()}
          />
        }
      >
        {templateModeIcon(active)}
      </DropdownMenuTrigger>
      <DropdownMenuContent aria-label="View mode" className="w-auto min-w-32">
        <DropdownMenuRadioGroup
          value={active.mode}
          onValueChange={(value) => applyMode(String(value))}
        >
          {visible.map((t) => (
            <DropdownMenuRadioItem
              key={t.mode}
              value={t.mode}
              closeOnClick
              disabled={isPending(t)}
              title={isPending(t) ? "Checking if this view applies…" : undefined}
            >
              <span className="flex size-4 items-center justify-center">
                {isPending(t) ? <Spinner /> : templateModeIcon(t)}
              </span>
              {modeTitle(t.mode)}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
