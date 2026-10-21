// The chat's rows, one per event (lib.ts groupRows): the user's lines on the
// right; the bot's answers, questions and approval cards on the left; its
// steps (thought/action) folded into one quiet block per run; harness lines
// (note, system, delivery, hand-off, build) as muted one-liners, never bubbles.
// Rows are keyed by seq and the list only appends, so a poll never shifts what
// is on screen.
import { memo, useMemo, useState, type ReactNode } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CornerUpLeftIcon,
  ShieldAlertIcon,
  SquareIcon,
  SquareCheckIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import {
  answerAfter,
  approvalVerdict,
  clock,
  dayBreak,
  dayLabel,
  groupRows,
  markdownLite,
  multiAnswer,
  multiPicked,
  oneLine,
  optionKey,
  severityLabel,
  type Item,
} from "./lib";
import type { BotDetail, BotEvent } from "./types";

export interface ThreadHandlers {
  /** Quote this event in the composer. */
  onReply: (e: BotEvent) => void;
  /** Approve / Deny the live approval card. */
  onVerdict: (seq: number, yes: boolean) => void;
  /** Answer the live question (an option, or the multi-select's list). */
  onAnswer: (seq: number, text: string) => void;
  onImage: (src: string, alt: string) => void;
}

const CHANNEL: Record<string, string> = { imessage: "iMessage", handoff: "Super Bot", routine: "a routine", phone: "the phone", botsend: "botsend" };
const chan = (k: string) => CHANNEL[k] || k;

