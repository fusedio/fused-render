// A SESSION THE PLAN'S USAGE LIMIT STOPPED, IN WORDS — and on the four surfaces
// that say them: the Tasks List row, the Board card, the Cards wall and the top
// of the chat itself.
//
// The lane and the ring are BLOCKED's, unchanged, because nothing is moving and
// nothing will move by itself. What these tests hold still is the one clause
// that separates this row from the runs beside it that actually broke: what
// stopped it, and when it starts again.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  isUsageLimited,
  resumesClock,
  usageLimitCaption,
  usageLimitStatusWord,
  USAGE_LIMIT_REASON,
} from "./usage-limit";

const HERE = new URL(".", import.meta.url).pathname;
const VIEWS = readFileSync(join(HERE, "../../shell/ScheduleTaskViews.tsx"), "utf8");
const CARDS = readFileSync(join(HERE, "../../shell/TaskCards.tsx"), "utf8");
const CARDS_CSS = readFileSync(join(HERE, "../../styles/task-cards.css"), "utf8");
const TOPBAR = readFileSync(join(HERE, "../../apps/claude/ui/Topbar.tsx"), "utf8");
const CHAT = readFileSync(join(HERE, "../../apps/claude/ClaudeChat.tsx"), "utf8");
const COMPOSER_CSS = readFileSync(
  join(HERE, "../../apps/claude/styles/composer.css"),
  "utf8",
);

/** 4:00 AM on the machine running this suite — built from local parts, so the
 *  assertion is about the WORDS and never about the zone. */
const at = (h: number, m = 0) =>
  Math.floor(new Date(2026, 8, 12, h, m, 0, 0).getTime() / 1000);

const limited = (over: Record<string, unknown> = {}) => ({
  status: "blocked",
  blocked_reason: USAGE_LIMIT_REASON,
  resumes_at: at(4),
  ...over,
});

describe("which rows are paused rather than broken", () => {
  it("needs BOTH halves — the lane and the reason", () => {
    expect(USAGE_LIMIT_REASON).toBe("usage_limit");
    expect(isUsageLimited(limited())).toBe(true);
    // `blocked` alone is every kind of not-moving, and a run that BROKE is the
    // common one.
    expect(isUsageLimited({ status: "blocked", blocked_reason: "failed" })).toBe(false);
    expect(isUsageLimited({ status: "blocked" })).toBe(false);
    // …and a reason left on a row that is running again is a leftover.
    expect(isUsageLimited({ status: "in_progress", blocked_reason: "usage_limit" })).toBe(false);
    expect(isUsageLimited(null)).toBe(false);
    expect(isUsageLimited(undefined)).toBe(false);
  });
});

describe("the clock the window reopens on", () => {
  it("is the reader's own, spelled `4:00 AM`", () => {
    expect(resumesClock(at(4))).toBe("4:00 AM");
    expect(resumesClock(at(4, 5))).toBe("4:05 AM");
    expect(resumesClock(at(16, 30))).toBe("4:30 PM");
    // The two hours a 12-hour clock gets wrong if it just takes a remainder.
    expect(resumesClock(at(0, 7))).toBe("12:07 AM");
    expect(resumesClock(at(12))).toBe("12:00 PM");
  });

  it("says NOTHING when the server named no instant", () => {
    // 0, absent and unreadable are one answer — "the server could not say" —
    // and the caller drops the clause rather than printing a clock nobody can
    // trust.
    expect(resumesClock(0)).toBe("");
    expect(resumesClock(undefined)).toBe("");
    expect(resumesClock(null)).toBe("");
    expect(resumesClock(Number.NaN)).toBe("");
    expect(resumesClock(-5)).toBe("");
  });
});

describe("the two sentences", () => {
  it("names the cause on a ROW — `Usage limit · resumes 4:00 AM`", () => {
    expect(usageLimitCaption(limited())).toBe("Usage limit · resumes 4:00 AM");
    // With no instant, the clause goes and the name stays: "it is the plan, not
    // a crash" is the half the reader most needs.
    expect(usageLimitCaption(limited({ resumes_at: 0 }))).toBe("Usage limit");
    // …and nothing at all on every other row, so the surfaces draw no line.
    expect(usageLimitCaption({ status: "blocked", blocked_reason: "failed" })).toBe("");
    expect(usageLimitCaption({ status: "queued" })).toBe("");
  });

  it("says `paused · resumes 4:00 AM` at the top of the CHAT", () => {
    // Inside the conversation the question is "why is nothing happening", and
    // the answer is "it will start again by itself, then". The cause is named on
    // the row instead, where it has other tasks to be told apart from.
    expect(usageLimitStatusWord(limited())).toBe("paused · resumes 4:00 AM");
    expect(usageLimitStatusWord(limited({ resumes_at: 0 }))).toBe("paused");
    expect(usageLimitStatusWord({ status: "in_progress" })).toBe("");
  });
});

describe("the surfaces that say them", () => {
  it("draws the caption on the List row, the Board card and the Cards wall", () => {
    // One function, three surfaces: a stopped session is described one way
    // everywhere, which is what stops the three drifting apart.
    expect(VIEWS).toContain("usageLimitCaption,");
    expect(VIEWS.match(/const limit = usageLimitCaption\(task\);/g)).toHaveLength(2);
    expect(VIEWS).toContain('<span className="tasks-row-queue" data-hint={limit}>');
    expect(VIEWS).toContain('{limit && <span className="tasks-card-queue">{limit}</span>}');
    expect(CARDS).toContain("usageLimitCaption,");
    expect(CARDS).toContain('{limit && <span className="task-card-limit">{limit}</span>}');
    // …and the Cards wall's line measures rather than guessing: no width, no
    // breakpoint, it ellipsises inside itself.
    const css = CARDS_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css.slice(
      css.indexOf(".task-card-limit {"),
      css.indexOf("}", css.indexOf(".task-card-limit {")),
    );
    expect(rule).toContain("text-overflow: ellipsis");
    expect(rule).toContain("max-width: 100%");
    expect(rule).not.toContain("width: 2");
  });

  it("replaces `running` at the top of the chat, without the shimmer", () => {
    expect(TOPBAR).toContain("status?: string;");
    expect(TOPBAR).toContain('<span className="c-tb-paused" aria-live="polite">');
    // It REPLACES the live word rather than sitting beside it: they answer one
    // question, and a paused session is precisely one where nothing is running.
    expect(TOPBAR).toContain("      ) : running ? (");
    expect(CHAT).toContain("const limitWord = usageLimitStatusWord(sched.rec);");
    expect(CHAT.slice(CHAT.indexOf("<Topbar"))).toContain("status={limitWord}");
    // No animation on the one word whose content is that nothing is moving —
    // and Blocked's own red, the colour its ring wears on the Tasks page.
    const css = COMPOSER_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css.slice(
      css.indexOf(".c-tb-paused {"),
      css.indexOf("}", css.indexOf(".c-tb-paused {")),
    );
    expect(rule).toContain("color: var(--status-failed)");
    expect(rule).not.toContain("animation");
  });
});
