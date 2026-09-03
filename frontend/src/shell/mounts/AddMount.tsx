// The "Add a mount" section — the page's one add flow, and the only place a
// provider gets connected.
//
// LINK-FIRST, because that is how it is actually used: people arrive with an
// s3:// URL or a console tab and paste it, and Name/Remote/Path are then filled
// in for them. The old form rendered the link box as a fourth co-equal field
// above three others, which said the opposite. Here the link input owns its own
// row and the three details sit under it as confirm-and-correct.
//
// The provider cards live at the FOOT of this same section rather than in a
// sibling "Add storage" heading. They were a second section with its own
// taxonomy, so connecting Drive and then mounting from it read as two unrelated
// features; they are the "no link yet" branch of one flow.
import { useEffect, useId, useRef, useState } from "react";
import { createDetectedRemote, createMount } from "@platform/lib/api";
import type { RcloneRemote, RemoteKind, RemoteSuggestion } from "@platform/lib/api";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Muted, SectionHeading } from "@platform/ui/flow/Typography";
import { Code, Note } from "./bits";
import { ProviderPicker } from "./setup";
import type { SetupKey } from "./setup";
import {
  mountRootForLink,
  parseStorageUrl,
  pickRemote,
  shouldApplyPreselect,
  suggestMountName,
} from "./links";
import type { RemoteChoice, RemoteHandoff } from "./links";

// The Remote dropdown's groups, in display order.
//
// Grouped by HOW A REMOTE IS REACHED, never by whether it has been created yet.
// Created-ness used to be the axis — one "Remotes" group plus two groups of
// not-yet-created suggestions — which is an implementation detail no user
// thinks in, and it degenerated badly at both ends: create everything and the
// two labelled groups vanish, leaving one flat list where an anonymous
// read-only remote sits between two credentialed ones, distinguishable only by
// reading to the end of its label. A suggestion and the remote it becomes now
// live in the same group; the "+" prefix on its option is the only difference,
// since picking it costs a creation round-trip.
const REMOTE_GROUPS: { kind: RemoteKind; label: string }[] = [
  { kind: "other", label: "Your remotes" },
  { kind: "detected", label: "Detected credentials (no keys stored)" },
  { kind: "public", label: "Public datasets (no credentials)" },
];

// How long the details grid stays highlighted after a setup flow hands a remote
// back. Long enough to be seen by someone whose eyes are still on the closing
// modal, short enough not to become page furniture.
const HANDOFF_FLASH_MS = 1800;

