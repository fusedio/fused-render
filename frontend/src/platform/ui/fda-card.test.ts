// ---- the Full Disk Access nudge ------------------------------------------------
// Claims about FdaCard that no pure function holds — which config state renders
// it, what the buttons do, and where it sits in the notification stack. Pinned
// to the source, this repo's habit for exactly this kind of claim (see
// repoCardControls.test.ts). The card is macOS-packaged-only, so a browser pass
// on a dev machine needs FUSED_RENDER_FDA_BANNER=1 to even see it — these pins
// hold the contract the rest of the time.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dir;
const CARD = readFileSync(join(HERE, "FdaCard.tsx"), "utf8");
const HOST = readFileSync(join(HERE, "NotificationHost.tsx"), "utf8");
const API = readFileSync(join(HERE, "../lib/api.ts"), "utf8");

describe("when the card renders", () => {
  it("not at launch — only once the session has read under a protected folder", () => {
    // Until the server flips `relevant` (its fs routes call note_touch), the
    // card quietly watches config and renders nothing.
    expect(CARD).toContain('setStage(fda.relevant ? "offer" : "watching")');
    expect(CARD).toContain('if (stage === "hidden" || stage === "watching") return null');
  });

  it("an absent fda field renders nothing and never starts watching", () => {
    expect(CARD).toContain('useState<Stage>("hidden")');
    expect(CARD).toContain("if (!fda || fda.granted || fda.dismissed) return;");
  });

  it("a grant or dismissal arriving mid-watch stops the watching poll", () => {
    expect(CARD).toContain('if (!fda || fda.granted || fda.dismissed) setStage("hidden")');
    expect(CARD).toContain('else if (fda.relevant) setStage("offer")');
  });

  it("waiting is never a dead end: it keeps a Not now, and its poll bails too", () => {
    // Backing out of System Settings without granting (Bugbot, PR #831) must
    // not strand the card on "In System Settings…" for the session. Anchor on
    // the waiting arm's own button — `{waiting ? (` alone finds the BODY
    // ternary first, whose arm holds no buttons at all.
    const at = CARD.indexOf("Reopen System Settings");
    expect(at).toBeGreaterThan(-1);
    const waitingArm = CARD.slice(at, CARD.indexOf(") : (", at));
    expect(waitingArm).toContain("dismissFdaNudge()");
    expect(CARD).toContain("} else if (!fda || fda.dismissed) {");
  });
});

describe("what the buttons do", () => {
  it("Open System Settings flips to the waiting instructions and opens the pane", () => {
    expect(CARD).toContain('setStage("waiting")');
    expect(CARD).toContain("openFdaSettings()");
  });

  it("Not now persists server-side — forever, not per-tab", () => {
    expect(CARD).toContain("dismissFdaNudge()");
    expect(API).toContain('postJson<{ ok: boolean }>("/api/fda/dismiss", {})');
  });

  it("while waiting, the grant landing hides the card and fires a toast", () => {
    expect(CARD).toContain("if (fda?.granted)");
    expect(CARD).toContain('pushToast({ msg: "Full Disk Access is on');
  });

  it("the offer body is the how-to steps", () => {
    expect(CARD).toContain("fda-steps");
    expect(CARD).toContain("Full Disk Access</li>");
  });
});

describe("where it sits", () => {
  it("in the notification stack, above the server card, top-level document only", () => {
    const fdaAt = HOST.indexOf("<FdaCard />");
    const serverAt = HOST.indexOf("<ServerStatusBanner />");
    expect(fdaAt).toBeGreaterThan(-1);
    expect(fdaAt).toBeLessThan(serverAt);
    expect(HOST).toContain("{!IS_EMBED && <FdaCard />}");
  });
});
