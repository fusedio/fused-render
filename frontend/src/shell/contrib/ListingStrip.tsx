// The author's Marketplace listing on the app page's Overview, under the
// Source panel (CONTRIB_LOOP.md §18.6/§18.7, handoff B3) — for an app of the
// user's own that has a GitHub repo, while the Marketplace switch is on.
//
// One strip: the server's status sentence (In review · Live · Needs your
// confirmation · Failed, or not listed yet), what the review said (its
// reasons when it failed, the new powers waiting on a confirm), and the
// listing buttons, which the server decides like every Source-panel button:
//
//   publish-market   opens PublishDialog (the details form)
//   publish-update   posts; the review takes ~30 minutes
//   confirm-powers   posts the author's "fused: confirm" for the new powers
//   fix-with-claude  posts; the answer's `open` is the new Claude session
//   check-listing    posts, then re-reads the status
//   remove-market    asks first (danger), then posts
//
// All post to /api/contrib/listing-action. The buttons are the listing
// answer's own `actions` (GET /api/contrib/listing, always present) — never
// `/api/contrib/app`'s: the listing follows the Marketplace switch alone,
// and the Source panel exists only with a Community switch on. SourcePanel
// leaves the six listing ids out of its row so they are drawn here only. The
// listing route also offers a gated publish's fix in its place:
// `publish-now` (posted like the rest), `open-doctor` (dialog "doctor":
// App Doctor opens here) and `sign-in` (dialog "sign-in").
//
// A GET whose answer carries `job` is re-reading GitHub: that job is
// watched to its end and the listing read once more (a named wait, never a
// loop).
//
// TWO RENDERERS, ONE LOGIC (§19.4): `useListing` holds the read, the
// buttons and the sheets; `ListingStrip` draws them as one strip and the
// Community tab's rung 3 and Listing card place the same pieces. Render
// `kit.sheets` exactly once.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BadgeCheck, Clock, Loader2, ShieldAlert, Stethoscope, Store } from "lucide-react";
import {
  getListing,
  okMessage,
  plainMessage,
  pollJob,
  runJob,
  runListingAction,
  useUnmountSignal,
  type AppContribState,
  type ContribAction,
  type Listing,
  type ListingAnswer,
  type ListingPower,
} from "@platform/lib/contrib";
import { releaseDate } from "@platform/lib/format";
import { useMarketplaceFeature } from "@platform/lib/marketplace-flag";
import { notify } from "@platform/lib/notifications";
import { navigateUrl } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
import { JobProgress } from "./JobProgress";
import { PublishDialog } from "./PublishDialog";
import { SignInSheet } from "./SignInSheet";
import { QuietAction, StatusStrip, type StripTone } from "./StatusStrip";
import { STEPS_FOR } from "./words";

export const PUBLISH_ID = "publish-market";
const FIX_ID = "fix-with-claude";
const CHECK_ID = "check-listing";
const REMOVE_ID = "remove-market";
/** While a review runs the status moves on its own: re-read this often. */
const REVIEW_POLL_MS = 120_000;

/** The status line when the server sends none (§18.6's four sentences). */
function fallbackSentence(l: Listing, name: string): string {
  const status = l.review?.status ?? null;
  if (status === "in-review") return `Fused is reviewing ${name}. About 30 minutes.`;
  if (status === "needs-confirmation") return `${name} adds new powers. Confirm them so the review can finish.`;
  if (status === "failed") return `The review of ${name} didn't pass.`;
  if (l.listed) {
    const when = releaseDate(l.approved?.at);
    return when ? `${name} is in the Marketplace, release of ${when}.` : `${name} is in the Marketplace.`;
  }
  return `${name} isn't in the Marketplace yet.`;
}

function toneOf(l: Listing | null, state: string | null | undefined): StripTone {
  if (state === "pending" || state === "in-review" || state === "removing") return "working";
  if (state === "failed" || state === "needs-confirmation") return "attention";
  if (state === "live") return "success";
  if (state) return "neutral";
  if (!l) return "neutral";
  const status = l.review?.status ?? null;
  if (status === "in-review") return "working";
  if (status === "failed" || status === "needs-confirmation") return "attention";
  if (l.listed) return "success";
  return "neutral";
}

