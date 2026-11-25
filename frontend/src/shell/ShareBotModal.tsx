// SHARE WITH BOT — the explorer's "Share with bot…" row as a share sheet: the
// file as the heading, a strip of bot faces to pick the recipient from (the
// macOS / iOS share sheet's row of people), an optional message, Send.
//
// WHAT SEND DOES is what the bot composer does with an attached file
// (apps/bots Composer `send`): the bytes go to the bot's FILES folder
// (`/attach`), then one message names them — the reader's words, then the
// composer's own "Attached …" line, so the bot reads a shared file exactly as
// it reads a dropped one. A bot can read no path on this Mac outside the
// folders it was granted, so a path in a message would not do.
//
// IT LIVES IN SHELL, not beside ShareFileModal in platform/ui: it draws bot
// faces and sends through the bots client, and platform may not import
// apps/bots. The open-request store is platform's (lib/share-bot.ts), so the
// explorer's menus reach it without importing either.
//
// LOADED ON FIRST USE (ShareBotHost.tsx): the composer's module and the bots
// client stay out of the shell's first chunk.
//
// FED WITHOUT THE BOTS PAGE (BotPickList's rule): the store's quiet feed for
// as long as the sheet is open.
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Folder, Loader2, XIcon } from "lucide-react";
import { api, type Bot } from "@apps/bots/lib/api";
import { ATTACH_MAX } from "@apps/bots/components/Composer";
import { Face } from "@apps/bots/components/Face";
import { fmtBytes } from "@apps/bots/lib/format";
import { botHref } from "@apps/bots/routes";
import { feedBotQuietly, useBots } from "@apps/bots/state/store";
import { rawUrl } from "@platform/lib/api";
import { basename, dirname } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { navigateUrl } from "@platform/lib/router";
import type { BotShareFile } from "@platform/lib/share-bot";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { iconForEntry } from "@platform/ui/FileIcons";
import { homeBots } from "./home/bots";

const tooLarge = (name: string, size: number) =>
  `${name} is ${fmtBytes(size)}; a bot takes files up to ${fmtBytes(ATTACH_MAX)}.`;

const b64 = (blob: Blob) =>
  new Promise<string>((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",")[1] || "");
    r.onerror = () => no(r.error);
    r.readAsDataURL(blob);
  });

/** The composer's own line for attached files (Composer `send`, routes.py `save_files`), so a shared file reads to
 *  the bot as a dropped one does. */
export function shareBotMessage(message: string, stored: string): string {
  const text = message.trim();
  return (text ? text + "\n\n" : "") + `Attached (in your FILES folder, use \`upload\` when a page needs them): ${stored}`;
}

/** The bots a file can go to, in the Bots list's order: no hidden bot, and no expert (a task session, not a bot of
 *  the bots backend — it has no FILES folder). */
export function shareBotTargets(bots: Bot[]): Bot[] {
  return homeBots(bots.filter((b) => b.kind !== "expert"));
}

