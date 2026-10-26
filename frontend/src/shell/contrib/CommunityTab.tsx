// The Community tab (docs/CONTRIB_LOOP.md §19.4/§19.5): the whole
// contribution loop for one app, on the app page (`variant="page"`) or in the
// explorer kebab's sheet (`variant="sheet"`, ContribModal). Owner's pick:
// "Community tab A" in the §19 design.
//
// TWO SIDES, ONE PLACE.
//   own app    the LADDER (§19.1) — three rung cards, On this Mac → On
//              GitHub → In the Marketplace, the current one emphasised, each
//              with one sentence and its buttons — then a grid: Proposals
//              (the ProposalsTab inbox), Activity (Undo as ever),
//              Contributors, Listing (details, Doctor, review, visibility).
//   a copy     the same three cards read from the other side: "It comes
//              from" / "Your changes" / "Your proposal" — the Source panel's
//              sentence and buttons split by subject — then Activity and
//              About this listing.
//
// NOTHING IS DECIDED HERE. Which rung is current comes from GET
// /api/contrib/ladder (`useLadder`); every button is one the server already
// sends — the Source panel's `ContribAction`s (`useSourcePanel`) and the
// listing's (`useListing`) — pressed through the same hooks SourcePanel and
// ListingStrip use, so a button behaves exactly as it does there. The only
// moves this file adds are doors to existing places: the Share sheet's
// GitHub card (rung 1), Copy install link (the ladder's `link`), App Doctor
// (the listing's "doctor" sheet), the .fused download.
//
// Sheets (each hook's `sheets`) are rendered ONCE, at the root, outside
// every card: a re-read that moves a rung on never unmounts a dialog.
import { useEffect, useMemo, useState, type MouseEvent, type ReactNode } from "react";
import { ArrowRight, Check, Copy, Download, ExternalLink, GitBranch, Laptop, Store } from "lucide-react";
import {
  ackContrib,
  useLadder,
  type AppContribState,
  type ContribAction,
  type LadderAnswer,
  type LadderState,
} from "@platform/lib/contrib";
import { copyToClipboard } from "@platform/lib/clipboard";
import { releaseDate } from "@platform/lib/format";
import { listingPageUrl, useCatalog } from "@platform/lib/marketplace";
import { notify } from "@platform/lib/notifications";
import { exportAppFileOnly, openShareApp } from "@platform/lib/share-app";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
import { BlockedStrip } from "./BlockedStrip";
import { PUBLISH_ID, ListingNotes, useListing, type ListingKit } from "./ListingStrip";
import { MergeReview } from "./MergeReview";
import { ProposalsTab } from "./ProposalsTab";
import { ActivityList, FileList, KitProgress, useSourcePanel, type SourcePanelKit } from "./SourcePanel";
import { QuietAction } from "./StatusStrip";
import { authorOf, githubUrl, plural, possessive } from "./words";

/** The review every listing gets (§18.2), said the same way everywhere. */
export const REVIEW_SENTENCE = "Fixed rules + Claude · every update · merged by a Fused maintainer";

// ---- which card a copy's button belongs to (§19.1 "split by subject") --------
//
// The server's ids (contrib/actions.py). An id not named here stays with
// "It comes from", so a new server button never vanishes.
const CHANGES_IDS = new Set(["propose", "remix", "discard", "push-main", "save-edits", "put-on-top"]);
const PROPOSAL_IDS = new Set(["update-proposal", "withdraw"]);
type CopyCard = "source" | "changes" | "proposal";
export function copyCardOf(id: string): CopyCard {
  if (CHANGES_IDS.has(id)) return "changes";
  if (PROPOSAL_IDS.has(id)) return "proposal";
  return "source";
}

/** The listing states whose buttons live on rung 3 (anything but "not listed
 *  yet", whose next action sits on rung 2). */
const NOT_LISTED = new Set(["not-listed"]);

export interface CommunityTabProps {
  dir: string;
  slug: string;
  contrib: AppContribState;
  variant: "page" | "sheet";
  /** The page's own ladder read (shared with its tab offer); the tab reads
   *  its own when not given. */
  ladder?: LadderState;
}

