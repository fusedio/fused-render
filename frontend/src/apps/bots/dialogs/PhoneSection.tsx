// Settings > Phone on Super Bot (docs §10), on shadcn/ui: the switch "Text Super Bot from your phone", then a
// four-step checklist until the link works (Full Disk Access · Messages signed in · your number · a test text),
// then the connected view (status line, change number, people it may text). The step is DERIVED from the switch,
// the shell's one Full Disk Access store (platform/lib/fda.ts, never a second probe), the bridge's state (own
// handles learned from chat.db, its error) and whether a text ever went out; only the switch, the handle and the
// contacts are written. Switch and number write at once (the bridge reads both from disk, and step 2 only happens
// after the switch is on there); the contacts go with the dialog's Save. "Send a test text" also saves first.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { CheckIcon } from "lucide-react";
import { openFdaSettings } from "@platform/lib/api";
import { fdaCopy, pokeFda, relaunchHref, useFda } from "@platform/lib/fda";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { api, type ImessageState } from "../lib/api";
import { imessageStatus } from "../lib/live";
import { act } from "../state/store";
import { Row, Rows } from "./SettingsRow";

export interface PhoneSectionProps {
  botId: string;
  handle: string; setHandle: (h: string) => void;
  enabled: boolean; setEnabled: (on: boolean) => void;
  contacts: string; setContacts: (c: string) => void;
}

const IMESSAGE_TOPIC = "bots.imessage";

/** The backend's norm_handle, so a typed "+1 (555) 123-4567" compares equal to the stored "+15551234567". */
export function normHandle(h: string): string {
  h = (h || "").trim();
  if (h.includes("@")) return h.toLowerCase();
  let d = h.replace(/[^\d+]/g, "");
  if (d && !d.startsWith("+")) d = "+" + (d.length > 10 ? d : "1" + d);
  return d;
}

type StepState = "ok" | "need" | "wait";

function Dot({ s, n }: { s: StepState; n: number }) {
  return (
    <span className={cn("mt-0.5 grid size-5 place-items-center rounded-full border text-[10px] font-medium",
      s === "ok" ? "border-primary bg-primary text-primary-foreground" : s === "need" ? "border-foreground text-foreground" : "border-input text-muted-foreground")}>
      {s === "ok" ? <CheckIcon className="size-3" /> : n}
    </span>
  );
}

function Step({ s, n, title, text, children, action }: { s: StepState; n: number; title: string; text: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <li className={cn("grid grid-cols-[20px_1fr_auto] items-start gap-3 py-3", s === "wait" && "opacity-50")}>
      <Dot s={s} n={n} />
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="text-sm font-medium">{title}</div>
        <p className="m-0 text-sm text-muted-foreground">{text}</p>
        {children}
      </div>
      <div className="self-center">{action}</div>
    </li>
  );
}