export default function ShareBotModal({ file, onClose }: { file: BotShareFile; onClose: () => void }) {
  useEffect(() => feedBotQuietly(), []);
  const S = useBots();
  const bots = useMemo(() => shareBotTargets(S.bots), [S.bots]);
  const [picked, setPicked] = useState<string | null>(null);
  // The first bot is the default once the list lands; a pick that left the list (deleted, hidden) falls back to it.
  const sel = bots.find((b) => b.id === picked) ?? bots[0] ?? null;
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const oversize = file.size != null && file.size > ATTACH_MAX;
  const [err, setErr] = useState<string | null>(oversize ? tooLarge(file.name, file.size!) : null);
  // The name each bot stored the file under: a Send that failed after the upload retries the message alone, so
  // the bot's FILES folder never gets the file twice.
  const stored = useRef(new Map<string, string>());
  const dir = dirname(file.path);

  const send = async () => {
    if (!sel || sending || oversize) return;
    setSending(true);
    setErr(null);
    try {
      let name = stored.current.get(sel.id);
      if (!name) {
        const res = await fetch(rawUrl(file.path));
        if (!res.ok) throw new Error(`Could not read ${file.name} (HTTP ${res.status}).`);
        const blob = await res.blob();
        if (blob.size > ATTACH_MAX) throw new Error(tooLarge(file.name, blob.size));
        name = (await api.attach(sel.id, file.name, await b64(blob))).name;
        stored.current.set(sel.id, name);
      }
      await api.send(sel.id, shareBotMessage(message, name));
      const id = sel.id;
      notify({
        title: `Sent ${file.name} to ${sel.name || sel.id}`,
        tone: "info",
        action: { label: "Open chat", onClick: () => navigateUrl(botHref(id)) },
      });
      onClose();
    } catch (e) {
      setErr(String((e as { message?: unknown })?.message || e));
      setSending(false);
    }
  };

  let strip;
  if (!S.polled) {
    strip = <p className="m-0 py-3 text-[13px] leading-5 text-muted-foreground">{S.banner.show ? S.banner.text : "Loading bots…"}</p>;
  } else if (!bots.length) {
    strip = (
      <p className="m-0 py-3 text-[13px] leading-5 text-muted-foreground">
        No bots yet.{" "}
        <a
          href="/bots"
          className="text-foreground underline underline-offset-2"
          onClick={(e) => {
            e.preventDefault();
            onClose();
            navigateUrl("/bots");
          }}
        >
          Add one on the Bots page
        </a>
        .
      </p>
    );
  } else {
    strip = (
      // `-mx-4 px-4`: the strip scrolls edge to edge under the sheet's padding, as a share sheet's row does.
      <div className="-mx-4 flex gap-1 overflow-x-auto px-4 pt-1 pb-2" role="radiogroup" aria-label="Send to">
        {bots.map((b) => {
          const on = b.id === sel?.id;
          return (
            <button
              key={b.id}
              type="button"
              role="radio"
              aria-checked={on}
              title={b.name || b.id}
              disabled={sending}
              onClick={() => setPicked(b.id)}
              className={cn(
                "flex w-[68px] shrink-0 cursor-pointer flex-col items-center gap-1.5 rounded-xl border-0 bg-transparent px-1 pt-2 pb-1.5 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50",
                on && "bg-muted",
              )}
            >
              <span className="relative block size-11">
                <span className={cn("block size-11 rounded-full [&_.eye]:fill-[#0f172a] [&_svg]:block [&_svg]:size-full", on && "ring-2 ring-primary ring-offset-2 ring-offset-background")}>
                  <Face b={b} />
                </span>
                {on && (
                  <span className="absolute -right-1 -bottom-1 flex size-4 items-center justify-center rounded-full bg-primary text-primary-foreground">
                    <Check className="size-3" strokeWidth={3} aria-hidden />
                  </span>
                )}
              </span>
              <span className={cn("w-full truncate text-center text-[11.5px] leading-4", on ? "font-medium text-foreground" : "text-muted-foreground")}>
                {b.name || b.id}
              </span>
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !sending) onClose();
      }}
    >
      <DialogContent
        className="max-w-[min(420px,calc(100%-2rem))] gap-0 overflow-hidden p-0 sm:max-w-[min(420px,calc(100%-2rem))]"
        showCloseButton={false}
      >
        {/* The file as the heading, ShareFileModal's arrangement (its comments carry the `min-w-0` / `m-0` reasons). */}
        <DialogHeader className="min-w-0 gap-0 px-4 pt-4 pb-3">
          <div className="flex min-w-0 items-start gap-3">
            <span className="mt-px flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted">
              {iconForEntry(file.name, false)}
            </span>
            <div className="flex min-w-0 flex-1 flex-col">
              <DialogTitle className="m-0 min-w-0 truncate text-[15px] leading-6 font-semibold">
                <span className="sr-only">Share with bot: </span>
                {file.name}
              </DialogTitle>
              <DialogDescription
                className="m-0 mt-0.5 flex min-w-0 items-center gap-1 text-left text-[12px] leading-4 text-muted-foreground"
                title={dir}
              >
                <Folder className="size-3 shrink-0" aria-hidden />
                <span className="min-w-0 truncate">{basename(dir) || dir}</span>
              </DialogDescription>
            </div>
            <DialogClose
              render={<Button variant="ghost" size="icon-sm" className="-mt-1 -mr-1.5 bg-transparent text-muted-foreground hover:text-foreground" />}
              disabled={sending}
            >
              <XIcon />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-2 border-t border-border px-4 pt-3 pb-4">
          <p className="m-0 text-[12px] leading-4 font-medium text-muted-foreground">Send to</p>
          {strip}
          <Textarea
            className="max-h-40 min-h-[72px] resize-none text-[13px] md:text-[13px]"
            placeholder={sel ? `Message to ${sel.name || sel.id} (optional)` : "Message (optional)"}
            aria-label="Message"
            value={message}
            disabled={sending || !sel}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              // ⌘/Ctrl+Enter sends; a bare Enter is a new line in a message box this size.
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {err && (
            <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
              {err}
            </p>
          )}
          <div className="mt-1 flex items-center justify-end gap-2">
            <Button variant="ghost" size="sm" disabled={sending} onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" disabled={!sel || sending || oversize} onClick={() => void send()}>
              {sending && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {sending ? "Sending…" : "Send"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