export function AddMount({
  remotes,
  suggested,
  preselect,
  onChanged,
  onPickProvider,
}: {
  remotes: RcloneRemote[];
  suggested: RemoteSuggestion[];
  // The last setup flow to finish, to select here once the reload carrying its
  // remote lands. Null when nothing is pending. Nonce-keyed so that connecting
  // the SAME remote twice is two handoffs, not one — see RemoteHandoff.
  preselect: RemoteHandoff | null;
  onChanged: () => void;
  onPickProvider: (key: SetupKey) => void;
}) {
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [subpath, setSubpath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Until the user edits Name themselves, it follows the path (the "slug tracks
  // title" pattern) — the mount name and its bucket/prefix are usually the
  // same, so typing the path twice is pure friction.
  const [nameTouched, setNameTouched] = useState(false);
  const ids = { link: useId(), name: useId(), remote: useId(), path: useId() };

  const onPathChange = (v: string) => {
    setSubpath(v);
    if (!nameTouched) setName(suggestMountName(v));
  };

  // A pasted S3/GCS link (see parseStorageUrl) that auto-fills the fields below.
  const [link, setLink] = useState("");

  // The suggestions this form may OFFER. `suggested` carries every suggestion,
  // including ones already materialized (the setup panels show those too), but
  // every option here submits "suggest:<id>" to create the remote on the fly —
  // offering an existing one would 409 or duplicate it. The materialized ones
  // are already listed under Remotes, so nothing is lost by dropping them here.
  const offerable = suggested.filter((s) => !s.exists);

  // -- the setup-flow handoff --------------------------------------------------
  //
  // A setup flow creates a REMOTE, which mounts nothing. The modal closing used
  // to be the entire feedback: the user was left to notice a new name in a
  // dropdown further down a page they were not looking at. So the handoff is
  // made visible — scroll this section into view, focus Path, flash the grid,
  // and say in words what just happened and what to do next.
  const sectionRef = useRef<HTMLElement>(null);
  const appliedNonce = useRef<number | null>(null);
  const [flash, setFlash] = useState(false);
  const [connected, setConnected] = useState<string | null>(null);
  // The flash timer lives in a ref, NOT in the effect's cleanup. This effect
  // re-runs on every `remotes` change — the 8s upload poll, and the
  // refresh-on-return that fires the moment the user comes back from the OAuth
  // browser tab — and a cleanup-owned timer was therefore cancelled by an
  // unrelated reload without ever being rescheduled, leaving the flash
  // painted on forever and every later handoff unable to replay it.
  const flashTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    },
    [],
  );
  useEffect(() => {
    if (!shouldApplyPreselect(preselect, appliedNonce.current, remotes.map((r) => r.name))) return;
    appliedNonce.current = preselect!.nonce;
    setRemote(preselect!.remote);
    setConnected(preselect!.remote);
    sectionRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    // Focus by id: the shadcn Input is a function component (React 18, no ref
    // forwarding), so the handoff reaches the field the way a <label> would.
    document.getElementById(ids.path)?.focus();
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    setFlash(true);
    flashTimer.current = window.setTimeout(() => {
      flashTimer.current = null;
      setFlash(false);
    }, HANDOFF_FLASH_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preselect, remotes]);

  // Everything the Remote picker can offer, as one list. A remote the user has
  // (value = its verbatim rclone spec) and a suggestion that becomes one on
  // submit (value = "suggest:<id>") differ only in `creates`; `kind` and
  // `provider` come from the SERVER for both, classified from the stored rclone
  // config rather than sniffed out of names and label substrings on this side.
  const choices: RemoteChoice[] = [
    ...remotes.map((r) => ({
      value: r.name,
      label: r.label,
      kind: r.kind,
      provider: r.provider,
      creates: false,
    })),
    ...offerable.map((s) => ({
      value: `suggest:${s.id}`,
      label: s.label,
      kind: s.kind,
      provider: s.provider,
      creates: true,
    })),
  ];
  // What the trigger shows for each value — the label, with the "+" that marks
  // a suggestion still to be created. base-ui renders the raw value otherwise.
  const selectItems = choices.map((c) => ({ value: c.value, label: c.creates ? `+ ${c.label}` : c.label }));

  const parsedLink = parseStorageUrl(link);
  const linkRemote = parsedLink ? pickRemote(choices, parsedLink.provider) : undefined;

  const applyLink = (raw: string) => {
    setLink(raw);
    const parsed = parseStorageUrl(raw);
    if (!parsed) return;
    const rv = pickRemote(choices, parsed.provider);
    if (rv) setRemote(rv);
    const rooted = mountRootForLink(parsed.path);
    setSubpath(rooted);
    // Name from the MOUNTED root (the dataset/collection), not a deep scene or
    // file name — and keep it tracking Path edits (no hand-typed name yet).
    setName(suggestMountName(rooted));
    setNameTouched(false);
  };

  // The rclone spec the Add button will mount, previewed live so it matches
  // what the mounted row then shows. A "suggest:<id>" selection resolves to
  // its real remote name at submit; use the suggestion's name for the preview.
  const resolvedBase = remote.startsWith("suggest:")
    ? `${offerable.find((s) => `suggest:${s.id}` === remote)?.remote_name ?? ""}:`
    : remote;
  const spec = resolvedBase && resolvedBase !== ":" ? resolvedBase + subpath : "";

  // Whether the typed Name is one add_mount() will accept — non-empty after
  // trimming, and no / \ : or leading dot. Gating the button and the preview
  // on this keeps the preview from ever describing a folder the server rejects
  // (auto-derived names are already folderSafe; this catches manual edits).
  const trimmedName = name.trim();
  const nameValid = trimmedName !== "" && !/[/\\:]/.test(trimmedName) && !trimmedName.startsWith(".");

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      // A "suggest:<id>" selection is a detected credential source, not an
      // existing remote — materialize it into a keyless remote first, then
      // mount against the real name it returns.
      let base = remote;
      if (remote.startsWith("suggest:")) {
        base = (await createDetectedRemote(remote.slice("suggest:".length))).name;
      }
      await createMount(name, base + subpath);
      setName("");
      setSubpath("");
      setRemote("");
      setLink("");
      setNameTouched(false);
      setConnected(null);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-3" ref={sectionRef}>
      <SectionHeading>Add a mount</SectionHeading>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && nameValid && remote) void add();
        }}
      >
        <div className="space-y-1.5">
          <Field>
            <FieldLabel htmlFor={ids.link}>Paste a storage link</FieldLabel>
            <Input
              id={ids.link}
              placeholder="s3://bucket/prefix, gs://bucket/prefix, or an S3/GCS console URL"
              value={link}
              onChange={(e) => applyLink(e.target.value)}
            />
          </Field>
          {link.trim() &&
            (parsedLink ? (
              <Note>
                Recognized {parsedLink.provider.toUpperCase()} link — filled the details below
                {linkRemote ? "" : "; pick a remote"}.
                {mountRootForLink(parsedLink.path) !== parsedLink.path
                  ? " Trimmed to the dataset root — edit Path to mount deeper."
                  : " Review, then mount."}
              </Note>
            ) : (
              <Note tone="warn">Not a recognized S3/GCS link — fill the details below manually.</Note>
            ))}
          {/* The setup handoff, said out loud. Without it the only evidence a
              sign-in worked was a dropdown quietly changing value. */}
          {connected && (
            <Note tone="ok" role="status">
              <Code>{connected}</Code> connected — pick a folder path and add the mount.
            </Note>
          )}
        </div>

        {/* The three details + submit on one row (stacked narrow). The handoff
            flash is a short background tint, motion-safe only. */}
        <div
          className={cn(
            "grid gap-3 items-end grid-cols-1 sm:grid-cols-[1fr_1.4fr_1.4fr_auto] rounded-md -m-1 p-1",
            "motion-safe:transition-colors motion-safe:duration-500",
            flash && "bg-accent/40",
          )}
        >
          <Field>
            <FieldLabel htmlFor={ids.name}>
              Name <span aria-hidden="true" className="text-muted-foreground">*</span>
            </FieldLabel>
            <Input
              id={ids.name}
              required
              placeholder="e.g. sensor-data"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setNameTouched(true);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={ids.remote}>
              Remote <span aria-hidden="true" className="text-muted-foreground">*</span>
            </FieldLabel>
            {/* base-ui wants `null` for "nothing picked"; the form keeps "". */}
            <Select items={selectItems} value={remote || null} onValueChange={(v) => setRemote(v ?? "")}>
              <SelectTrigger id={ids.remote} className="w-full">
                <SelectValue placeholder="— remote —" />
              </SelectTrigger>
              <SelectContent>
                {REMOTE_GROUPS.map((g) => {
                  const items = choices.filter((c) => c.kind === g.kind);
                  if (items.length === 0) return null;
                  return (
                    <SelectGroup key={g.kind}>
                      <SelectLabel>{g.label}</SelectLabel>
                      {items.map((c) => (
                        // value is the raw rclone spec — or "suggest:<id>", which
                        // add() materializes first; only the shown text differs.
                        <SelectItem key={c.value} value={c.value}>
                          {c.creates ? `+ ${c.label}` : c.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  );
                })}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor={ids.path}>Path</FieldLabel>
            <Input
              id={ids.path}
              placeholder="bucket/prefix"
              value={subpath}
              onChange={(e) => onPathChange(e.target.value)}
            />
          </Field>
          {/* Bottom-aligned by the grid, not by a blank label. */}
          <Button type="submit" disabled={busy || !nameValid || !remote}>
            {busy ? "Mounting…" : "Add & mount"}
          </Button>
        </div>
      </form>

      {spec && (
        <Note>
          Mounts <Code>{spec}</Code>
          {nameValid ? (
            <>
              {" "}
              as folder <Code>{trimmedName}</Code>
            </>
          ) : trimmedName ? (
            <span className={cn("ml-1", "text-destructive")}>— name can’t contain / \ : or start with “.”</span>
          ) : (
            <>
              {" "}
              as folder <Code>…</Code>
            </>
          )}
        </Note>
      )}

      {/* The ONE explainer this section gets. It used to carry three stacked
          paragraphs plus an essay about how the Remote dropdown is grouped. */}
      <Muted className="text-xs">
        Mount a specific <b>bucket/prefix</b> rather than a whole bucket — narrow mounts browse and search much
        faster. An entry marked <b>+</b> is set up as part of adding the mount.
      </Muted>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <ProviderPicker onPick={onPickProvider} />
    </section>
  );
}