export function Thread({
  events,
  bot,
  held,
  newSeq,
  now,
  ...h
}: ThreadHandlers & {
  events: BotEvent[];
  bot: BotDetail;
  /** Cards answered here whose answer the poll has not brought back yet. */
  held: ReadonlySet<number>;
  /** The first event this phone had not shown when the chat opened. */
  newSeq: number | null;
  now: number;
}) {
  const items = useMemo(() => groupRows(events), [events]);
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (key: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  const lastKey = items.length ? items[items.length - 1].key : "";
  const working = bot.status === "running";
  const out: ReactNode[] = [];
  let prev: BotEvent | undefined;
  for (const it of items) {
    const first = it.kind === "steps" ? it.events[0] : it.e;
    if (dayBreak(prev, first)) {
      out.push(
        <div key={`d${first.seq}`} className="lan-mono py-3 text-center text-[11px] tracking-[0.08em] text-[var(--fg-muted)] uppercase">
          {dayLabel(first.ts, now)}
        </div>,
      );
    }
    if (newSeq != null && (it.kind === "steps" ? it.events.some((e) => e.seq === newSeq) : it.e.seq === newSeq)) {
      out.push(
        <div key={`n${newSeq}`} className="flex items-center gap-2 py-2 text-[11px] font-semibold tracking-[0.08em] text-[var(--accent)] uppercase">
          <span className="h-px flex-1 bg-[var(--accent)] opacity-40" />
          New
          <span className="h-px flex-1 bg-[var(--accent)] opacity-40" />
        </div>,
      );
    }
    prev = it.kind === "steps" ? it.events[it.events.length - 1] : it.e;
    if (it.kind === "steps") {
      out.push(
        <Steps
          key={it.key}
          item={it}
          open={open.has(it.key)}
          onToggle={() => toggle(it.key)}
          live={working && it.key === lastKey}
          onImage={h.onImage}
        />,
      );
    } else {
      const card = it.e.role === "approval" || it.e.role === "question";
      out.push(
        <Row
          key={it.key}
          item={it}
          answer={card ? answerAfter(events, it.e.seq) : null}
          live={card && bot.status === "waiting" && bot.waiting_seq === it.e.seq && !held.has(it.e.seq)}
          held={held.has(it.e.seq)}
          {...h}
        />,
      );
    }
  }
  return <div className="flex flex-col gap-1.5 px-3 pt-2 pb-3">{out}</div>;
}

// ---------------------------------------------------------------- steps ----
const Steps = memo(function Steps({
  item,
  open,
  onToggle,
  live,
  onImage,
}: {
  item: Extract<Item, { kind: "steps" }>;
  open: boolean;
  onToggle: () => void;
  live: boolean;
  onImage: ThreadHandlers["onImage"];
}) {
  const evs = item.events;
  const short = evs.length <= 2;
  const last = evs[evs.length - 1];
  if (short || open) {
    return (
      <div className="flex flex-col gap-1 py-1 pl-1">
        {!short && (
          <button type="button" onClick={onToggle} className="flex min-h-9 items-center gap-1.5 text-left text-[13px] text-[var(--fg-muted)]">
            <ChevronDownIcon className="size-3.5" />
            {evs.length} steps
          </button>
        )}
        {evs.map((e) => (
          <StepLine key={e.seq} e={e} onImage={onImage} />
        ))}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onToggle}
      className="lan-press -mx-1 flex min-h-11 items-center gap-2 rounded-xl px-2 py-1.5 text-left text-[13px] text-[var(--fg-muted)]"
    >
      <ChevronRightIcon className="size-3.5 shrink-0" />
      <span className="shrink-0 font-medium">{evs.length} steps</span>
      <span className="min-w-0 flex-1 truncate">· {oneLine(last.role === "action" ? last.result || last.text : last.text, 120)}</span>
      {live && <span className="lan-pulse size-1.5 shrink-0 rounded-full bg-[var(--success)]" aria-label="Working" />}
    </button>
  );
});

function StepLine({ e, onImage }: { e: BotEvent; onImage: ThreadHandlers["onImage"] }) {
  if (e.role === "thought") {
    return <div className="line-clamp-3 pl-5 text-[13px] leading-snug text-[var(--fg-muted)] italic">{e.text}</div>;
  }
  return (
    <div className="flex items-start gap-2 pl-5 text-[13px] leading-snug">
      <div className="min-w-0 flex-1">
        <div className="line-clamp-2 text-[var(--fg)] opacity-80">{e.text}</div>
        {e.result && <div className="line-clamp-2 text-[var(--fg-muted)]">{e.result}</div>}
      </div>
      {e.thumb && (
        <button
          type="button"
          onClick={() => onImage(e.thumb!, e.text)}
          className="shrink-0 overflow-hidden rounded-md border border-[var(--sh-border)]"
          aria-label="Open the step's screenshot"
        >
          <img src={e.thumb} alt="" loading="lazy" className="h-10 w-16 object-cover object-top" />
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- rows ----
type RowProps = ThreadHandlers & {
  item: Extract<Item, { kind: "event" }>;
  /** approval / question: the user line that answered it, if loaded. */
  answer: BotEvent | null;
  /** This card's buttons are live (it is `bot.waiting_seq`). */
  live: boolean;
  held: boolean;
};

function Muted({ children, center }: { children: ReactNode; center?: boolean }) {
  return (
    <div className={cn("px-1 py-1 text-[13px] leading-snug text-[var(--fg-muted)]", center && "text-center")}>{children}</div>
  );
}

function Foot({ e, onReply, right }: { e: BotEvent; onReply?: () => void; right?: boolean }) {
  return (
    <div className={cn("flex items-center gap-1 px-1 text-[12px] text-[var(--fg-muted)]", right && "justify-end")}>
      <span className="lan-mono">{clock(e.ts)}</span>
      {onReply && (
        <button
          type="button"
          onClick={onReply}
          className="-my-2 flex min-h-9 items-center gap-1 rounded-lg px-2 active:text-[var(--fg)]"
          aria-label="Reply to this message"
        >
          <CornerUpLeftIcon className="size-3.5" />
          Reply
        </button>
      )}
    </div>
  );
}

function Quote({ text }: { text: string }) {
  return (
    <div className="mb-1.5 line-clamp-2 border-l-2 border-current pl-2 text-[13px] leading-snug opacity-70">{text}</div>
  );
}

const BUBBLE = "max-w-[86%] rounded-[20px] px-3.5 py-2.5 text-[16px] leading-[1.4] break-words";
const LEFT = cn(BUBBLE, "self-start rounded-bl-md border border-[var(--sh-border)] bg-[var(--card)]");

const Row = memo(function Row({ item, answer, live, held, onReply, onVerdict, onAnswer }: RowProps) {
  const e = item.e;
  const deliveries = item.deliveries.length ? (
    <div className="flex flex-col px-1">
      {item.deliveries.map((d) => (
        <div key={d.seq} className={cn("text-[12px] text-[var(--fg-muted)]", d.error && "text-[var(--error)]")}>
          {d.error ? `Not sent to ${chan(d.channel || "")}: ${d.error}` : `→ ${chan(d.channel || "")}`}
        </div>
      ))}
    </div>
  ) : null;

  // Harness lines: muted, never a bubble.
  if (e.source === "clear") {
    return (
      <div className="flex items-center gap-2 py-2 text-[12px] text-[var(--fg-muted)]">
        <span className="h-px flex-1 bg-[var(--sh-border)]" />
        Context cleared
        <span className="h-px flex-1 bg-[var(--sh-border)]" />
      </div>
    );
  }
  if (e.build && e.source === "build") return <Muted>{oneLine(e.build.summary || e.text, 240)}</Muted>;
  if (e.handoff) return <Muted>{oneLine(e.summary || e.text, 240)}</Muted>;
  if (e.role === "system") return <Muted center>{oneLine(e.text, 240)}</Muted>;
  if (e.role === "note") return <Muted>{oneLine(e.text, 240)}</Muted>;
  if (e.role === "delivery") return <Muted>{`→ ${chan(e.channel || "")}: ${oneLine(e.text, 160)}`}</Muted>;

  if (e.role === "user") {
    const k = e.via?.kind;
    const via = k && k !== "web" && k !== "phone" ? (k === "handoff" || k === "routine" ? `from ${chan(k)}` : `via ${chan(k)}`) : "";
    return (
      <div className="flex flex-col items-end gap-1 pt-1">
        <div
          className={cn(
            BUBBLE,
            "rounded-br-md bg-[color-mix(in_srgb,var(--accent)_20%,var(--card))] whitespace-pre-wrap text-[var(--fg)]",
          )}
        >
          {e.reply?.text && <Quote text={oneLine(e.reply.text, 160)} />}
          {e.text}
        </div>
        <Foot e={e} right />
        {via && <div className="px-1 text-[12px] text-[var(--fg-muted)]">{via}</div>}
      </div>
    );
  }

  if (e.role === "approval") {
    const verdict = approvalVerdict(answer?.text);
    const risk = severityLabel(e.severity);
    return (
      <div className="flex flex-col gap-1 pt-1">
        <div
          className={cn(
            "max-w-[92%] self-start rounded-[20px] border p-3.5",
            live
              ? "border-[color-mix(in_srgb,var(--warning)_55%,transparent)] bg-[color-mix(in_srgb,var(--warning)_9%,var(--card))]"
              : "border-[var(--sh-border)] bg-[var(--card)]",
          )}
        >
          <div className={cn("mb-1.5 flex items-center gap-1.5 text-[13px] font-semibold", live ? "text-[var(--warning)]" : "text-[var(--fg-muted)]")}>
            <ShieldAlertIcon className="size-4" />
            {live ? "Needs your OK" : "Asked for approval"}
          </div>
          {risk ? (
            <div className="mb-1.5 flex flex-col items-start gap-1">
              <span
                className={cn(
                  "m-0 rounded-full border px-2.5 py-0.5 text-[12px] font-semibold",
                  e.severity === "high"
                    ? "border-[color-mix(in_srgb,var(--error)_45%,transparent)] bg-[color-mix(in_srgb,var(--error)_14%,transparent)] text-[var(--error)]"
                    : e.severity === "medium"
                      ? "border-[color-mix(in_srgb,var(--warning)_45%,transparent)] bg-[color-mix(in_srgb,var(--warning)_14%,transparent)] text-[var(--warning)]"
                      : "border-[color-mix(in_srgb,var(--success)_45%,transparent)] bg-[color-mix(in_srgb,var(--success)_14%,transparent)] text-[var(--success)]",
                )}
              >
                {risk}
              </span>
              {e.risk_summary ? <div className="m-0 text-[16px] leading-[1.4]">{e.risk_summary}</div> : null}
            </div>
          ) : null}
          <div className={cn("break-words whitespace-pre-wrap", risk && e.risk_summary ? "text-[13px] leading-[1.35] text-[var(--fg-muted)]" : "text-[16px] leading-[1.4]")}>{e.text}</div>
          {live ? (
            <div className="mt-3 flex gap-2">
              <Button variant="accent" className="h-11 flex-1 rounded-xl text-[15px]" onClick={() => onVerdict(e.seq, true)}>
                <CheckIcon />
                Approve
              </Button>
              <Button variant="outline" className="h-11 flex-1 rounded-xl text-[15px]" onClick={() => onVerdict(e.seq, false)}>
                <XIcon />
                Deny
              </Button>
            </div>
          ) : (
            <div className="mt-2 flex items-center gap-1.5 text-[13px] text-[var(--fg-muted)]">
              {held ? "Sending…" : verdict ? (
                <>
                  {verdict === "Approved" ? <CheckIcon className="size-3.5" /> : <XIcon className="size-3.5" />}
                  {verdict}
                </>
              ) : answer ? (
                `Answered: ${oneLine(answer.text, 80)}`
              ) : (
                "Not answered"
              )}
            </div>
          )}
        </div>
        <Foot e={e} />
      </div>
    );
  }

  if (e.role === "question") {
    const options = e.options || [];
    return (
      <div className="flex flex-col gap-1 pt-1">
        <div className={cn(LEFT, "max-w-[92%]")}>
          {e.reply?.text && <Quote text={oneLine(e.reply.text, 160)} />}
          <div className="lan-md" dangerouslySetInnerHTML={{ __html: markdownLite(e.text) }} />
          {options.length > 0 &&
            (e.multi ? (
              <MultiOptions options={options} live={live} answer={answer?.text ?? null} onSend={(t) => onAnswer(e.seq, t)} />
            ) : (
              <div className="mt-2.5 flex flex-wrap gap-2">
                {options.map((o, i) => {
                  const chosen = answer && optionKey(answer.text) === optionKey(o);
                  return (
                    <button
                      key={i}
                      type="button"
                      disabled={!live}
                      onClick={() => onAnswer(e.seq, o)}
                      className={cn(
                        "min-h-11 rounded-full border px-4 py-2 text-left text-[15px] leading-snug transition-colors",
                        live && "border-[var(--sh-border)] bg-[var(--bg)] active:bg-[var(--row-bg-hover)]",
                        !live && !chosen && "border-[var(--sh-border)] text-[var(--fg-muted)] opacity-60",
                        chosen && "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_18%,transparent)] font-medium",
                      )}
                    >
                      {chosen && <CheckIcon className="mr-1 inline size-3.5 align-[-2px]" />}
                      {o}
                    </button>
                  );
                })}
              </div>
            ))}
        </div>
        <Foot e={e} onReply={() => onReply(e)} />
        {deliveries}
      </div>
    );
  }

  if (e.role === "error") {
    return (
      <div className="flex flex-col gap-1 pt-1">
        <div
          className={cn(
            BUBBLE,
            "self-start rounded-bl-md border border-[color-mix(in_srgb,var(--error)_35%,transparent)] bg-[color-mix(in_srgb,var(--error)_10%,var(--card))]",
          )}
        >
          <div className="lan-md" dangerouslySetInnerHTML={{ __html: markdownLite(e.text) }} />
        </div>
        <Foot e={e} onReply={() => onReply(e)} />
        {deliveries}
      </div>
    );
  }

  // done (and anything unknown that carries text): the bot's answer.
  return (
    <div className="flex flex-col gap-1 pt-1">
      <div className={LEFT}>
        {e.reply?.text && <Quote text={oneLine(e.reply.text, 160)} />}
        <div className="lan-md" dangerouslySetInnerHTML={{ __html: markdownLite(e.text) }} />
      </div>
      <Foot e={e} onReply={() => onReply(e)} />
      {deliveries}
    </div>
  );
});

/** `multi`: tick any number, then Send sends "A, B" (or "None"). Settled, the
 *  options named in the answer stay ticked. */
function MultiOptions({
  options,
  live,
  answer,
  onSend,
}: {
  options: string[];
  live: boolean;
  answer: string | null;
  onSend: (text: string) => void;
}) {
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set());
  const answered = answer != null ? multiPicked(options, answer) : null;
  const on = (o: string) => (live ? picked.has(o) : !!answered?.has(optionKey(o)));
  return (
    <div className="mt-2.5 flex flex-col gap-1">
      {options.map((o, i) => (
        <button
          key={i}
          type="button"
          role="checkbox"
          aria-checked={on(o)}
          disabled={!live}
          onClick={() =>
            setPicked((p) => {
              const n = new Set(p);
              if (n.has(o)) n.delete(o);
              else n.add(o);
              return n;
            })
          }
          className={cn(
            "flex min-h-11 items-center gap-2.5 rounded-xl px-2 text-left text-[15px] leading-snug",
            live && "active:bg-[var(--row-bg-hover)]",
            !live && !on(o) && "opacity-55",
          )}
        >
          {on(o) ? (
            <SquareCheckIcon className="size-5 shrink-0 text-[var(--accent)]" />
          ) : (
            <SquareIcon className="size-5 shrink-0 text-[var(--fg-muted)]" />
          )}
          {o}
        </button>
      ))}
      {live && (
        <Button variant="accent" className="mt-1 h-11 rounded-xl text-[15px]" onClick={() => onSend(multiAnswer(options, picked))}>
          {picked.size ? `Send · ${picked.size}` : "None of these"}
        </Button>
      )}
    </div>
  );
}