export function CommunityTab({ dir, slug, contrib, variant, ladder: given }: CommunityTabProps) {
  const own = useLadder(given ? null : dir);
  const ladder = given ?? own;
  const l = ladder.data;
  const mine = !!l?.ok && l.mine;
  const refreshLadder = () => void ladder.refresh();
  // The page shows the block itself (the Overview's strip / BlockedNotice);
  // the sheet has no Overview, so it says it here.
  const kit = useSourcePanel({ dir, slug, contrib, blockShown: variant === "page", onChanged: refreshLadder });
  const market = mine && !!l?.flags.marketplace && (l.rung ?? 1) >= 2;
  const listing = useListing({ dir, contrib, eligible: market, onChanged: refreshLadder });
  const [doctorOpen, setDoctorOpen] = useState(false);

  // Seeing a copy's tab IS reading its newest event: say so once (the tab's
  // dot goes out on the re-read).
  const event = !mine ? (contrib.data?.copy.event ?? null) : null;
  const eventKey = event ? `${event.kind}:${event.at}` : null;
  useEffect(() => {
    if (!eventKey) return;
    void ackContrib(dir, "event").then(() => contrib.refresh());
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per event
  }, [dir, eventKey]);

  // The ladder decides which side this is: wait for it (and, for a copy,
  // for the contrib read) rather than flash the wrong view.
  if (!l || (!mine && !contrib.settled)) {
    return (
      <div className={cn("contrib-community", `is-${variant}`)} aria-busy="true">
        <Skeleton className="h-[148px] w-full rounded-[12px]" />
      </div>
    );
  }

  const body = mine ? (
    <OwnView
      dir={dir}
      slug={slug}
      contrib={contrib}
      ladder={l as LadderAnswer}
      kit={kit}
      listing={listing}
      openDoctor={() => setDoctorOpen(true)}
    />
  ) : contrib.data ? (
    <CopyView dir={dir} contrib={contrib} kit={kit} variant={variant} />
  ) : (
    <p className="contrib-community-empty">
      Nothing to show for {slug} yet. Once it is on GitHub or comes from someone else, where it stands shows up
      here.
    </p>
  );

  return (
    <div className={cn("contrib-community", `is-${variant}`)}>
      {body}
      {kit?.sheets}
      {listing?.sheets}
      {doctorOpen && (
        <AppDoctorModal
          dir={dir}
          onClose={() => {
            setDoctorOpen(false);
            refreshLadder();
          }}
        />
      )}
    </div>
  );
}

// ---- own app: the ladder + the grid ------------------------------------------

type RungState = "done" | "now" | "next";

function Rung({
  n,
  eyebrow,
  Icon,
  state,
  title,
  chip,
  children,
  actions,
}: {
  n: number;
  eyebrow: string;
  Icon: typeof Laptop;
  state: RungState;
  title: ReactNode;
  chip?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section
      className={cn("contrib-rung", `is-${state}`)}
      aria-label={`Step ${n}: ${eyebrow}`}
      aria-current={state === "now" ? "step" : undefined}
      data-rung={n}
    >
      <div className="contrib-rung-eyebrow">
        <span className="contrib-rung-mark" aria-hidden="true">
          {state === "done" ? <Check /> : <Icon />}
        </span>
        {eyebrow}
      </div>
      <h3 className="contrib-rung-title">
        <span className="contrib-rung-title-text">{title}</span>
        {chip}
      </h3>
      {children}
      {actions && <div className="contrib-rung-actions">{actions}</div>}
    </section>
  );
}

function rungState(rung: number, current: number): RungState {
  return rung < current ? "done" : rung === current ? "now" : "next";
}

/** The ladder's rung-2 sentence (§19.1): "Public · 2 edits not pushed." */
function githubSentence(g: LadderAnswer["github"]): string {
  const vis = g.visibility === "public" ? "Public" : g.visibility === "private" ? "Private" : null;
  const pushed = g.unpushed > 0 ? `${plural(g.unpushed, "edit")} not pushed yet` : "pushed";
  return `${vis ? `${vis} · ` : ""}${pushed}.`;
}

