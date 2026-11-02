// Export bot… (the bot menu, Settings' footer): writes the bot as one `.fusedbot` package into Downloads (bots/package.py,
// docs/bots.md §17). The dialog is the plan made visible: GET /package says what would travel, the user trims it, POST
// /package writes it. Top to bottom: who (face, name, the file name), what always goes (instructions, skills, the
// model/effort/approval posture), Memory (private, so default OFF), Apps (every manifest row ticked on open; a row the
// backend flagged with `problem` cannot travel and says why), a caution when something looks like a secret (export is
// never blocked on it), and the footer total. After a write the footer becomes the result row: where it went, Reveal, Done.
//
// Same shell as every bots dialog (dialogs/shell.ts) and non-modal like Settings / Browsers. Errors stay in the dialog
// (under the footer), never the page banner: the user is looking here, and the plan they trimmed must survive a retry.
import { useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon, TriangleAlertIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { AppIcon } from "@platform/ui/appstore/AppIcon";
import { Face } from "../components/Face";
import { api, appIconUrl, type AppRow, type Bot, type PackageAppRow, type PackagePlan, type PackageSourceKind } from "../lib/api";
import { EFFORTS, MODELS } from "../lib/botform";
import { errMsg } from "../state/store";
import { CARD_CLASS, DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";

const EXT = ".fusedbot";
const SOURCE_LABEL: Record<PackageSourceKind, string> = { bundled: "Bundled", marketplace: "Marketplace", starter: "Built in" };
const APPROVAL_LABEL: Record<string, string> = { ask: "Ask me first", auto: "Never ask" };

/** Human size with GB too: a package may carry up to 1 GiB of bundled apps. */
const size = (n: number): string =>
  n < 1024 ? `${n} B` : n < 1 << 20 ? `${Math.round(n / 1024)} KB` : n < 1 << 30 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${(n / (1 << 30)).toFixed(2)} GB`;
const label = (table: [string, string][], v: string): string => table.find(([k]) => k === v)?.[1] ?? v;
const str = (v: unknown): string => (typeof v === "string" ? v : "");
/** UTF-8 bytes, the unit the backend's caps are in. */
const utf8 = (s: string): number => new TextEncoder().encode(s).length;
/** The file name as the server will write it: only [A-Za-z0-9._ -], no leading dots, always `.fusedbot`; empty = the
 *  plan's default. Mirrors bots/package.py so the name shown under the input is the name you get. */
const normName = (raw: string, dflt: string): string => {
  let n = raw.replace(/[^A-Za-z0-9._ -]/g, "").replace(/\.{2,}/g, ".").trim().replace(/^\.+/, "");
  if (!n) return dflt;
  if (!n.toLowerCase().endsWith(EXT)) n += EXT;
  return n;
};
/** The result row shows the path's END (the file name is what you look for); the full path is the tooltip. */
const tail = (p: string, n = 64): string => (p.length > n ? "…" + p.slice(p.length - n + 1) : p);

/** An app added from the gallery ("Add app…"): not in the bot's manifest yet (the write bundles it and adds its row),
 *  so the plan never sized it. */
interface Extra { folder: string; name: string }

export function ExportBotDialog({ bot, onClose }: { bot: Bot; onClose: () => void }) {
  const [plan, setPlan] = useState<PackagePlan | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [fileName, setFileName] = useState("");
  const [memory, setMemory] = useState(false);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [extras, setExtras] = useState<Extra[]>([]);
  const [gallery, setGallery] = useState<{ root: string; apps: AppRow[] } | null>(null);
  const [galleryErr, setGalleryErr] = useState(false);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const live = useRef(true);

  // The gallery gives each row its icon (by absolute dir) and the "Add app…" list; failing it only costs those.
  const loadGallery = () => {
    setGalleryErr(false);
    api.apps().then((r) => { if (live.current) setGallery(r); }, () => { if (live.current) setGalleryErr(true); });
  };
  useEffect(() => {
    live.current = true;
    loadGallery();
    // Super Bot never travels (package.write refuses it): no plan to ask for.
    if (bot.kind === "super") return () => { live.current = false; };
    api.packagePlan(bot.id).then((p) => {
      if (!live.current) return;
      setPlan(p);
      setFileName(p.file_name);
      setTicked(new Set(p.apps.filter((a) => !a.problem).map((a) => a.folder)));
    }, (e) => { if (live.current) setLoadErr(errMsg(e)); });
    return () => { live.current = false; };
  }, [bot.id]);

  const byFolder = useMemo(() => new Map((gallery?.apps || []).map((a) => [a.folder, a])), [gallery]);
  const iconFor = (folder: string): string | null => {
    const a = byFolder.get(folder);
    return a?.icon ? appIconUrl(a.dir) : null;
  };
  const inPlan = useMemo(() => new Set([...(plan?.apps || []).map((a) => a.folder), ...extras.map((x) => x.folder)]), [plan, extras]);
  const addable = (gallery?.apps || []).filter((a) => !inPlan.has(a.folder));

  const pub = plan?.public || {};
  const instructions = str(pub.instructions);
  const firstLine = instructions.split("\n").find((l) => l.trim())?.trim() || "";
  // Text = bot.json + skill files (+ memory when ticked), in UTF-8 bytes; the backend's caps are checked at write.
  const textBytes = useMemo(() => (plan ? utf8(JSON.stringify(pub)) + (plan.skills_bytes ?? 0) : 0), [plan]);
  const appBytes = (plan?.apps || []).reduce((n, a) => n + (ticked.has(a.folder) && !a.problem && a.source.kind === "bundled" ? a.bytes : 0), 0);
  const total = textBytes + (memory ? plan?.memory_bytes || 0 : 0) + appBytes;
  const extraTicked = extras.some((x) => ticked.has(x.folder));

  const toggle = (folder: string, on: boolean) => setTicked((cur) => {
    const next = new Set(cur);
    if (on) next.add(folder); else next.delete(folder);
    return next;
  });
  const addApp = (a: AppRow) => {
    setExtras((cur) => [...cur, { folder: a.folder, name: a.name || a.folder }]);
    toggle(a.folder, true);
    setAdding(false);
  };

  const doExport = async () => {
    if (!plan || busy) return;
    setBusy(true); setErr("");
    const apps = [...plan.apps.filter((a) => !a.problem).map((a) => a.folder), ...extras.map((x) => x.folder)].filter((f) => ticked.has(f));
    const name = normName(fileName, plan.file_name);
    try {
      const r = await api.packageWrite(bot.id, { memory, apps, ...(name !== plan.file_name ? { file_name: name } : {}) });
      if (live.current) setResult(r.path);
    } catch (e) {
      if (live.current) setErr(errMsg(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };
  const reveal = async () => {
    if (!result) return;
    setErr("");
    try { await api.reveal(bot.id, result); } catch (e) { if (live.current) setErr(errMsg(e)); }
  };

  const isSuper = bot.kind === "super" || !!plan?.super;
  const savedName = plan ? normName(fileName, plan.file_name) : fileName;
  const skillTitles = (plan?.skills || []).map((s) => s.title || s.name).join(", ");

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <div className="flex min-w-0 items-center gap-3">
            <span className="size-10 shrink-0 [&>svg]:block [&>svg]:size-full"><Face b={bot} /></span>
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <DialogTitle>Export {plan?.name || bot.name}</DialogTitle>
              <DialogDescription>One file you can send to someone or keep. Opening it on a Mac makes a new bot.</DialogDescription>
            </div>
          </div>
          {plan && !isSuper ? (
            <div className="mt-3 flex flex-col gap-1">
              <div className="flex items-center gap-2.5">
                <label htmlFor="exfile" className="shrink-0 text-[13px] text-muted-foreground">File name</label>
                {/* Path separators and ".." never reach the server: stripped as typed. */}
                <Input id="exfile" value={fileName} disabled={busy || !!result} onChange={(e) => setFileName(e.target.value.replace(/[\\/]/g, "").replace(/\.{2,}/g, "."))}
                  onKeyDown={(e) => { if (e.key === "Enter") void doExport(); }} className="h-8" spellCheck={false} />
              </div>
              {savedName !== fileName ? <p className="m-0 pl-[4.6rem] text-xs text-muted-foreground">Saved as {savedName}</p> : null}
            </div>
          ) : null}
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-6 pb-5">
          {isSuper ? <p className="m-0 text-sm text-muted-foreground">Super Bot is one per Mac and cannot be exported.</p>
            : loadErr ? <p className="m-0 text-sm text-destructive">{loadErr}</p>
            : !plan ? <p className="m-0 text-sm text-muted-foreground">Loading…</p>
            : (
              <>
                <section className={CARD_CLASS} aria-labelledby="exalways">
                  <h3 id="exalways" className="m-0 text-sm font-medium">Always included</h3>
                  <dl className="m-0 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2 text-[13px]">
                    <dt className="text-muted-foreground">Instructions</dt>
                    <dd className="m-0 min-w-0">
                      {firstLine ? <><span className="block truncate" title={instructions}>{firstLine}</span>
                        <span className="text-muted-foreground">{instructions.length.toLocaleString()} characters</span></>
                        : <span className="text-muted-foreground">None</span>}
                    </dd>
                    <dt className="text-muted-foreground">Skills</dt>
                    <dd className="m-0 min-w-0">
                      {plan.skills.length ? <span className="block truncate" title={skillTitles}>{plan.skills.length} · {skillTitles}</span>
                        : <span className="text-muted-foreground">None</span>}
                      {plan.skills_skipped?.length ? (
                        <span className="block truncate text-xs text-muted-foreground" title={plan.skills_skipped.map((k) => `${k.name}: ${k.problem}`).join("\n")}>
                          Not included: {plan.skills_skipped.map((k) => `${k.name} (${k.problem})`).join(", ")}
                        </span>
                      ) : null}
                    </dd>
                    <dt className="text-muted-foreground">Model</dt>
                    <dd className="m-0">{label(MODELS, str(pub.model)) || "—"} · {label(EFFORTS, str(pub.effort)) || "—"}</dd>
                    <dt className="text-muted-foreground">Approval</dt>
                    <dd className="m-0">{APPROVAL_LABEL[str(pub.approval)] || str(pub.approval) || "—"}</dd>
                  </dl>
                  <p className="m-0 text-xs text-muted-foreground">Routines go along switched off. Logins, history and the Inbox stay on this Mac.</p>
                </section>

                <section className={CARD_CLASS} aria-labelledby="exmem">
                  <h3 id="exmem" className="m-0 text-sm font-medium">Memory</h3>
                  <label className={cn("flex items-center gap-2.5 text-sm", plan.memory_bytes ? "cursor-pointer" : "opacity-60")}>
                    <Checkbox checked={memory} disabled={!plan.memory_bytes || busy || !!result} onCheckedChange={(c) => setMemory(!!c)} />
                    <span>Include what it remembers</span>
                    <span className="ml-auto text-xs text-muted-foreground tabular-nums">{plan.memory_bytes ? size(plan.memory_bytes) : "Empty"}</span>
                  </label>
                  <p className="m-0 text-xs text-muted-foreground">Memory is private: notes about you and your work. Leave it off unless the file stays with you.</p>
                </section>

                <section className={CARD_CLASS} aria-labelledby="exapps">
                  <h3 id="exapps" className="m-0 text-sm font-medium">Apps</h3>
                  {plan.apps.length || extras.length ? (
                    <div className="-mx-1 flex flex-col">
                      {plan.apps.map((a) => <PlanAppRow key={a.folder} a={a} on={ticked.has(a.folder)} icon={iconFor(a.folder)}
                        disabled={busy || !!result} onToggle={(on) => toggle(a.folder, on)} />)}
                      {extras.map((x) => (
                        <label key={x.folder} className="flex cursor-pointer items-center gap-3 rounded-md px-1 py-1.5 text-sm hover:bg-foreground/[0.04]">
                          <Checkbox checked={ticked.has(x.folder)} disabled={busy || !!result} onCheckedChange={(c) => toggle(x.folder, !!c)} />
                          <AppIcon src={iconFor(x.folder)} name={x.name} seed={x.folder} size={28} />
                          <span className="flex min-w-0 flex-1 flex-col">
                            <span className="truncate">{x.name}</span>
                            <span className="truncate text-xs text-muted-foreground">{x.folder}</span>
                          </span>
                          <span className="text-xs text-muted-foreground">Added</span>
                          <span className="text-right text-xs text-muted-foreground">size unknown until export</span>
                        </label>
                      ))}
                    </div>
                  ) : <p className="m-0 text-[13px] text-muted-foreground">No apps yet.</p>}
                  {adding ? (
                    <div className="flex max-h-56 flex-col overflow-y-auto rounded-lg border border-foreground/10">
                      {galleryErr ? (
                        <p className="m-0 px-3 py-2 text-[13px] text-muted-foreground">Couldn't load apps.{" "}
                          <button type="button" onClick={loadGallery} className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-foreground underline underline-offset-2 outline-none [font:inherit] focus-visible:ring-3 focus-visible:ring-ring/50">Retry</button>
                        </p>
                      ) : !gallery ? <p className="m-0 px-3 py-2 text-[13px] text-muted-foreground">Loading…</p>
                        : !addable.length ? <p className="m-0 px-3 py-2 text-[13px] text-muted-foreground">Every app is already in the list.</p>
                        : addable.map((a) => (
                          <button key={a.folder} type="button" onClick={() => addApp(a)}
                            className="flex w-full cursor-pointer appearance-none items-center gap-3 border-0 bg-transparent px-3 py-1.5 text-left text-sm text-foreground outline-none hover:bg-foreground/[0.04] focus-visible:bg-foreground/[0.06]">
                            <AppIcon src={a.icon ? appIconUrl(a.dir) : null} name={a.name || a.folder} seed={a.folder} size={24} />
                            <span className="min-w-0 flex-1 truncate">{a.name || a.folder}</span>
                            {a.name && a.name !== a.folder ? <span className="truncate text-xs text-muted-foreground">{a.folder}</span> : null}
                          </button>
                        ))}
                    </div>
                  ) : null}
                  {result ? null : (
                    <div className="flex gap-2">
                      <Button variant="outline" size="sm" disabled={busy} onClick={() => setAdding((v) => !v)}>
                        {adding ? "Close list" : <><PlusIcon />Add app…</>}
                      </Button>
                    </div>
                  )}
                </section>

                {plan.text_problem ? (
                  <p className="m-0 text-sm text-destructive">{plan.text_problem}. Trim memory or skills before exporting.</p>
                ) : null}
                {plan.secrets.length ? (
                  <section className="flex flex-col gap-1.5 rounded-xl border border-amber-500/30 bg-amber-500/[0.06] p-4 text-[13px]" aria-labelledby="exsecrets">
                    <h3 id="exsecrets" className="m-0 flex items-center gap-2 text-sm font-medium"><TriangleAlertIcon className="size-4 text-amber-500" />This may contain a secret</h3>
                    <ul className="m-0 flex list-none flex-col gap-1 p-0">
                      {plan.secrets.map((s, i) => (
                        <li key={i} className="flex min-w-0 gap-2">
                          <span className="shrink-0 text-muted-foreground">{s.where}</span>
                          <code className="min-w-0 truncate">{s.snippet}</code>
                        </li>
                      ))}
                    </ul>
                    <p className="m-0 text-xs text-muted-foreground">Anyone with the file can read it. Remove it first if this is not meant to travel.</p>
                    <p className="m-0 text-xs text-muted-foreground">Scanned: {(plan.secrets_scope ?? SCAN_SCOPE).join(", ")}. Bundled app files are not scanned.</p>
                  </section>
                ) : (
                  <p className="m-0 px-1 text-xs text-muted-foreground">Scanned for secrets: {(plan.secrets_scope ?? SCAN_SCOPE).join(", ")}. Bundled app files are not scanned.</p>
                )}
              </>
            )}
        </div>

        <DialogFooter className={cn(FOOTER_CLASS, "flex-col gap-2 sm:flex-col sm:justify-start")}>
          {result ? (
            <div className="flex w-full items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-[13px]" title={result}>Saved to <span className="text-muted-foreground">{tail(result)}</span></span>
              <Button variant="outline" onClick={() => { void reveal(); }}>Reveal folder</Button>
              <Button onClick={onClose}>Done</Button>
            </div>
          ) : (
            <div className="flex w-full items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-[13px] text-muted-foreground tabular-nums">
                {plan && !isSuper ? `About ${size(total)}${extraTicked ? " plus added apps" : ""}` : ""}
              </span>
              <Button variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
              <Button disabled={!plan || isSuper || busy || !!plan.text_problem} onClick={() => { void doExport(); }}>{busy ? "Exporting…" : "Export"}</Button>
            </div>
          )}
          {err ? <p className="m-0 w-full text-[13px] text-destructive" role="alert">{err}</p> : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One manifest row: a `problem` row is muted, unticked and untickable, with the reason where the size would be. */
/** What package.plan() scans when an older backend omits `secrets_scope` (mirrors package.py). */
const SCAN_SCOPE = ["instructions", "memory", "skills", "readme", "routines"];

function PlanAppRow({ a, on, icon, disabled, onToggle }: { a: PackageAppRow; on: boolean; icon: string | null; disabled: boolean; onToggle: (on: boolean) => void }) {
  const bad = !!a.problem;
  return (
    <label className={cn("flex items-center gap-3 rounded-md px-1 py-1.5 text-sm", bad ? "opacity-60" : "cursor-pointer hover:bg-foreground/[0.04]")}>
      <Checkbox checked={on && !bad} disabled={bad || disabled} onCheckedChange={(c) => onToggle(!!c)} />
      <AppIcon src={bad ? null : icon} name={a.name || a.folder} seed={a.folder} size={28} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate">{a.name || a.folder}</span>
        <span className="truncate text-xs text-muted-foreground">{bad ? a.problem : a.folder}</span>
        {!bad && a.mints_app_id ? (
          <span className="truncate text-xs text-muted-foreground" title={`Exporting writes <meta name="fused-app-id"> into ${a.folder}'s entry page, the same stamp sharing the app makes.`}>
            Export stamps an app id into {a.folder}'s entry page (same as sharing the app)
          </span>
        ) : null}
      </span>
      <Badge variant="secondary">{SOURCE_LABEL[a.source.kind] ?? a.source.kind}</Badge>
      <span className="w-16 text-right text-xs text-muted-foreground tabular-nums">{a.source.kind === "bundled" && !bad ? size(a.bytes) : "—"}</span>
    </label>
  );
}