/** What `useListing` hands a renderer once the first answer is in. */
export interface ListingKit {
  answer: ListingAnswer;
  listing: Listing | null;
  /** `actions_listing.decide`'s state ("not-listed", "live", …). */
  state: string | null;
  sentence: string;
  actions: ContribAction[];
  primary: ContribAction | null;
  others: ContribAction[];
  tone: StripTone;
  icon: ReactNode;
  busy: string | null;
  error: string | null;
  /** A confirm sheet is open (it shows the error inside, not the strip). */
  sheetOpen: boolean;
  job: { id: string; since: number } | null;
  /** The review's reasons when it failed. */
  reasons: string[];
  /** The new powers waiting on the author's confirm. */
  powers: ListingPower[];
  /** App Doctor stands between this app and a first listing. */
  doctorBlocks: boolean;
  /** The server already offers Open App Doctor as a button. */
  doctorOffered: boolean;
  press: (a: ContribAction) => void;
  /** A listing button: `primary` as a Button, else a quiet text action. */
  button: (a: ContribAction) => ReactNode;
  openDoctor: () => void;
  openPublish: () => void;
  /** Re-read the listing fresh (GitHub re-checked): after a Source-panel
   *  action moved the app — Publish now pushes the tip the listing
   *  compares against, so "Publish update" must appear without a reload. */
  refresh: () => Promise<void>;
  /** Every sheet the listing can open. Render exactly once. */
  sheets: ReactNode;
}

/** The listing's logic, for any renderer. Null while not eligible, before
 *  the first answer, or when the route has nothing for this folder. */