/** Rung 3's title for the listing's state. */
function listingTitle(k: ListingKit | null, l: LadderAnswer): string {
  const state = k?.state ?? l.listing?.state ?? null;
  const when = releaseDate(k?.listing?.approved?.at ?? l.listing?.listing?.approved?.at ?? null);
  switch (state) {
    case "live":
      return when ? `Live · release of ${when}` : "Live";
    case "in-review":
      return "In review";
    case "removing":
      return "Being removed";
    case "needs-confirmation":
      return "Needs your confirmation";
    case "failed":
      return "The review didn't pass";
    case "signed-out":
      return "Sign in to list it";
    case "pending":
    case "unknown":
      return "Checking…";
    default:
      return "Not listed yet";
  }
}

function OwnView({
  dir,
  slug,
  contrib,
  ladder: l,
  kit,
  listing,
  openDoctor,
}: {
  dir: string;
  slug: string;
  contrib: AppContribState;
  ladder: LadderAnswer;
  kit: SourcePanelKit | null;
  listing: ListingKit | null;
  openDoctor: () => void;
}) {
  const c = contrib.data;
  const marketOn = l.flags.marketplace;
  // Without the Marketplace the ladder has two rungs (§19.2).
  const rung = l.rung ?? 1;
  const current = marketOn ? rung : Math.min(rung, 2);
  const link = l.flags.github_link ? l.github.link : null;
  const listingState = listing?.state ?? l.listing?.state ?? null;
  const notListed = !listingState || NOT_LISTED.has(listingState);
  const busy = !!kit?.busy || !!listing?.busy;

  const copyLink = async () => {
    if (!link) return;
    const ok = await copyToClipboard(link);
    notify({ title: ok ? "Install link copied" : "Couldn't copy the link", tone: ok ? "info" : "error" });
  };
  const copyLinkButton = (primary: boolean) =>
    link ? (
      primary ? (
        <Button key="copy-link" size="sm" onClick={() => void copyLink()}>
          <Copy data-icon="inline-start" />
          Copy install link
        </Button>
      ) : (
        <QuietAction key="copy-link" onClick={() => void copyLink()}>
          Copy install link
        </QuietAction>
      )
    ) : null;

  // ---- rung 2's next action: List it in the Marketplace ----------------------
  let next: ReactNode = null;
  if (current === 2 && !marketOn) {
    next = copyLinkButton(true);
  } else if (current === 2 && notListed) {
    const publish = listing?.actions.find((a) => a.id === PUBLISH_ID) ?? null;
    const gate = listing?.primary && listing.primary.id !== PUBLISH_ID && listing.primary.id !== "open-doctor"
      ? listing.primary
      : null;
    if (l.doctor.clear !== true) {
      next = (
        <div className="contrib-rung-gate">
          <Button size="sm" disabled>
            <Store data-icon="inline-start" />
            List it in the Marketplace
          </Button>
          <span className="contrib-rung-hint">
            Turns on when Doctor is green
            {l.doctor.failing ? ` (${plural(l.doctor.failing, "check")} to fix)` : ""}.
          </span>
          <QuietAction onClick={openDoctor}>Fix with Claude</QuietAction>
        </div>
      );
    } else if (publish && listing) {
      next = (
        <Button size="sm" onClick={() => listing.press(publish)} disabled={busy}>
          <Store data-icon="inline-start" />
          List it in the Marketplace
        </Button>
      );
    } else if (gate && listing) {
      // The listing's own fix comes first (Publish now while edits are not on
      // GitHub, Sign in): rung 3's sentence says why.
      next = <div className="contrib-rung-gate">{listing.button({ ...gate, primary: true })}</div>;
    } else {
      next = (
        <Button size="sm" disabled>
          <Store data-icon="inline-start" />
          List it in the Marketplace
        </Button>
      );
    }
  }

  // The Source panel's buttons sit on rung 2 (the GitHub side): the server's
  // primary as a button, the rest quiet, Check now in the foot line.
  const sourceButtons = kit ? kit.rest.map(kit.button) : [];
  const panelSays = kit && kit.tone !== "success" && kit.tone !== "neutral" ? kit.panel.sentence : null;

  const inboxOffered = !!c?.flags.inbox && !!c.inbox;
  const waiting = c?.inbox?.count ?? 0;
  const people = c?.flags.loop ? (c.contributors ?? []) : [];

  return (
    <>
      <div className={cn("contrib-ladder", !marketOn && "is-two")} aria-label="Where this app stands">
        <Rung n={1} eyebrow="On this Mac" Icon={Laptop} state={rungState(1, current)} title="Your app">
          <p className="contrib-rung-sentence">{current === 1 ? "Only you have it." : `${slug}, on this Mac.`}</p>
          {current === 1 && l.github.blocked && <p className="contrib-rung-detail">{l.github.blocked}</p>}
          <div className="contrib-rung-actions">
            {current === 1 && (
              <Button size="sm" onClick={() => openShareApp({ path: dir, name: slug })}>
                <GitBranch data-icon="inline-start" />
                Put it on GitHub
              </Button>
            )}
            <QuietAction onClick={() => void exportAppFileOnly({ path: dir, name: slug })}>
              <Download aria-hidden="true" />
              Download .fused
            </QuietAction>
          </div>
        </Rung>

        <Rung
          n={2}
          eyebrow="On GitHub"
          Icon={GitBranch}
          state={rungState(2, current)}
          title={
            l.github.repo ? (
              <a href={githubUrl(l.github.repo)} target="_blank" rel="noopener noreferrer">
                {l.github.repo}
              </a>
            ) : (
              "Not on GitHub yet"
            )
          }
        >
          <p className="contrib-rung-sentence">
            {current >= 2 ? githubSentence(l.github) : "Put it on GitHub to share an install link with anyone."}
          </p>
          {current >= 2 && kit && (
            <>
              {panelSays && <p className="contrib-rung-detail">{panelSays}</p>}
              <FileList files={kit.panel.files} />
              <KitProgress kit={kit} />
              {kit.merge && (
                <MergeReview dir={dir} job={kit.merge} brief disabled={!!kit.busy} refresh={contrib.refresh} />
              )}
              {kit.error && !kit.sheetOpen && (
                <p className="contrib-error" role="alert">
                  {kit.error}
                </p>
              )}
            </>
          )}
          {current >= 2 && (
            <div className="contrib-rung-actions">
              {sourceButtons}
              {current === 2 && !marketOn ? null : copyLinkButton(false)}
              {kit?.footCheck && (
                <QuietAction onClick={() => kit.press(kit.footCheck as ContribAction)} disabled={!!kit.busy}>
                  {kit.checkLabel(kit.footCheck)}
                </QuietAction>
              )}
            </div>
          )}
          {next && <div className="contrib-rung-next">{next}</div>}
        </Rung>

        {marketOn && (
          <Rung
            n={3}
            eyebrow="In the Marketplace"
            Icon={Store}
            state={rungState(3, current)}
            title={listingTitle(listing, l)}
            chip={
              listingState === "live" ? (
                <Badge variant="outline" className="contrib-rung-chip is-ok">
                  Reviewed
                </Badge>
              ) : null
            }
          >
            <p className="contrib-rung-sentence">{listing?.sentence ?? l.listing?.sentence ?? ""}</p>
            {listing && !notListed && <ListingNotes kit={listing} />}
            {listing && !notListed && (
              <div className="contrib-rung-actions">
                {listing.primary && listing.button(listing.primary)}
                {listing.others.map(listing.button)}
                {listing.listing?.listed && listing.listing.slug && (
                  <QuietAction href={listingPageUrl(listing.listing.slug)}>Listing page</QuietAction>
                )}
              </div>
            )}
          </Rung>
        )}
      </div>

      <div className="contrib-grid">
        {inboxOffered && (
          <Card title={waiting > 0 ? `Proposals · ${waiting} waiting` : "Proposals"}>
            <ProposalsTab dir={dir} slug={slug} contrib={contrib} />
          </Card>
        )}
        {kit && kit.activity.length > 0 && (
          <Card title="Activity">
            <ActivityList kit={kit} />
          </Card>
        )}
        {people.length > 0 && (
          <Card title="Contributors">
            <People people={people} author={l.github.login} />
            <Remixes slug={listing?.listing?.slug ?? null} />
          </Card>
        )}
        {marketOn && current >= 2 && (
          <Card title="Listing">
            <Row label="Name, description, category, tags, icon, preview">
              {listing ? (
                <Button size="sm" variant="outline" onClick={listing.openPublish} disabled={busy}>
                  Edit details
                </Button>
              ) : null}
            </Row>
            <Row label="Doctor">
              <button
                type="button"
                className={cn("contrib-chip", l.doctor.clear === true ? "is-ok" : l.doctor.clear === false && "is-warn")}
                onClick={openDoctor}
                title="Open App Doctor"
              >
                {l.doctor.clear === true
                  ? "Ready to publish"
                  : l.doctor.failing
                    ? `${plural(l.doctor.failing, "check")} to fix`
                    : "Open App Doctor"}
              </button>
            </Row>
            <Row label="Review">
              <span className="contrib-row-quiet">{REVIEW_SENTENCE}</span>
            </Row>
            <Row label="Repository visibility">
              <span className="contrib-row-quiet">
                {l.github.visibility === "public"
                  ? "Public (the Marketplace needs it)"
                  : l.github.visibility === "private"
                    ? "Private · the Marketplace needs a public repository"
                    : "Checking…"}
              </span>
            </Row>
          </Card>
        )}
      </div>
    </>
  );
}

