// The `.fusedbot` package's INSTALL PAGE (BUILD_SPEC §10): the same App-Store detail page as the `.fused` app
// file's (apps/marketplace FusedFilePage), built from the same @platform/ui/appstore blocks — hero with the one
// action, the Information strip, Instructions / Skills beside an Information list, the "Comes with" checklist and
// the "Before you install" card. Not imported from marketplace (an app may not import another app): the idiom is
// copied, the blocks are shared.
//
// Rendered IN PLACE by the shell (shell/App.tsx BotFileView) for /explorer/view|embed/<path>.fusedbot, so a
// double-click, a deeplink, the CLI, a click in a listing and a drop on the Bots page (which uploads the bytes first,
// then navigates here) all land on one page.
//
// Nothing in the file runs here. The page reads the read-only probe (GET /api/botfile/install), and its one action
// POSTs the install, which installs the ticked apps first and then creates a NEW bot (import never updates an
// existing one), its routines paused. On a clean success the page opens the bot; with a failed app or a server
// warning it stays open, so they are read, and the pill opens the bot.
//
// Retries never make a second bot. Every install POST carries the page mount's `request_id` (the server dedups on
// it and answers a repeat with the FIRST result, without comparing the body), so the first submit pins its
// selection (apps + memory) in state, the checkboxes lock from then on, and the error-phase Retry pill re-sends
// exactly that body. A row that failed after the bot exists has its own Retry: it POSTs only that folder with the
// bot's `bot_id` (the server adds the app to that bot) and a fresh `request_id`, then merges the returned row and
// its warnings.
//
// Permissions never travel (botfile._public_fields): whatever the file says, the new bot gets the creation defaults
// (approval ask, build_access scoped, terminal off), so the page shows those, never the packaged values.
//
// Styling: the blocks come from @platform/ui/appstore; the "Comes with" list borrows three of their classes from
// styles/marketplace.css (loaded by shell.css) — `as-info` (the aside section), `as-side-title` (its heading),
// `as-tag` (the source pill) and `as-link-btn` (a row's Retry) — so it reads as one of the aside's own sections. A rename there must follow here.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Bot as BotIcon, Brain, CalendarClock, Globe, ShieldCheck } from "lucide-react";
import {
  ActionPill,
  AppIcon,
  DetailBody,
  DetailHero,
  DetailLayout,
  DetailSection,
  DetailSkeleton,
  Delayed,
  InfoList,
  InfoStrip,
  StatusCallout,
  TrustCard,
  plainClick,
  type ActionPillProps,
  type BackLink,
  type InfoListRow,
  type InfoStripItem,
  type TrustItem,
} from "@platform/ui/appstore";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { basename, dirname } from "@platform/lib/format";
import { useDocumentTitle } from "@platform/lib/hooks";
import { navReach } from "@platform/lib/nav-history";
import { notify } from "@platform/lib/notifications";
import { IS_EMBED, IS_TOP_EMBED, navigateUrl, viewUrlForFsPath } from "@platform/lib/router";
import { botHref } from "../routes";
import {
  iconUrl,
  install,
  newRequestId,
  probe as getProbe,
  type BotFileApp,
  type BotFileAppResult,
  type BotFileInstallResult,
  type BotFileProbe,
  visibleWarnings,
} from "./botfile";

// -- Formatting -------------------------------------------------------------------------------------------------

function shortDate(iso: string | null | undefined): string | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function fmtStamp(iso: string | null | undefined): string | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// What an imported bot gets, in the Settings dialog's own words (dialogs/BotSettings.tsx): install never applies the
// file's approval / build_access / terminal (botfile._public_fields), so these are the creation defaults.
const DEFAULT_APPROVAL = "Ask me first";
const DEFAULT_BUILD_ACCESS = "Ask before risky steps";
const DEFAULT_TERMINAL = "Off";
const SOURCE: Record<string, string> = { bundled: "Bundled", marketplace: "Marketplace", starter: "Built in", github: "GitHub" };

/** A probe state as a short line: appfile's install states for a bundled app, installed/missing for a reference
 *  (a GitHub reference that is missing names the repo the install clones). */
