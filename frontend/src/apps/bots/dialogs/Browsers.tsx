// Browsers (the bot list header's globe): every browser the bots run on, one card each. A browser is one set of
// logins; several bots may share it (Super Bot's included). Per card: its name (click to rename), the sites it is
// signed in to, the bots on it, and what belongs to the browser rather than a bot: Sign in… (pops it out as a real
// window, Dock brings it back), encrypt at rest, start from a Chrome profile, delete. Every op refetches the list.
//
// Same black shadcn Dialog as BotSettings, non-modal for the same reason: askConfirm renders as a sibling in the
// bots portal host, so a click in it reads as an outside press here; `busy` covers the time a confirm is up.
import { useEffect, useRef, useState } from "react";
import { PencilIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Switch } from "@platform/shadcn/ui/switch";
import { api, type Bot, type BrowserOpBody, type BrowserRow, type ChromeProfile } from "../lib/api";
import { browserOf } from "../lib/botform";
import { act, useBotsSelector } from "../state/store";
import { askConfirm } from "./ask";
import { DIALOG_CLASS, FOOTER_CLASS } from "./BotSettings";
import { Row, Rows } from "./SettingsRow";

const botsN = (n: number) => `${n} bot${n === 1 ? "" : "s"}`;

export function BrowsersDialog({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<BrowserRow[] | null>(null);
  const [profiles, setProfiles] = useState<ChromeProfile[]>([]);
  const bots = useBotsSelector((s) => s.bots);
  const busy = useRef(false);  // a confirm is up: outside presses are its
  const live = useRef(true);

  const reload = async () => {
    const [b, p] = await Promise.all([act(() => api.browsers()), act(() => api.profiles(), true)]);
    if (!live.current) return;
    if (b) setRows(b.browsers);
    if (p) setProfiles(p.profiles);
  };
  useEffect(() => {
    live.current = true;
    void reload();
    return () => { live.current = false; };
  }, []);

  /** One op, then the fresh list. */
  const run = async (id: string, body: BrowserOpBody) => { await act(() => api.browserOp(id, body)); await reload(); };
  /** askConfirm with the dialog held open while it is up. */
  const confirm = async (title: string, text: string, ok: string, danger: boolean) => {
    busy.current = true;
    try { return await askConfirm(title, text, ok, danger); } finally { busy.current = false; }
  };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open && !busy.current) onClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, "flex max-h-[min(760px,90vh)] flex-col sm:max-w-[680px]")}>
        <DialogHeader className="gap-1 px-6 pt-5 pb-4">
          <DialogTitle>Browsers</DialogTitle>
          <DialogDescription>The logins your bots use. Bots on one browser share its sign-ins: log in once, every bot on it stays in.</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-6 pb-5" id="browserlist">
          {rows === null ? <p className="m-0 text-sm text-muted-foreground">Loading…</p>
            : !rows.length ? <p className="m-0 text-sm text-muted-foreground">No browsers yet. Every bot gets one when it is made.</p>
            : rows.map((r) => <BrowserCard key={r.id} r={r} bots={bots} profiles={profiles} run={run} confirm={confirm} />)}
        </div>
        <DialogFooter className={FOOTER_CLASS}>
          <Button variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface CardProps {
  r: BrowserRow;
  bots: Bot[];
  profiles: ChromeProfile[];
  run: (id: string, body: BrowserOpBody) => Promise<void>;
  confirm: (title: string, text: string, ok: string, danger: boolean) => Promise<boolean>;
}

