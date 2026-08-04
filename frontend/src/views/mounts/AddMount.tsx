// The "Add a mount" form — the page's primary action. Split out of
// views/Mounts.tsx; the pure decisions it makes live in ./links.ts.
import { useEffect, useRef, useState } from "react";
import { createDetectedRemote, createMount } from "../../lib/api";
import type { RcloneRemote, RemoteKind, RemoteSuggestion } from "../../lib/api";
import { ErrorBanner } from "../../components/ErrorBanner";
import { Field, Select, TextInput } from "../../components/field/fields";
import {
  mountRootForLink,
  parseStorageUrl,
  pickRemote,
  shouldApplyPreselect,
  suggestMountName,
} from "./links";
import type { RemoteChoice } from "./links";

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

export function AddMount({
  remotes,
  suggested,
  preselect,
  onChanged,
}: {
  remotes: RcloneRemote[];
  suggested: RemoteSuggestion[];
  // A remote spec a setup flow just created (incl. trailing ':'), to select
  // here once the reload carrying it lands. Null when nothing is pending.
  preselect: string | null;
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [subpath, setSubpath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Until the user edits Name themselves, it follows the last path segment
  // (the "slug tracks title" pattern) — the mount name and its bucket/prefix
  // are usually the same, so typing the path twice is pure friction.
  const [nameTouched, setNameTouched] = useState(false);

  const onPathChange = (v: string) => {
    setSubpath(v);
    if (!nameTouched) setName(suggestMountName(v));
  };

  // A pasted S3/GCS link (see parseStorageUrl) that auto-fills the fields below.
  const [link, setLink] = useState("");

  // The suggestions this form may OFFER. `suggested` now carries every
  // suggestion, including ones already materialized (the setup panels need to
  // show those as "already added"), but every option here submits
  // "suggest:<id>" to create the remote on the fly — offering an existing one
  // would 409 or duplicate it. The materialized ones are already listed under
  // Remotes, so nothing is lost by dropping them here.
  const offerable = suggested.filter((s) => !s.exists);

  // Pre-select the remote a setup flow just created, and focus Path — the modal
  // closing used to be the whole feedback, leaving the user to spot a new name
  // in a dropdown. Applied at most once per preselect, and only once the reload
  // carrying the remote has landed (shouldApplyPreselect owns both rules).
  const pathRef = useRef<HTMLInputElement>(null);
  const appliedPreselect = useRef<string | null>(null);
  useEffect(() => {
    if (!shouldApplyPreselect(preselect, appliedPreselect.current, remotes.map((r) => r.name)))
      return;
    appliedPreselect.current = preselect;
    setRemote(preselect!);
    pathRef.current?.focus();
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
  // what the mounted card then shows. A "suggest:<id>" selection resolves to
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
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Add mount</h2>
      <p className="deploy-muted">
        Surface a remote as a local folder. The <b>Remote</b> list groups by how each one is
        reached — your own remotes, credentials detected on this machine, and public data
        that needs none. An entry marked <b>+</b> isn’t set up yet; picking it creates the
        remote as part of adding the mount.
      </p>
      <div className="mount-paste">
        <Field label="Paste a link">
          <TextInput
            placeholder="s3://bucket/prefix, gs://bucket/prefix, or an S3/GCS console URL"
            value={link}
            onChange={(e) => applyLink(e.target.value)}
          />
        </Field>
        {link.trim() &&
          (parsedLink ? (
            <p className="deploy-muted mount-paste-hint">
              Recognized {parsedLink.provider.toUpperCase()} link — filled the fields below
              {linkRemote ? "" : "; pick a remote"}.
              {mountRootForLink(parsedLink.path) !== parsedLink.path
                ? " Trimmed to the dataset root — edit Path to mount deeper."
                : " Review, then mount."}
            </p>
          ) : (
            <p className="deploy-muted mount-paste-hint warn">
              Not a recognized S3/GCS link — fill the fields below manually.
            </p>
          ))}
      </div>
      <form
        className="mount-form-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && nameValid && remote) void add();
        }}
      >
        <Field label="Name" required>
          <TextInput
            placeholder="e.g. sensor-data"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setNameTouched(true);
            }}
          />
        </Field>
        <Field label="Remote" required>
          <Select value={remote} onChange={(e) => setRemote(e.target.value)}>
            <option value="">— remote —</option>
            {REMOTE_GROUPS.map((g) => {
              const items = choices.filter((c) => c.kind === g.kind);
              if (items.length === 0) return null;
              return (
                <optgroup key={g.kind} label={g.label}>
                  {items.map((c) => (
                    // value is the raw rclone spec — or "suggest:<id>", which
                    // add() materializes first; only the shown text differs.
                    <option key={c.value} value={c.value}>
                      {c.creates ? `+ ${c.label}` : c.label}
                    </option>
                  ))}
                </optgroup>
              );
            })}
          </Select>
        </Field>
        <Field label="Path">
          <TextInput
            ref={pathRef}
            placeholder="bucket/prefix"
            style={{ minWidth: 200 }}
            value={subpath}
            onChange={(e) => onPathChange(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button
            aligns with the input boxes, not the labels above them. */}
        <Field label={" "}>
          <button type="submit" className="btn btn-primary" disabled={busy || !nameValid || !remote}>
            {busy ? "Mounting…" : "Add & mount"}
          </button>
        </Field>
      </form>
      {spec && (
        <p className="deploy-muted mount-spec">
          Mounts <code>{spec}</code>
          {nameValid ? (
            <>
              {" "}
              as folder <code>{trimmedName}</code>
            </>
          ) : trimmedName ? (
            <span className="warn">
              {" "}
              — name can’t contain / \ : or start with “.”
            </span>
          ) : (
            <>
              {" "}
              as folder <code>…</code>
            </>
          )}
        </p>
      )}
      <p className="deploy-muted mount-paste-hint">
        Tip: mount a specific <b>bucket/prefix</b>, not a whole bucket — narrow mounts browse and
        search much faster.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}