function stateText(a: BotFileApp): string {
  switch (a.state) {
    case "missing":
      if (a.source.kind === "github") return `From GitHub: ${a.source.repo || a.source.url || "?"}`;
      return "Not installed";
    case "new":
      return "Not installed";
    case "update":
      return "Update available";
    case "same":
      return "Already installed";
    case "installed":
      return "Installed";
    case "clash":
      return "Name taken";
    default:
      return a.state;
  }
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** A failed row's sentence, by where the app was to come from. */
const FAILED: Record<string, string> = {
  marketplace: "Could not fetch it from the Marketplace.",
  starter: "Built-in app could not be installed.",
  bundled: "Could not install the bundled app.",
  github: "Could not install it from GitHub.",
};

// -- Navigation -------------------------------------------------------------------------------------------------
// A top-level embed (the window a double-click opened) becomes the bot's page as a new document with its chrome;
// a framed embed hands the bot to its top window; a normal window navigates in place.

function openBot(id: string) {
  const href = botHref(id);
  if (IS_TOP_EMBED) location.assign(href);
  else if (IS_EMBED) {
    try {
      (window.top ?? window).location.assign(href);
    } catch {
      location.assign(href);
    }
  } else navigateUrl(href);
}

// -- The page ---------------------------------------------------------------------------------------------------

type Phase =
  | { kind: "idle" }
  | { kind: "installing" }
  // `rowWarnings`: the latest per-row Retry's warnings by folder, replaced on each Retry of that row (so a warning a
  // Retry fixed goes away); the page shows the first install's warnings plus these (botfile.visibleWarnings).
  // `firstHadSettings`: the first install left a row in `settings` (its `settings: …` warning goes once none is).
  | { kind: "done"; result: BotFileInstallResult; firstHadSettings: boolean; rowWarnings?: Record<string, string[]> }
  | { kind: "error"; message: string };

/** The selection the first submit sent: every Retry with the same `request_id` re-sends exactly this. */
type Sent = { apps: string[]; memory: boolean };

export function BotFilePage({ path }: { path: string }) {
  const [probe, setProbe] = useState<BotFileProbe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [memory, setMemory] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  /** Rows whose per-row Retry is in flight. */
  const [retrying, setRetrying] = useState<Set<string>>(new Set());
  /** Pinned on the first submit (state, so the locked checkboxes render); null until then. */
  const [sent, setSent] = useState<Sent | null>(null);
  // One id per page mount: a Retry of the whole install re-sends it, and the server answers the first install's
  // result instead of creating a second bot.
  const requestId = useRef(newRequestId());
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (openTimer.current) clearTimeout(openTimer.current);
    },
    [],
  );
  const fileName = basename(path);
  const name = probe?.name || fileName.replace(/\.fusedbot$/i, "");
  useDocumentTitle(name);

  useEffect(() => {
    let live = true;
    getProbe(path)
      .then((p) => {
        if (!live) return;
        setProbe(p);
        setError(null);
        // The probe's ticks are the defaults; memory is on when the file carries it — the person installing chose
        // to open a file that has it.
        setTicked(new Set(p.apps.filter((a) => a.ticked && a.tickable).map((a) => a.folder)));
        setMemory(p.memory);
      })
      .catch((e: Error) => {
        if (live) setError(e.message || "Couldn't read this bot package");
      });
    return () => {
      live = false;
    };
  }, [path]);

  const canBack = navReach().back && !IS_TOP_EMBED;
  const back: BackLink | undefined = canBack
    ? { label: "Back", href: viewUrlForFsPath(dirname(path)), onClick: plainClick(() => history.back()) }
    : undefined;

  if (!probe) {
    return (
      <DetailLayout back={back}>
        {error ? (
          <StatusCallout tone="warn" title={/no such file/i.test(error) ? "Bot package not found" : "Couldn't open this bot package"}>
            {error}
          </StatusCallout>
        ) : (
          <Delayed>
            <DetailSkeleton />
          </Delayed>
        )}
      </DetailLayout>
    );
  }

  const p = probe;
  const pub = p.public;
  const model = str(pub.model);
  const effort = str(pub.effort);
  const routines = Array.isArray(pub.routines) ? pub.routines.length : 0;
  const instructions = str(pub.instructions).trim();
  // From the first submit on the selection is the pinned one, in the error phase too (a Retry resends it).
  const busy = sent !== null || phase.kind === "installing" || phase.kind === "done";
  const results = phase.kind === "done" ? new Map(phase.result.apps.map((r) => [r.folder, r])) : null;
  const warnings =
    phase.kind === "done"
      ? visibleWarnings(phase.result.warnings ?? [], phase.firstHadSettings, Object.fromEntries(results ?? []), phase.rowWarnings ?? {})
      : [];

  const toggle = (folder: string, on: boolean) =>
    setTicked((s) => {
      const next = new Set(s);
      if (on) next.add(folder);
      else next.delete(folder);
      return next;
    });

  const run = async () => {
    // The first submit pins the body; a Retry re-sends it as is (the server answers a repeated request_id with the
    // first result, so a different body would be reported as installed without ever being installed).
    // The probe's order: apps install in the order the file lists them.
    const body: Sent = sent ?? {
      apps: p.apps.filter((a) => a.tickable && ticked.has(a.folder)).map((a) => a.folder),
      memory: p.memory && memory,
    };
    if (!sent) setSent(body);
    setPhase({ kind: "installing" });
    try {
      const r = await install(path, { apps: body.apps, memory: body.memory, request_id: requestId.current });
      setPhase({ kind: "done", result: r, firstHadSettings: r.apps.some((a) => a.state === "settings") });
      const failed = r.apps.filter((a) => !a.ok).length;
      const notInstalled = r.apps.filter((a) => !a.ok && a.state !== "settings").length;
      notify({
        title: notInstalled ? `${name} installed · ${notInstalled} app${notInstalled === 1 ? "" : "s"} did not install` : `${name} installed`,
        tone: "info",
      });
      // Every app landed and nothing to say: a beat for the pill's check, then the bot. Otherwise stay, so the
      // failed rows and the warnings are read; the pill opens the bot.
      if (!failed && !r.warnings?.length) openTimer.current = setTimeout(() => openBot(r.bot_id), 700);
    } catch (e) {
      setPhase({ kind: "error", message: (e as Error).message || "Install failed" });
    }
  };

  // One failed row again, into the bot the first install made: only that folder (one of the pinned selection's: a
  // failed row comes from the pinned install's results), never the memory (the bot has it already, or was installed
  // without it), a fresh request id (this is a new request, not a resend). Its warnings replace that row's previous
  // Retry's, so one a later Retry fixed goes away. The answer row is matched by the index folder we sent; a lone row
  // that came back under another spelling is still ours (we asked for exactly one) and keeps the index folder.
  const retryRow = async (folder: string) => {
    if (phase.kind !== "done" || !sent?.apps.includes(folder)) return;
    const botId = phase.result.bot_id;
    setRetrying((s) => new Set(s).add(folder));
    let row: BotFileAppResult;
    let more: string[] = [];
    try {
      const r = await install(path, { apps: [folder], memory: false, bot_id: botId, request_id: newRequestId() });
      const hit = r.apps.find((a) => a.folder === folder) ?? (r.apps.length === 1 ? r.apps[0] : undefined);
      row = hit ? { ...hit, folder } : { folder, ok: false, state: "missing", error: "The server did not answer for this app." };
      more = r.warnings ?? [];
    } catch (e) {
      row = { folder, ok: false, state: "error", error: (e as Error).message || "Install failed" };
    }
    setPhase((ph) => {
      if (ph.kind !== "done") return ph;
      const apps = ph.result.apps.some((a) => a.folder === folder) ? ph.result.apps.map((a) => (a.folder === folder ? row : a)) : [...ph.result.apps, row];
      return { ...ph, result: { ...ph.result, apps }, rowWarnings: { ...ph.rowWarnings, [folder]: more } };
    });
    setRetrying((s) => {
      const next = new Set(s);
      next.delete(folder);
      return next;
    });
  };

  // ---- primary action ----
  let pill: ActionPillProps;
  let note: ReactNode;
  if (phase.kind === "installing") {
    pill = { state: "progress", progress: null, label: "Installing" };
    note = sent?.apps.length ? "Installing its apps, then the bot…" : "Creating the bot…";
  } else if (phase.kind === "done") {
    const id = phase.result.bot_id;
    const failed = phase.result.apps.filter((a) => !a.ok && a.state !== "settings").length;
    const unlinked = phase.result.apps.filter((a) => !a.ok && a.state === "settings").length;
    pill = { state: "open", label: "Open bot", onClick: () => openBot(id) };
    note =
      failed || unlinked || warnings.length ? (
        <span className="is-warn">
          {[
            "Installed",
            failed ? `${failed} app${failed === 1 ? "" : "s"} did not install` : "",
            unlinked ? `${unlinked} app${unlinked === 1 ? "" : "s"} not added to the bot` : "",
            ...warnings,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      ) : (
        <b>Installed · opening…</b>
      );
  } else if (phase.kind === "error") {
    pill = { state: "retry", title: phase.message, onClick: () => void run() };
    note = <span className="is-error">{phase.message}</span>;
  } else {
    pill = { state: "get", emphasis: "filled", wide: true, label: "Install bot", onClick: () => void run() };
    note = "Installs as a new bot";
  }

  // ---- information strip ----
  const exported = shortDate(p.exported_at);
  const strip: InfoStripItem[] = [
    { label: "Skills", value: String(p.skills.length), caption: p.skills.length === 1 ? "Playbook" : "Playbooks" },
    { label: "Memory", value: p.memory ? "Yes" : "No", caption: p.memory ? "In this file" : "Not in this file" },
    { label: "Apps", value: String(p.apps.length), caption: "Comes with" },
    {
      label: "Exported",
      value: exported ?? "—",
      caption: "Export date",
      title: fmtStamp(p.exported_at) ? `Exported ${fmtStamp(p.exported_at)}` : undefined,
    },
  ];

  // ---- aside ----
  const rows: InfoListRow[] = [
    { label: "Model", value: model || "Default" },
    { label: "Effort", value: effort || "Default" },
    // Not the file's values: permissions are never imported (see the header).
    { label: "Approval", value: DEFAULT_APPROVAL },
    { label: "Build access", value: DEFAULT_BUILD_ACCESS },
    { label: "Terminal", value: DEFAULT_TERMINAL },
    { label: "Routines", value: routines ? `${routines}, installed paused` : "None" },
  ];

  // The apps the bot will use without asking: the backend trusts exactly the file's apps that install OK, so before
  // the install that is the ticked rows.
  const trusted = p.apps.filter((a) => a.tickable && ticked.has(a.folder)).map((a) => a.name || a.folder);
  const trust: TrustItem[] = [
    {
      key: "approval",
      icon: <ShieldCheck size={16} strokeWidth={2} />,
      title: "Asks before acting",
      detail: "Permissions are not imported; the new bot starts with the defaults.",
      tone: "ok",
    },
    {
      key: "trusted",
      icon: <BotIcon size={16} strokeWidth={2} />,
      title: trusted.length ? "May use without asking" : "Uses no apps without asking",
      detail: trusted.length ? trusted.join(", ") : undefined,
    },
    {
      key: "browser",
      icon: <Globe size={16} strokeWidth={2} />,
      title: "Gets a fresh browser with no logins",
      detail: "Sign in to the sites it needs from its own window.",
    },
    {
      key: "memory",
      icon: <Brain size={16} strokeWidth={2} />,
      title: p.memory ? (memory ? "Memory: included" : "Memory: left out") : "Memory: not in this file",
      detail: p.memory ? "What its author's bot remembered." : undefined,
    },
  ];
  if (routines) {
    trust.push({
      key: "routines",
      icon: <CalendarClock size={16} strokeWidth={2} />,
      title: `${routines} routine${routines === 1 ? "" : "s"}, paused`,
      detail: "None run until you turn them on.",
    });
  }

  const memoryToggle = p.memory ? (
    <label className="flex cursor-pointer items-center gap-2 text-[13px]">
      <Checkbox checked={memory} disabled={busy} onCheckedChange={(c) => setMemory(!!c)} />
      Include memory
    </label>
  ) : undefined;

  const comesWith =
    p.apps.length > 0 ? (
      <section className="as-info">
        <h3 className="as-side-title">Comes with</h3>
        <ul className="flex flex-col">
          {p.apps.map((a) => {
            const res = results?.get(a.folder);
            // A settings row's error repeats the label before its reason; show only the reason.
            const reason = res?.error && res.state === "settings" ? res.error.replace(/^installed, but the bot could not be updated:\s*/i, "") : res?.error;
            const on = a.tickable && ticked.has(a.folder);
            return (
              <li key={a.folder} className={"flex items-start gap-2.5 border-b border-border py-2 last:border-b-0" + (a.tickable ? "" : " opacity-60")}>
                <Checkbox
                  className="mt-0.5"
                  checked={on}
                  disabled={!a.tickable || busy}
                  onCheckedChange={(c) => toggle(a.folder, !!c)}
                  aria-label={`Install ${a.name || a.folder}`}
                />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium" title={a.folder}>
                      {a.name || a.folder}
                    </span>
                    <span className="as-tag shrink-0">{SOURCE[a.source.kind] ?? a.source.kind}</span>
                  </div>
                  <span className="truncate text-[12px] text-muted-foreground" title={a.folder}>
                    {a.folder} · {res ? (res.ok || res.state === "settings" ? "Installed" : "Not installed") : stateText(a)}
                  </span>
                  {!a.tickable && a.reason && !res && <span className="text-[12px] text-muted-foreground">{a.reason}</span>}
                  {res && !res.ok && (
                    <span className="flex items-start gap-2 text-[12px] text-destructive">
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span>
                          {res.state === "settings"
                            ? "Installed, but the bot could not be updated."
                            : (FAILED[a.source.kind] ?? "Could not install it.")}
                        </span>
                        {reason && (
                          <span className="truncate text-muted-foreground" title={reason}>
                            {reason}
                          </span>
                        )}
                      </span>
                      <button
                        type="button"
                        className="as-link-btn shrink-0"
                        disabled={retrying.has(a.folder)}
                        onClick={() => void retryRow(a.folder)}
                      >
                        {retrying.has(a.folder) ? "Retrying…" : "Retry"}
                      </button>
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    ) : null;

  const main = (
    <>
      <DetailSection title="Instructions">
        {instructions ? (
          <p className="whitespace-pre-wrap text-[14px] leading-relaxed">{instructions}</p>
        ) : (
          <p className="text-[14px] text-muted-foreground">This bot has no instructions.</p>
        )}
      </DetailSection>
      <DetailSection title="Skills">
        {p.skills.length ? (
          <ul className="flex flex-col">
            {p.skills.map((s) => (
              <li key={s.name} className="flex flex-col gap-0.5 border-b border-border py-2.5 last:border-b-0">
                <span className="text-[14px] font-medium">{s.title || s.name}</span>
                {s.trigger && <span className="text-[13px] text-muted-foreground">{s.trigger}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[14px] text-muted-foreground">This bot comes with no skills.</p>
        )}
      </DetailSection>
    </>
  );

  return (
    <DetailLayout back={back}>
      <DetailHero
        icon={<AppIcon src={iconUrl(path)} name={name} seed={name} size={112} raised />}
        name={name}
        subtitle={[model, effort].filter(Boolean).join(" · ") || "A Fused bot, packed into one file."}
        author={<span title={path}>{fileName}</span>}
        actions={<ActionPill size="lg" {...pill} />}
        actionsNote={note}
      />
      <InfoStrip items={strip} />
      <DetailBody
        main={main}
        aside={
          <>
            <InfoList rows={rows} />
            {comesWith}
            <TrustCard items={trust} footer={memoryToggle} />
          </>
        }
      />
    </DetailLayout>
  );
}

export default BotFilePage;