// ---- a copy of someone else's app: the other side ----------------------------

function CopyView({
  dir,
  contrib,
  kit,
  variant,
}: {
  dir: string;
  contrib: AppContribState;
  kit: SourcePanelKit | null;
  variant: "page" | "sheet";
}) {
  const c = contrib.data;
  if (!c) return null;
  const copy = c.copy;
  const who = kit?.who ?? authorOf(copy);
  const proposal = c.proposal;
  const by = (card: CopyCard) => (kit ? kit.rest.filter((a) => copyCardOf(a.id) === card) : []);
  const buttons = (card: CopyCard) => by(card).map((a) => kit!.button(a));
  const marketplace = copy.kind === "marketplace";
  const repo = kit?.panel.repo ?? copy.repo;
  const people = c.flags.loop ? (c.contributors ?? []) : [];
  const version = copy.approved?.version ?? null;
  const releasedOn = releaseDate(copy.approved?.at ?? null);

  const sourceTitle = marketplace
    ? `${possessive(who)} reviewed releases`
    : copy.remix_of
      ? `Remix of ${copy.remix_of}`
      : repo
        ? repo
        : `${possessive(who)} app`;
  const changes = copy.changes;
  const proposalTitle = !proposal
    ? "Nothing sent yet"
    : proposal.state === "merged"
      ? "Accepted"
      : proposal.state === "declined"
        ? "Declined"
        : proposal.state === "commented"
          ? `${who} replied`
          : `Waiting for ${who}`;

  const changesCurrent = changes > 0 && !proposal;
  const proposalCurrent = !!proposal && (proposal.state === "waiting" || proposal.state === "commented");

  return (
    <>
      {variant === "sheet" && kit && (kit.block || kit.blockWarn) && (
        <BlockedStrip
          dir={dir}
          block={kit.block}
          warn={kit.blockWarn}
          who={who}
          actions={kit.allActions}
          disabled={!!kit.busy}
          onChanged={() => void contrib.refresh()}
        />
      )}
      <div className="contrib-ladder" aria-label="Where this copy stands">
        <Rung
          n={1}
          eyebrow="It comes from"
          Icon={marketplace ? Store : GitBranch}
          state={!changesCurrent && !proposalCurrent ? "now" : "next"}
          title={sourceTitle}
          chip={
            version ? (
              <Badge variant="outline" className="contrib-rung-chip is-ok">
                v{version}
              </Badge>
            ) : null
          }
        >
          <p className="contrib-rung-sentence">{kit?.panel.sentence ?? ""}</p>
          {kit && (
            <>
              <FileList files={kit.panel.files} />
              <KitProgress kit={kit} />
              {kit.merge && (
                <MergeReview dir={dir} job={kit.merge} brief disabled={!!kit.busy} refresh={contrib.refresh} />
              )}
              {kit.error && !kit.sheetOpen && (
                <p className="contrib-error" role="alert">
                  {kit.error}
                </p>
              )}
              <p className="contrib-rung-foot">
                {kit.checked ? `Checked ${kit.checked}` : "Not checked yet"}
                {kit.footCheck && (
                  <>
                    {" · "}
                    <QuietAction onClick={() => kit.press(kit.footCheck as ContribAction)} disabled={!!kit.busy}>
                      {kit.checkLabel(kit.footCheck)}
                    </QuietAction>
                  </>
                )}
                {kit.dismiss && (
                  <>
                    {" · "}
                    <QuietAction onClick={kit.dismiss}>Dismiss</QuietAction>
                  </>
                )}
              </p>
            </>
          )}
          {by("source").length > 0 && <div className="contrib-rung-actions">{buttons("source")}</div>}
        </Rung>

        <Rung
          n={2}
          eyebrow="Your changes"
          Icon={Laptop}
          state={changesCurrent ? "now" : "next"}
          title={changes > 0 ? `${plural(changes, "file")} changed` : "No changes of yours"}
        >
          <p className="contrib-rung-sentence">
            {changes > 0
              ? `They stay on your copy. Send them to ${who}, or make them an app of your own.`
              : `Edit the app and what you change shows up here, ready to send to ${who}.`}
          </p>
          {by("changes").length > 0 && <div className="contrib-rung-actions">{buttons("changes")}</div>}
        </Rung>

        <Rung
          n={3}
          eyebrow="Your proposal"
          Icon={ArrowRight}
          state={proposalCurrent ? "now" : "next"}
          title={proposalTitle}
        >
          <p className="contrib-rung-sentence">
            {proposal ? (
              <>
                &ldquo;{proposal.title}&rdquo;
                {proposal.comment?.body ? ` · ${proposal.comment.author}: ${proposal.comment.body}` : ""}
              </>
            ) : changes > 0 ? (
              `Propose your changes and ${who} decides whether to take them.`
            ) : (
              "When you propose a change, its answer shows up here."
            )}
          </p>
          {(proposal || by("proposal").length > 0) && (
            <div className="contrib-rung-actions">
              {buttons("proposal")}
              {proposal?.url && (
                <QuietAction href={proposal.url}>
                  Open on GitHub
                  <ExternalLink aria-hidden="true" />
                </QuietAction>
              )}
            </div>
          )}
        </Rung>
      </div>

      <div className="contrib-grid">
        {kit && kit.activity.length > 0 && (
          <Card title="Activity">
            <ActivityList kit={kit} />
          </Card>
        )}
        <Card title={marketplace ? "About this listing" : "About this app"}>
          <Row label="Author">
            <span>
              <b>{who}</b>
              {repo && (
                <>
                  {" · "}
                  <a href={githubUrl(repo)} target="_blank" rel="noopener noreferrer">
                    github.com/{repo}
                  </a>
                </>
              )}
            </span>
          </Row>
          {marketplace && (
            <Row label="Reviewed">
              <span className="contrib-row-quiet">
                {releasedOn ? `Release of ${releasedOn} · ` : ""}
                {REVIEW_SENTENCE}
              </span>
            </Row>
          )}
          {people.length > 0 && (
            <Row label="Contributors">
              <span className="contrib-row-quiet">{people.map((p) => p.login || p.name).join(", ")}</span>
            </Row>
          )}
          {marketplace && <RemixesRow slug={copy.slug ?? null} />}
          {marketplace && copy.slug && (
            <Row label="Listing page">
              <QuietAction href={listingPageUrl(copy.slug)}>
                Open
                <ExternalLink aria-hidden="true" />
              </QuietAction>
            </Row>
          )}
        </Card>
      </div>
    </>
  );
}

