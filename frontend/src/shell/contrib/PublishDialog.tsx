// "Publish to marketplace" (CONTRIB_LOOP.md §18.6, handoff B3): the listing
// details, prefilled from the app (GET /api/contrib/listing-draft — name,
// description, category, tags, icon, preview), all editable, then
// `publish-market` with them as its args on
// /api/contrib/listing-action. The server opens the index request in the
// author's name; the review takes about half an hour and the ListingStrip
// says where it stands.
//
// THE LISTING ID IS NOT EDITABLE: the index row is named after the app's
// own fused-app-id, which the server derives from the app. The draft's
// `slug` is shown as a read-only "Listing id" line and never sent back. An
// app with no id yet is refused with reason "app-id" (App Doctor adds one),
// said like Doctor's own refusal: the sentence, and Open App Doctor.
//
// Same posture as RemixDialog: mounted per open, refuses to close while
// sending, a refusal's sentence shown inside so the next move is to fix and
// press again.
import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import {
  opensDoctor,
  plainMessage,
  getListingDraft,
  runJob,
  runListingAction,
  useUnmountSignal,
  type ListingCategory,
  type ListingDetails,
} from "@platform/lib/contrib";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ContribDialog } from "./ContribDialog";
import { JobProgress } from "./JobProgress";

/** The four categories today's Marketplace shows — the fallback when the
 *  draft does not list them. */
const FALLBACK_CATEGORIES: ListingCategory[] = [
  { id: "starters", label: "Starters" },
  { id: "local-ai", label: "Local AI" },
  { id: "productivity", label: "Productivity" },
  { id: "geospatial", label: "Geospatial" },
];

const STEPS = [{ key: "run", label: "Sending your app for review…" }];

function categoriesOf(raw: (ListingCategory | string)[] | undefined): ListingCategory[] {
  const out = (raw ?? [])
    .map((c) => (typeof c === "string" ? { id: c, label: c } : c))
    .filter((c) => c && typeof c.id === "string" && c.id);
  return out.length > 0 ? out : FALLBACK_CATEGORIES;
}

/** "a, b , c" → ["a", "b", "c"], no blanks or repeats. */
function tagsOf(text: string): string[] {
  const seen = new Set<string>();
  for (const t of text.split(",")) {
    const v = t.trim().toLowerCase();
    if (v) seen.add(v);
  }
  return [...seen];
}

type Phase = "loading" | "form" | "sending";

