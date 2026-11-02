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
// it), so the error-phase Retry pill re-sends the same request. A row that failed after the bot exists has its own
// Retry: it POSTs only that folder with the bot's `bot_id` (the server adds the app to that bot) and a fresh
// `request_id`, then merges the returned row.
//
// Styling: the blocks come from @platform/ui/appstore; the "Comes with" list borrows three of their classes from
// styles/marketplace.css (loaded by shell.css) — `as-info` (the aside section), `as-side-title` (its heading),
// `as-tag` (the source pill) and `as-link-btn` (a row's Retry) — so it reads as one of the aside's own sections. A rename there must follow here.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Bot as BotIcon, Brain, CalendarClock, Globe, Hammer, ShieldAlert, ShieldCheck, TerminalSquare } from "lucide-react";
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

// The Settings dialog's own words for each value (dialogs/BotSettings.tsx), so the page and the bot agree.
const APPROVAL: Record<string, string> = { ask: "Ask me first", auto: "Never ask" };
const BUILD_ACCESS: Record<string, string> = { scoped: "Ask before risky steps", full: "Run unattended" };
const SOURCE: Record<string, string> = { bundled: "Bundled", marketplace: "Marketplace", starter: "Built in" };

/** A probe state as a short line: appfile's install states for a bundled app, installed/missing for a reference. */
function stateText(a: BotFileApp): string {
  switch (a.state) {
    case "new":
    case "missing":
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
  | { kind: "done"; result: BotFileInstallResult }
  | { kind: "error"; message: string };

export function BotFilePage({ path }: { path: string }) {
  const [probe, setProbe] = useState<BotFileProbe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [memory, setMemory] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  /** Rows whose per-row Retry is in flight. */
  const [retrying, setRetrying] = useState<Set<string>>(new Set());
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
  const approval = str(pub.approval) || "ask";
  const buildAccess = str(pub.build_access) || "scoped";
  const routines = Array.isArray(pub.routines) ? pub.routines.length : 0;
  const instructions = str(pub.instructions).trim();
  const busy = phase.kind === "installing" || phase.kind === "done";
  const results = phase.kind === "done" ? new Map(phase.result.apps.map((r) => [r.folder, r])) : null;

  const toggle = (folder: string, on: boolean) =>
    setTicked((s) => {
      const next = new Set(s);
      if (on) next.add(folder);
      else next.delete(folder);
      return next;
    });

  const run = async () => {
    setPhase({ kind: "installing" });
    try {
      // The probe's order: apps install in the order the file lists them.
      const apps = p.apps.filter((a) => a.tickable && ticked.has(a.folder)).map((a) => a.folder);
      const r = await install(path, { apps, memory: p.memory && memory, request_id: requestId.current });
      setPhase({ kind: "done", result: r });
      const failed = r.apps.filter((a) => !a.ok).length;
      notify({
        title: failed ? `${name} installed · ${failed} app${failed === 1 ? "" : "s"} did not install` : `${name} installed`,
        tone: "info",
      });
      // Every app landed and nothing to say: a beat for the pill's check, then the bot. Otherwise stay, so the
      // failed rows and the warnings are read; the pill opens the bot.
      if (!failed && !r.warnings?.length) openTimer.current = setTimeout(() => openBot(r.bot_id), 700);
    } catch (e) {
      setPhase({ kind: "error", message: (e as Error).message || "Install failed" });
    }
  };

  // One failed row again, into the bot the first install made: only that folder, never the memory (the bot has it
  // already, or was installed without it), a fresh request id (this is a new request, not a resend).
  const retryRow = async (folder: string) => {
    if (phase.kind !== "done") return;
    const botId = phase.result.bot_id;
    setRetrying((s) => new Set(s).add(folder));
    let row: BotFileAppResult;
    try {
      const r = await install(path, { apps: [folder], memory: false, bot_id: botId, request_id: newRequestId() });
      row = r.apps.find((a) => a.folder === folder) ?? { folder, ok: false, state: "missing", error: "The server did not answer for this app." };
    } catch (e) {
      row = { folder, ok: false, state: "error", error: (e as Error).message || "Install failed" };
    }
    setPhase((ph) =>
      ph.kind === "done"
        ? { ...ph, result: { ...ph.result, apps: ph.result.apps.some((a) => a.folder === folder) ? ph.result.apps.map((a) => (a.folder === folder ? row : a)) : [...ph.result.apps, row] } }
        : ph,
    );
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
    note = ticked.size ? "Installing its apps, then the bot…" : "Creating the bot…";
  } else if (phase.kind === "done") {
    const id = phase.result.bot_id;
    const failed = phase.result.apps.filter((a) => !a.ok).length;
    const warnings = phase.result.warnings ?? [];
    pill = { state: "open", label: "Open bot", onClick: () => openBot(id) };
    note =
      failed || warnings.length ? (
        <span className="is-warn">
          {["Installed", failed ? `${failed} app${failed === 1 ? "" : "s"} did not install` : "", ...warnings].filter(Boolean).join(" · ")}
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
    { label: "Approval", value: APPROVAL[approval] ?? approval },
    { label: "Build access", value: BUILD_ACCESS[buildAccess] ?? buildAccess },
    { label: "Terminal", value: pub.terminal === true ? "On" : "Off" },
    { label: "Routines", value: routines ? `${routines}, installed paused` : "None" },
  ];

  // The apps the bot will use without asking: the backend trusts exactly the file's apps that install OK, so before
  // the install that is the ticked rows.
  const trusted = p.apps.filter((a) => a.tickable && ticked.has(a.folder)).map((a) => a.name || a.folder);
  const trust: TrustItem[] = [
    approval === "auto"
      ? {
          key: "approval",
          icon: <ShieldAlert size={16} strokeWidth={2} />,
          title: "Acts without asking",
          detail: "Its approval is set to Never ask: it runs every step on its own.",
          tone: "caution",
        }
      : {
          key: "approval",
          icon: <ShieldCheck size={16} strokeWidth={2} />,
          title: "Asks before acting",
          detail: "You approve each step that does something.",
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
  if (buildAccess === "full") {
    trust.push({
      key: "build",
      icon: <Hammer size={16} strokeWidth={2} />,
      title: "Builds apps unattended",
      detail: "Its builds run without asking before risky steps.",
      tone: "caution",
    });
  }
  if (pub.terminal === true) {
    trust.push({
      key: "terminal",
      icon: <TerminalSquare size={16} strokeWidth={2} />,
      title: "Can use a terminal",
      tone: "caution",
    });
  }
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
                    {a.folder} · {res ? (res.ok ? "Installed" : "Not installed") : stateText(a)}
                  </span>
                  {!a.tickable && a.reason && !res && <span className="text-[12px] text-muted-foreground">{a.reason}</span>}
                  {res && !res.ok && (
                    <span className="flex items-start gap-2 text-[12px] text-destructive">
                      <span className="min-w-0 flex-1" title={res.error || undefined}>
                        {FAILED[a.source.kind] ?? "Could not install it."}
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