// ---- small pieces ------------------------------------------------------------

function Card({ title, children, wide }: { title: string; children: ReactNode; wide?: boolean }) {
  return (
    <section className={cn("contrib-card", wide && "is-wide")} aria-label={title}>
      <h3 className="contrib-card-title">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children?: ReactNode }) {
  return (
    <div className="contrib-row">
      <span className="contrib-row-label">{label}</span>
      {children}
    </div>
  );
}

function People({
  people,
  author,
}: {
  people: { login: string | null; name: string; commits: number }[];
  author: string | null;
}) {
  return (
    <ul className="contrib-people">
      {people.map((p, i) => {
        const name = p.login || p.name;
        return (
          <li key={name + i} className="contrib-row">
            <span className="contrib-person">
              {p.login ? (
                <a href={`https://github.com/${p.login}`} target="_blank" rel="noopener noreferrer">
                  <b>{name}</b>
                </a>
              ) : (
                <b>{name}</b>
              )}
              <span className="contrib-row-quiet">
                {author && p.login === author ? "author" : plural(p.commits, "change")}
              </span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** The catalog's remix count for a listed slug, from what is cached (never
 *  a read of its own: "when available"). */
function useRemixCount(slug: string | null): number | null {
  const { catalog } = useCatalog(false);
  return useMemo(() => {
    if (!slug || !catalog) return null;
    const row = catalog.apps.find((a) => a.slug === slug);
    return typeof row?.remix_count === "number" ? row.remix_count : null;
  }, [catalog, slug]);
}

function Remixes({ slug }: { slug: string | null }) {
  const n = useRemixCount(slug);
  if (n === null) return null;
  return (
    <div className="contrib-row">
      <span className="contrib-row-quiet">{plural(n, "remix", "remixes")}</span>
    </div>
  );
}

function RemixesRow({ slug }: { slug: string | null }) {
  const n = useRemixCount(slug);
  if (n === null) return null;
  return (
    <Row label="Remixes">
      <span className="contrib-row-quiet">{plural(n, "remix", "remixes")}</span>
    </Row>
  );
}

// ---- the Overview's one line (§19.4) -----------------------------------------

/** States that say all is well: nothing for the Overview to point at. */
const QUIET_PANEL_STATES = new Set(["following", "published", "delisted"]);

/** "<sentence> · Community →" for the Overview, or null when there is nothing
 *  to say — so a blocked, needs-merge or waiting state is visible on first
 *  open without the whole panel. */
export function communityLine(contrib: AppContribState, ladder: LadderAnswer | null): string | null {
  const c = contrib.data;
  const waiting = c?.flags.inbox ? (c.inbox?.count ?? 0) : 0;
  if (waiting > 0) return `${plural(waiting, "proposal")} waiting for you`;
  const panel = c?.panel;
  if (panel?.sentence && !QUIET_PANEL_STATES.has(panel.state)) return panel.sentence;
  if (ladder?.ok && ladder.mine && (ladder.rung ?? 1) >= 2 && ladder.github.unpushed > 0) {
    return `${plural(ladder.github.unpushed, "edit")} not on GitHub yet`;
  }
  return null;
}

export function CommunityLine({
  sentence,
  href,
  onOpen,
}: {
  sentence: string;
  href: string;
  onOpen: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  return (
    <p className="contrib-community-line">
      <span className="contrib-community-line-text">{sentence}</span>
      <span aria-hidden="true"> · </span>
      <a href={href} onClick={onOpen}>
        Community
        <ArrowRight aria-hidden="true" />
      </a>
    </p>
  );
}

/** The tab's badge: waiting proposals on an own app, a dot for a copy's
 *  unread event. */
export function communityBadge(contrib: AppContribState, ladder: LadderAnswer | null): number | "dot" | null {
  const c = contrib.data;
  const mine = !!ladder?.ok && ladder.mine;
  if (mine || c?.copy.kind === "own") {
    const n = c?.flags.inbox ? (c.inbox?.count ?? 0) : 0;
    return n > 0 ? n : null;
  }
  return c?.copy.event ? "dot" : null;
}