export function PublishDialog({
  dir,
  onClose,
  onPublished,
  onOpenDoctor,
}: {
  dir: string;
  onClose: () => void;
  /** The request is open: re-read the listing (it now says In review). */
  onPublished: (message: string | null) => void;
  /** Swap this sheet for App Doctor (a "doctor" / "app-id" refusal). */
  onOpenDoctor?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<string | null>(null);
  // The refusal on screen is one App Doctor fixes: offer it beside the line.
  const [doctorFix, setDoctorFix] = useState(false);
  const [categories, setCategories] = useState<ListingCategory[]>(FALLBACK_CATEGORIES);
  const [listingId, setListingId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("");
  const [tags, setTags] = useState("");
  const [icon, setIcon] = useState("");
  const [preview, setPreview] = useState("");
  const signal = useUnmountSignal();

  useEffect(() => {
    let live = true;
    void getListingDraft(dir).then((d) => {
      if (!live) return;
      if (d.ok === false) setError(plainMessage(d));
      const cats = categoriesOf(d.categories);
      setCategories(cats);
      setListingId(d.slug ?? "");
      setName(d.name ?? "");
      setDescription(d.description ?? "");
      setCategory(d.category && cats.some((c) => c.id === d.category) ? d.category : (cats[0]?.id ?? ""));
      setTags((d.tags ?? []).join(", "));
      setIcon(d.icon ?? "");
      setPreview(d.preview ?? "");
      setPhase("form");
    });
    return () => {
      live = false;
    };
  }, [dir]);

  const canSend = phase === "form" && name.trim() !== "" && description.trim() !== "" && category !== "";

  const send = async () => {
    if (!canSend) return;
    setPhase("sending");
    setError(null);
    setDoctorFix(false);
    const details: ListingDetails = {
      name: name.trim(),
      description: description.trim(),
      category,
      tags: tagsOf(tags),
      icon: icon.trim() || null,
      preview: preview.trim() || null,
    };
    const started = await runListingAction(dir, "publish-market", { args: { ...details } });
    if (!started.ok) {
      setError(plainMessage(started));
      setDoctorFix(opensDoctor(started.reason));
      setPhase("form");
      return;
    }
    if (!started.job) {
      onPublished(typeof started.message === "string" ? started.message : null);
      onClose();
      return;
    }
    const r = await runJob(Promise.resolve(started), { signal: signal() });
    if (!r.ok) {
      if (r.reason === "aborted") return;
      setError(r.message);
      setDoctorFix(opensDoctor(r.reason));
      setPhase("form");
      return;
    }
    const m = (r.result as { message?: unknown } | null)?.message;
    onPublished(typeof m === "string" ? m : null);
    onClose();
  };

  const sending = phase === "sending";
  let body;
  if (phase === "loading") {
    body = (
      <div className="flex flex-col gap-3" aria-hidden="true">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-[76px] w-full" />
      </div>
    );
  } else if (sending) {
    body = <JobProgress steps={STEPS} current="run" />;
  } else {
    body = (
      <>
        <FieldGroup className="gap-4">
          <Field>
            <FieldLabel htmlFor="listing-name">Name</FieldLabel>
            <Input id="listing-name" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="listing-description">Description</FieldLabel>
            <Textarea
              id="listing-description"
              value={description}
              rows={3}
              className="min-h-[76px] resize-none"
              onChange={(e) => setDescription(e.target.value)}
            />
            <FieldDescription>One or two sentences. It is what people read on the card.</FieldDescription>
          </Field>
          <div className="contrib-listing-row">
            <Field>
              <FieldLabel htmlFor="listing-category">Category</FieldLabel>
              <NativeSelect
                id="listing-category"
                className="w-full"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                {categories.map((c) => (
                  <NativeSelectOption key={c.id} value={c.id}>
                    {c.label}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </Field>
            {listingId && (
              <Field>
                <FieldLabel htmlFor="listing-id">Listing id</FieldLabel>
                <Input id="listing-id" value={listingId} readOnly />
                <FieldDescription>Your app&rsquo;s own id. It can&rsquo;t be changed.</FieldDescription>
              </Field>
            )}
          </div>
          <Field>
            <FieldLabel htmlFor="listing-tags">Tags</FieldLabel>
            <Input id="listing-tags" value={tags} placeholder="charts, maps" onChange={(e) => setTags(e.target.value)} />
            <FieldDescription>Separate tags with commas.</FieldDescription>
          </Field>
          <div className="contrib-listing-row">
            <Field>
              <FieldLabel htmlFor="listing-icon">Icon</FieldLabel>
              <Input id="listing-icon" value={icon} placeholder="icon.png" onChange={(e) => setIcon(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel htmlFor="listing-preview">Screenshot</FieldLabel>
              <Input
                id="listing-preview"
                value={preview}
                placeholder="preview.png"
                onChange={(e) => setPreview(e.target.value)}
              />
            </Field>
          </div>
          <FieldDescription>Icon and screenshot are files in your app. Leave them empty to go without.</FieldDescription>
        </FieldGroup>
      </>
    );
  }

  return (
    <ContribDialog
      open
      onClose={onClose}
      locked={sending}
      title="Publish to the Community"
      description="Fused reviews your app before it is listed, usually in about 30 minutes. Anyone can then find and install it."
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={sending}>
            Cancel
          </Button>
          <Button onClick={() => void send()} disabled={!canSend}>
            {sending && <Loader2 data-icon="inline-start" className="animate-spin" />}
            {sending ? "Sending…" : "Publish"}
          </Button>
        </>
      }
    >
      {body}
      {error && (
        <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
          {error}
        </p>
      )}
      {error && doctorFix && onOpenDoctor && (
        <div>
          <Button size="sm" variant="outline" onClick={onOpenDoctor}>
            Open App Doctor
          </Button>
        </div>
      )}
    </ContribDialog>
  );
}
