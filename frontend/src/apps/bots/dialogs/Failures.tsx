// The failed model calls behind a bot's "N failed" usage count (owner, 2026-10-12: "clicking on the failure count
// should open a popup with the error logs for that failure"). Reads GET /api/bots/{id}/failures once on open: the same
// week-long window and the same `ok` test the count came from, newest first, so the list is as long as the number
// clicked. Each row: when, model, origin, the task, then the call's own error text; a line logged before the ledger
// kept that text says so instead of showing nothing. Each row ends in a clear icon (owner, 2026-10-13: "each row
// should have a clear icon, so that users can remove the old failure logs that they are not concerned about"):
// POST dismiss flags the ledger line, the row leaves this list and the count at once (the usage summary cache is reset
// server-side, the page's next poll shows it); the row goes optimistically and comes back on a rejected POST, with the
// reason in the header line. Keys are the call's ts (the server's own match key), so a removal never reshuffles the
// rows below it. Settings-sized shell (dialogs/shell.ts), scrolling body. Tailwind only: the dialog portals to <body>,
// outside .bots-page, so bots.css (the .chip pill, .lnk) does not reach it.
import { useEffect, useState } from "react";
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { api, type UsageFailure } from "../lib/api";
import { fmtWhen } from "../lib/format";
import { DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";

export function FailuresDialog({ id, name, onClose }: { id: string; name?: string; onClose: () => void }) {
  const [rows, setRows] = useState<UsageFailure[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api.failures(id).then((r) => { if (live) setRows(r.failures || []); }, (e: unknown) => { if (live) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [id]);
  const clear = (row: UsageFailure) => {
    setErr("");  // a new attempt starts clean: a stale refusal would outlive a later success and hide the empty-state line
    setRows((cur) => (cur || []).filter((r) => r.ts !== row.ts));
    api.dismissFailure(id, row.ts).catch((e: unknown) => {
      // Back where it was: newest first, like the server's order.
      setRows((cur) => [...(cur || []), row].sort((a, b) => b.ts - a.ts));
      setErr(e instanceof Error ? e.message : String(e));
    });
  };
  const n = rows?.length ?? 0;
  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>Failed calls{name ? ` · ${name}` : ""}</DialogTitle>
          <DialogDescription>{err ? (rows ? "A row could not be cleared." : "The usage log could not be read.") : rows ? `${n} failed model call${n === 1 ? "" : "s"} in the last 7 days, newest first.` : "Reading the usage log…"}</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-5" id="failures">
          {err ? <p className="mb-3 text-sm text-destructive">{err}</p> : null}
          {rows && !rows.length && !err ? <p className="m-0 text-sm text-muted-foreground">No failed calls this week.</p> : null}
          {(rows || []).map((r) => (
            <div key={r.ts} className="mb-3 rounded-xl border border-foreground/10 bg-foreground/[0.02] p-4">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span>{fmtWhen(r.ts)}</span>
                <i className="rounded-full bg-foreground/[0.06] px-1.5 py-px font-mono text-[10px] not-italic">{r.model}</i>
                <span>{r.origin}</span>
                {r.task ? <span className="min-w-0 truncate" title={r.task}>“{r.task}”</span> : null}
                {/* shadcn Button, not a raw <button>: no preflight, so a bare button in this portaled dialog wears the UA's
                    grey buttonface tile (owner, 2026-10-19). The [data-slot] base layer clears that; ghost/icon-xs is the
                    same clear-icon skin as the routine card's pencil and trash. */}
                <Button variant="ghost" size="icon-xs" className="ml-auto -mr-1.5 -my-1 text-muted-foreground hover:text-foreground"
                  title="Clear this failure" aria-label="Clear this failure" data-ts={r.ts} onClick={() => clear(r)}>
                  <XIcon className="size-3.5" />
                </Button>
              </div>
              {r.error
                ? <pre className="mt-2 mb-0 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-foreground/[0.04] p-3 font-mono text-xs leading-relaxed text-destructive">{r.error}</pre>
                : <p className="mt-2 mb-0 text-xs italic text-muted-foreground">No error text was recorded for this call (it was logged before the ledger kept the reason).</p>}
            </div>
          ))}
        </div>
        <DialogFooter className={FOOTER_CLASS}>
          <Button id="fclose" variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