export function useListing({
  dir,
  contrib,
  eligible: eligibleOverride,
  onChanged,
}: {
  dir: string;
  contrib: AppContribState;
  /** The caller already knows (the ladder's `flags.marketplace` + `mine`);
   *  else the Marketplace switch and `/api/contrib/app`'s copy decide. */
  eligible?: boolean;
  /** Called after every action's re-read. */
  onChanged?: () => void;
}): ListingKit | null {
  const flag = useMarketplaceFeature();
  const c = contrib.data;
  // The Marketplace switch alone decides; the route answers `{ok:false}` for
  // a folder that is not an own app with a repo. When the Source panel has
  // already said the copy is someone else's, the read is skipped.
  const notMine = !!c && (c.copy.kind !== "own" || !c.copy.repo);
  const eligible = eligibleOverride ?? (flag === true && !notMine);
  const [answer, setAnswer] = useState<ListingAnswer | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [job, setJob] = useState<{ id: string; since: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sheet, setSheet] = useState<
    null | "publish" | "doctor" | "sign-in" | { confirm: ContribAction; sentence: string }
  >(null);
  const signal = useUnmountSignal();
  const genRef = useRef(0);

  const read = useCallback(
    async (fresh = false) => {
      const gen = ++genRef.current;
      const next = await getListing(dir, { fresh });
      if (gen !== genRef.current) return;
      setAnswer(next);
      // GitHub is being re-read: wait for that job, then read once more.
      if (next.job) {
        try {
          await pollJob(next.job, { signal: signal() });
        } catch {
          return; // unmounted
        }
        if (gen !== genRef.current) return;
        const after = await getListing(dir);
        if (gen === genRef.current) setAnswer({ ...after, job: null });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- signal is a stable getter
    [dir],
  );

  useEffect(() => {
    setAnswer(null);
    setError(null);
    setSheet(null);
    if (eligible) void read();
  }, [dir, eligible, read]);

  const status = answer?.listing?.review?.status ?? null;
  const state = answer?.state ?? null;
  const reviewing =
    status === "in-review" || status === "needs-confirmation" || state === "in-review" || state === "needs-confirmation";
  useEffect(() => {
    if (!eligible || !reviewing) return;
    const t = window.setInterval(() => void read(), REVIEW_POLL_MS);
    const onFocus = () => void read();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("focus", onFocus);
    };
  }, [eligible, reviewing, read]);

  // Nothing until the first answer: a guess would say "not listed" for an
  // app that is.
  if (!eligible || !answer || answer.ok === false) return null;
  const listing = answer.listing ?? null;
  const actions = answer.actions ?? [];
  if (!listing && actions.length === 0) return null;

  const name =
    listing?.name ||
    c?.copy.repo?.split("/")[1] ||
    dir.replace(/\/+$/, "").split("/").pop() ||
    "Your app";
  const sentence = answer?.sentence || (listing ? fallbackSentence(listing, name) : `${name} isn't in the Marketplace yet.`);
  const doctorOffered = actions.some((a) => a.id === "open-doctor" || a.dialog === "doctor");
  const doctorBlocks = !!listing && !listing.doctor_clear && !listing.listed;

  const run = async (a: ContribAction, confirm = false) => {
    if (busy) return false;
    setBusy(a.id);
    setError(null);
    const started = await runListingAction(dir, a.id, { confirm });
    let ok = started.ok;
    if (!started.ok && started.reason === "confirm" && !confirm) {
      setBusy(null);
      setSheet({ confirm: a, sentence: plainMessage(started) });
      return false;
    }
    if (!started.ok) {
      setError(plainMessage(started));
    } else if (a.id === FIX_ID && started.open) {
      navigateUrl(started.open);
    } else if (started.job) {
      setJob({ id: a.id, since: Date.now() });
      const r = await runJob(Promise.resolve(started), { signal: signal() });
      setJob(null);
      if (r.ok) {
        const m = okMessage(r, "");
        if (m) notify({ title: m, tone: "info" });
        const open = (r.result as { open?: unknown } | null)?.open;
        if (a.id === FIX_ID && typeof open === "string") navigateUrl(open);
      } else if (r.reason !== "aborted") {
        setError(r.message);
        ok = false;
      }
    } else {
      const m = okMessage(started, "");
      if (m) notify({ title: m, tone: "info" });
    }
    await read(a.id === CHECK_ID);
    await contrib.refresh();
    onChanged?.();
    setBusy(null);
    return ok;
  };

  const press = (a: ContribAction) => {
    if (busy) return;
    if (a.dialog === "doctor" || a.id === "open-doctor") return setSheet("doctor");
    if (a.dialog === "sign-in") return setSheet("sign-in");
    if (a.id === PUBLISH_ID || a.dialog === "listing") {
      if (doctorBlocks) return setSheet("doctor");
      return setSheet("publish");
    }
    if (a.confirm || a.id === REMOVE_ID) {
      return setSheet({
        confirm: a,
        sentence:
          a.confirm ||
          "Your app leaves the Marketplace. People who installed it keep their copy on its last reviewed release.",
      });
    }
    void run(a);
  };

  const label = (a: ContribAction) => (busy === a.id ? `${a.label}…` : a.label);
  const button = (a: ContribAction) =>
    a.primary ? (
      <Button key={a.id} variant={a.danger ? "destructive" : "default"} onClick={() => press(a)} disabled={!!busy}>
        {busy === a.id && <Loader2 data-icon="inline-start" className="animate-spin" />}
        {label(a)}
      </Button>
    ) : (
      <QuietAction key={a.id} onClick={() => press(a)} disabled={!!busy} tone={a.danger ? "danger" : undefined}>
        {label(a)}
      </QuietAction>
    );
  const primary = actions.find((a) => a.primary) ?? null;
  const others = actions.filter((a) => a !== primary);
  const tone = job || answer?.job ? "working" : toneOf(listing, state);
  const icon =
    tone === "working" ? (
      reviewing && !job ? <Clock /> : <Loader2 className="animate-spin" />
    ) : tone === "attention" ? (
      <ShieldAlert />
    ) : tone === "success" ? (
      <BadgeCheck />
    ) : (
      <Store />
    );
  const reasons = status === "failed" ? (listing?.review?.reasons ?? []) : [];
  const powers = status === "needs-confirmation" ? (listing?.review?.new_powers ?? []) : [];

  const sheets = (
    <>
      {sheet === "publish" && (
        <PublishDialog
          dir={dir}
          onClose={() => setSheet(null)}
          onPublished={(m) => {
            notify({ title: m || "Sent for review. It usually takes about 30 minutes.", tone: "info" });
            void read(true).then(() => contrib.refresh()).then(() => onChanged?.());
          }}
        />
      )}
      {sheet === "sign-in" && (
        <SignInSheet
          doing="publish to the Marketplace"
          onClose={() => setSheet(null)}
          onSignedIn={() => {
            setSheet(null);
            void read(true).then(() => contrib.refresh()).then(() => onChanged?.());
          }}
        />
      )}
      {sheet === "doctor" && (
        <AppDoctorModal
          dir={dir}
          onClose={() => {
            setSheet(null);
            void read(true).then(() => onChanged?.());
          }}
        />
      )}
      <AlertDialog
        open={typeof sheet === "object" && sheet !== null}
        onOpenChange={(o) => {
          if (!o && !busy) {
            setSheet(null);
            setError(null);
          }
        }}
      >
        {typeof sheet === "object" && sheet !== null && (
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{sheet.confirm.label}?</AlertDialogTitle>
              <AlertDialogDescription>{sheet.sentence}</AlertDialogDescription>
            </AlertDialogHeader>
            {error && (
              <p className="contrib-error" role="alert">
                {error}
              </p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={!!busy}>Cancel</AlertDialogCancel>
              <AlertDialogAction
                variant={sheet.confirm.danger || sheet.confirm.id === REMOVE_ID ? "destructive" : "default"}
                disabled={!!busy}
                onClick={async () => {
                  const ok = await run(sheet.confirm, true);
                  if (ok) setSheet(null);
                }}
              >
                {busy === sheet.confirm.id && <Loader2 data-icon="inline-start" className="animate-spin" />}
                {sheet.confirm.label}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>
    </>
  );

  return {
    answer,
    listing,
    state,
    sentence,
    actions,
    primary,
    others,
    tone,
    icon,
    busy,
    error,
    sheetOpen: sheet !== null,
    job,
    reasons,
    powers,
    doctorBlocks,
    doctorOffered,
    press,
    button,
    openDoctor: () => setSheet("doctor"),
    openPublish: () => setSheet(doctorBlocks ? "doctor" : "publish"),
    refresh: () => read(true),
    sheets,
  };
}

/** The review's reasons, the powers waiting on a confirm, App Doctor's gate,
 *  a running action's progress and the last error — everything the strip
 *  says under its sentence. */
export function ListingNotes({ kit }: { kit: ListingKit }) {
  return (
    <>
      {kit.reasons.length > 0 && (
        <ul className="contrib-listing-reasons" aria-label="Why the review didn't pass">
          {kit.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
      {kit.powers.length > 0 && (
        <ul className="contrib-listing-powers" aria-label="New powers">
          {kit.powers.map((p, i) => (
            <li key={i}>{p.detail || p.kind}</li>
          ))}
        </ul>
      )}
      {kit.doctorBlocks && !kit.doctorOffered && (
        <p className="contrib-listing-doctor">
          <Stethoscope aria-hidden="true" />
          <span>Run App Doctor first. Its sharing checks must be clear before the app can be listed.</span>
          <QuietAction onClick={kit.openDoctor}>Open App Doctor</QuietAction>
        </p>
      )}
      {kit.job && (
        <JobProgress
          steps={STEPS_FOR[kit.job.id] ?? [{ key: "run", label: "Working…" }]}
          current={STEPS_FOR[kit.job.id]?.[0]?.key ?? "run"}
          since={kit.job.since}
        />
      )}
      {kit.error && !kit.sheetOpen && (
        <p className="contrib-error" role="alert">
          {kit.error}
        </p>
      )}
    </>
  );
}

export function ListingStrip({ dir, contrib }: { dir: string; contrib: AppContribState }) {
  const kit = useListing({ dir, contrib });
  if (!kit) return null;
  const { primary, others } = kit;
  return (
    <>
      <div className="contrib-stack contrib-listing">
        <StatusStrip
          tone={kit.tone}
          icon={kit.icon}
          busy={!!kit.job}
          sentence={kit.sentence}
          primary={primary ? kit.button(primary) : null}
          secondary={others.length > 0 ? others.map(kit.button) : null}
        >
          <ListingNotes kit={kit} />
        </StatusStrip>
      </div>
      {kit.sheets}
    </>
  );
}
