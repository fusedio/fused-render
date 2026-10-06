import { describe, expect, test } from "bun:test";
import { esc, fmtAgo, fmtBytes, fmtDay, fmtSecs, fmtTime, fmtWhen, fmtWhenShort } from "./format";

describe("format", () => {
  test("esc", () => {
    expect(esc(`<a href="x">'&'</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
    expect(esc(null)).toBe("");
    expect(esc(undefined)).toBe("");
    expect(esc(42)).toBe("42");
  });
  test("fmtBytes", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1023)).toBe("1023 B");
    expect(fmtBytes(1024)).toBe("1 KB");
    expect(fmtBytes(1536 * 1024)).toBe("1.5 MB");
  });
  test("fmtSecs", () => {
    expect(fmtSecs(0)).toBe("0:00");
    expect(fmtSecs(65)).toBe("1:05");
    expect(fmtSecs(600)).toBe("10:00");
  });
  test("fmtAgo branches (fixed now)", () => {
    const now = Date.UTC(2026, 9, 2, 12, 0, 0), s = now / 1000;
    expect(fmtAgo(0, now)).toBe("");
    expect(fmtAgo(s - 30, now)).toBe("now");
    expect(fmtAgo(s + 30, now)).toBe("now");  // clock skew clamps to 0
    expect(fmtAgo(s - 600, now)).toBe("10 min ago");
    expect(fmtAgo(s - 3 * 3600, now)).toBe("3 h ago");
    expect(fmtAgo(s - 2 * 86400, now)).toBe("2 d ago");
    expect(fmtAgo(s - 10 * 86400, now)).toBe(fmtWhenShort(s - 10 * 86400, now));
  });
  test("fmtWhenShort: time today, date otherwise", () => {
    const now = new Date(2026, 9, 2, 15, 0, 0).getTime(), today = new Date(2026, 9, 2, 9, 5).getTime() / 1000;
    expect(fmtWhenShort(today, now)).toBe(fmtTime(today));
    expect(fmtWhenShort(new Date(2026, 8, 8, 9, 5).getTime() / 1000, now)).not.toBe(fmtTime(today));
    expect(fmtWhenShort(0, now)).toBe("");
  });
  test("fmtDay: Today / Yesterday / dated", () => {
    const now = new Date(2026, 9, 2, 15, 0, 0).getTime();
    const t = new Date(2026, 9, 2, 9, 5).getTime() / 1000, y = new Date(2026, 9, 1, 22, 0).getTime() / 1000;
    expect(fmtDay(t, now)).toBe(`Today ${fmtTime(t)}`);
    expect(fmtDay(y, now)).toBe(`Yesterday ${fmtTime(y)}`);
    const old = new Date(2026, 8, 8, 15, 0).getTime() / 1000;
    expect(fmtDay(old, now).endsWith(` · ${fmtTime(old)}`)).toBe(true);
  });
  test("fmtWhen", () => {
    expect(fmtWhen(0)).toBe("—");
    expect(fmtWhen(null)).toBe("—");
    expect(fmtWhen(1_700_000_000).length).toBeGreaterThan(5);
  });
});