function BrowserCard({ r, bots, profiles, run, confirm }: CardProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(r.name);
  const [pending, setPending] = useState(false);
  const skipBlur = useRef(false);  // Escape (or Enter's own blur) already settled the edit
  // Popped out: any bot on this browser has its window up (top-level `visible` is in every poll row; `browser` too).
  const out = bots.some((b) => browserOf(b) === r.id && (b.visible || b.browser?.visible));
  const n = r.bots.length;
  const inputId = `brname-${r.id}`;

  const go = async (body: BrowserOpBody) => {
    setPending(true);
    try { await run(r.id, body); } finally { setPending(false); }
  };
  const startEdit = () => {
    setDraft(r.name); skipBlur.current = false; setEditing(true);
    window.setTimeout(() => { const el = document.getElementById(inputId) as HTMLInputElement | null; el?.focus(); el?.select(); }, 0);
  };
  const saveName = () => {
    if (skipBlur.current) return;
    skipBlur.current = true;
    setEditing(false);
    const v = draft.trim();
    if (v && v !== r.name) void go({ op: "rename", name: v });
  };
  const pickProfile = async (dir: string) => {
    if (!dir) return;
    const p = profiles.find((x) => x.dir === dir);
    if (!(await confirm("Start from a Chrome profile?", `Replace the logins of ${r.name} with your Chrome profile ${p?.name || dir}? Every bot on it is affected.`, "Replace", true))) return;
    await go({ op: "profile", profile: dir });
  };
  const remove = async () => {
    if (!(await confirm(`Delete browser "${r.name}"?`, `Its logins are removed. ${botsN(n)} ${n === 1 ? "gets" : "get"} a fresh, logged-out browser of ${n === 1 ? "its" : "their"} own.`, "Delete", true))) return;
    await go({ op: "delete" });
  };

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-white/10 bg-white/[0.02] p-4" data-browser={r.id} aria-busy={pending || undefined}>
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex min-w-0 items-center gap-2">
          {editing ? (
            <Input id={inputId} aria-label="Browser name" value={draft} className="h-8 max-w-72"
              onChange={(e) => setDraft(e.target.value)} onBlur={saveName}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); saveName(); }
                else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skipBlur.current = true; setDraft(r.name); setEditing(false); }
              }} />
          ) : (
            <>
              <button type="button" onClick={startEdit} title="Rename"
                className="min-w-0 cursor-text appearance-none truncate border-0 bg-transparent p-0 text-left text-[15px] font-medium text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                {r.name}
              </button>
              <Button variant="ghost" size="icon-xs" aria-label={`Rename ${r.name}`} title="Rename" onClick={startEdit}><PencilIcon /></Button>
            </>
          )}
          {r.running ? <span className="ml-auto text-xs text-muted-foreground">{out ? "Open as a window" : "Running"}</span> : null}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {r.sites.length ? r.sites.map((s) => <Badge key={s} variant="secondary">{s}</Badge>)
            : <span className="text-[13px] text-muted-foreground">Not signed in anywhere yet</span>}
        </div>
        <p className="m-0 text-[13px] text-muted-foreground">Used by: {n ? r.bots.map((b) => b.name).join(", ") : "no bot"}</p>
      </div>
      <Rows>
        <Row title="Sign in to a site" text="Opens the browser as a real window so you can sign in to a site. Close it or Hand back when done.">
          {out ? <Button variant="outline" size="sm" disabled={pending} onClick={() => { void go({ op: "dock" }); }}>Dock</Button>
            : <Button variant="outline" size="sm" disabled={pending || !n} onClick={() => { void go({ op: "signin" }); }}>Sign in…</Button>}
        </Row>
        <Row title="Encrypt at rest" text="While it is closed the profile is one AES-256 file; the key lives in your macOS Keychain." htmlFor={`brenc-${r.id}`}>
          <Switch id={`brenc-${r.id}`} checked={r.encrypt} disabled={pending} onCheckedChange={(c) => { void go({ op: "encrypt", on: !!c }); }} />
        </Row>
        <Row title="Start from a Chrome profile" text={`Copies that profile's logins into this browser, replacing what it has.${r.chrome_profile ? ` Now from ${r.chrome_profile}.` : ""}`} htmlFor={`brprof-${r.id}`}>
          <NativeSelect className="w-full" id={`brprof-${r.id}`} value="" disabled={pending || !profiles.length}
            onChange={(e) => { void pickProfile(e.target.value); }}>
            <NativeSelectOption value="">{profiles.length ? "Pick a profile…" : "No Chrome profiles found"}</NativeSelectOption>
            {profiles.map((p) => <NativeSelectOption key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</NativeSelectOption>)}
          </NativeSelect>
        </Row>
      </Rows>
      <div className="flex justify-end">
        <Button variant="destructive" size="sm" disabled={pending} onClick={() => { void remove(); }}>Delete</Button>
      </div>
    </section>
  );
}