export function PhoneSection({ botId, handle, setHandle, enabled, setEnabled, contacts, setContacts }: PhoneSectionProps) {
  const fda = useFda();
  const [st, setSt] = useState<ImessageState | null>(null);
  const [other, setOther] = useState(false);          // "Another number…" input open
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<{ ok: boolean; text: string } | null>(null);
  const [opened, setOpened] = useState(false);        // System Settings was opened from here

  // The bridge's state, fresh while the section shows: topic `bots.imessage` (the body of `GET /api/bots/imessage`),
  // which the server re-reads every 3 s while subscribed — the switch turning on makes the bridge read chat.db and
  // learn this Mac's own handles a tick or two later. A refusal leaves the last state shown (the read was silent).
  useEffect(() => subscribeTopic<ImessageState>(IMESSAGE_TOPIC, null, (snap) => { if (snap) setSt(snap); }), []);

  const own = st?.own_handles || [];
  const nh = normHandle(handle);
  const fdaOk = fda === null ? null : !!fda?.granted;   // null: not offered (dev server, not macOS) → the bridge's error is the only word
  const fdaPending = !!fda?.pending_relaunch;
  const bridgeNoFda = /full disk access/i.test(st?.error || "");
  const signedIn = own.length > 0;
  const saved = !!st && st.enabled === enabled && (st.handle || "") === nh;
  const sentOnce = !!st?.last_out || !!tested?.ok;
  const connected = enabled && !!nh && saved && !!st?.running && sentOnce;

  const openSettings = () => { setOpened(true); openFdaSettings().catch(() => {}).finally(pokeFda); };
  // One write at a time, and every write sends what the user wants NOW (`want`), not what the click that queued it
  // saw: two quick toggles collapse into the last state instead of racing. An empty or half-typed number is left out
  // of the body: the stored one stays (off is meant to remember it).
  const want = useRef({ on: enabled, h: handle });
  const chain = useRef<Promise<void>>(Promise.resolve());
  const persist = (on: boolean, h: string): Promise<void> => {
    want.current = { on, h };
    const run = async () => {
      const { on: o, h: hh } = want.current, w = normHandle(hh);
      await act(() => api.settings(botId, { imessage_enabled: o, ...(w ? { imessage_handle: w } : {}) }));
      // The bridge reads both from disk: ask the stream for its state now rather than at the next tick.
      resyncTopic(IMESSAGE_TOPIC);
    };
    chain.current = chain.current.then(run, run);
    return chain.current;
  };
  const flip = (on: boolean) => { setEnabled(on); void persist(on, handle); };
  const choose = (h: string) => { setHandle(h); setOther(false); void persist(enabled, h); };
  const sendTest = async () => {
    setTesting(true); setTested(null);
    try {
      if (!saved) await persist(enabled, handle);
      const r = await act(() => api.imessageTest(), true);
      if (r?.ok) setTested({ ok: true, text: `Sent to ${r.handle}. Check your phone.` });
      else setTested({ ok: false, text: "The text did not go out." });
    } catch (e) {
      setTested({ ok: false, text: (e as Error).message });
    } finally {
      setTesting(false);
    }
  };

  const step1: StepState = fdaOk === true ? "ok" : fdaOk === null ? (bridgeNoFda ? "need" : "ok") : "need";
  const step2: StepState = step1 !== "ok" ? "wait" : signedIn ? "ok" : "need";
  const step3: StepState = step2 !== "ok" ? "wait" : nh ? "ok" : "need";
  const step4: StepState = step3 !== "ok" ? "wait" : sentOnce ? "ok" : "need";
  const typing = other || (!!nh && !own.includes(nh));
  const identity = own.includes(nh)
    ? "Texting yourself: Super Bot's replies start with “@Super Bot” so you can tell them from your own notes."
    : "A separate Apple ID signed into Messages here: replies come as plain texts.";

  return (
    <div className="flex flex-col">
      <Rows className="pb-4">
        <Row title="Text Super Bot from your phone" text="Texts from your number become tasks. Replies come back as texts. Approvals stay on this Mac." htmlFor="bmphone">
          <Switch id="bmphone" checked={enabled} onCheckedChange={(c) => flip(!!c)} />
        </Row>
      </Rows>

      {!enabled ? (
        <p className="m-0 border-t border-foreground/10 pt-4 text-[13px] text-muted-foreground">{nh ? `Off. Your number (${nh}) is remembered; turn the switch on to use it again.` : "Off. Turn it on to set your number up."}</p>
      ) : connected ? (
        <Rows className="border-t border-foreground/10 pt-4">
          <Row title="Status" text={imessageStatus(nh, st)}>
            <Button type="button" variant="outline" size="sm" disabled={testing} onClick={() => { void sendTest(); }}>{testing ? "Sending…" : "Send a test text"}</Button>
          </Row>
          {tested ? <p className={cn("m-0 pb-3 text-[13px]", tested.ok ? "text-muted-foreground" : "text-destructive")}>{tested.text}</p> : null}
          <Row title="Your number" text={identity} htmlFor="bmimsg">
            <Input id="bmimsg" placeholder="+1 555 123 4567 or an Apple ID" autoComplete="off" value={handle} onChange={(e) => setHandle(e.target.value)}
              onBlur={() => { if (nh && !saved) void persist(enabled, handle); }} />
          </Row>
          <Row title="People Super Bot may text for you" htmlFor="bmimsgto" stack
            text="A name and a number or Apple ID per line. Every text goes through the approval card unless Permissions says never ask. Your own number is always allowed.">
            <Textarea id="bmimsgto" rows={2} placeholder={"Ali +1 555 123 4567\nMom mom@icloud.com"} value={contacts} onChange={(e) => setContacts(e.target.value)} />
          </Row>
        </Rows>
      ) : (
        <ol className="m-0 list-none divide-y divide-foreground/10 border-t border-foreground/10 p-0 [&>li:first-child]:pt-4">
          <Step s={step1} n={1} title="Full Disk Access"
            text={step1 === "ok" ? "Granted. It lets Super Bot read Messages."
              : fdaPending ? fdaCopy().pending
              : opened ? fdaCopy().waiting
              : "Lets Super Bot read Messages. Granted once in System Settings; survives upgrades."}
            action={step1 === "ok" ? null : fdaPending
              ? <Button size="sm" variant="outline" render={<a href={relaunchHref()} />}>{fdaCopy().relaunch}</Button>
              : <Button size="sm" variant="outline" onClick={openSettings}>{opened ? fdaCopy().reopen : fdaCopy().open}</Button>} />
          <Step s={step2} n={2} title="Messages is signed in"
            text={step2 === "ok" ? `Found ${own.join(", ")} on this Mac.`
              : step2 === "wait" ? "Checked after step 1."
              : st?.error && !bridgeNoFda ? st.error
              : "Open Messages, sign in with your Apple ID, and send any text so this Mac learns its number. This updates on its own."} />
          <Step s={step3} n={3} title="Your number" text={step3 === "wait" ? "Which number you will text from." : "Which number will you text from?"}>
            {step3 !== "wait" ? (
              <div className="flex flex-wrap gap-1.5">
                {own.map((h) => (
                  <Button key={h} type="button" size="sm" variant={nh === h ? "secondary" : "outline"} aria-pressed={nh === h} onClick={() => choose(h)}>
                    {h} · my own number
                  </Button>
                ))}
                <Button type="button" size="sm" variant={typing ? "secondary" : "outline"} aria-pressed={typing} onClick={() => setOther(true)}>Another…</Button>
              </div>
            ) : null}
            {step3 !== "wait" && typing ? (
              <Input id="bmimsg" placeholder="+1 555 123 4567 or an Apple ID" autoComplete="off" value={handle} onChange={(e) => setHandle(e.target.value)}
                onBlur={() => { if (nh && !saved) void persist(enabled, handle); }} />
            ) : null}
            {step3 === "ok" ? <p className="m-0 text-sm text-muted-foreground">{identity}</p> : null}
          </Step>
          <Step s={step4} n={4} title="Send a test text"
            text={tested ? tested.text : "Super Bot texts you “Connected”. macOS asks once to let this app control Messages."}
            action={step4 === "need" ? <Button size="sm" disabled={testing} onClick={() => { void sendTest(); }}>{testing ? "Sending…" : "Send"}</Button> : null} />
        </ol>
      )}
    </div>
  );
}
