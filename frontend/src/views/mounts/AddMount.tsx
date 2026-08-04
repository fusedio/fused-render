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
import { useEffect, useRef, useState } from "react";
import { createDetectedRemote, createMount } from "../../lib/api";
import type { RcloneRemote, RemoteKind, RemoteSuggestion } from "../../lib/api";
import { ErrorBanner } from "../../components/ErrorBanner";
import { Field, Select, TextInput } from "../../components/field/fields";
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
  const pathRef = useRef<HTMLInputElement>(null);
  const appliedNonce = useRef<number | null>(null);
  const [flash, setFlash] = useState(false);
  const [connected, setConnected] = useState<string | null>(null);
  // The flash timer lives in a ref, NOT in the effect's cleanup. This effect
  // re-runs on every `remotes` change — the 8s upload poll, and the
  // refresh-on-return that fires the moment the user comes back from the OAuth
  // browser tab — and a cleanup-owned timer was therefore cancelled by an
  // unrelated reload without ever being rescheduled, leaving .mount-grid--flash
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
    pathRef.current?.focus();
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    setFlash(true);
    flashTimer.current = window.setTimeout(() => {
      flashTimer.current = null;
      setFlash(false);
    }, HANDOFF_FLASH_MS);
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
      setConnected(null);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section mount-add" ref={sectionRef}>
      <h2>Add a mount</h2>
      <form
        className="mount-add-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && nameValid && remote) void add();
        }}
      >
        <div className="mount-hero">
          <Field label="Paste a storage link">
            <TextInput
              placeholder="s3://bucket/prefix, gs://bucket/prefix, or an S3/GCS console URL"
              value={link}
              onChange={(e) => applyLink(e.target.value)}
            />
          </Field>
          {link.trim() &&
            (parsedLink ? (
              <p className="mount-note">
                Recognized {parsedLink.provider.toUpperCase()} link — filled the details below
                {linkRemote ? "" : "; pick a remote"}.
                {mountRootForLink(parsedLink.path) !== parsedLink.path
                  ? " Trimmed to the dataset root — edit Path to mount deeper."
                  : " Review, then mount."}
              </p>
            ) : (
              <p className="mount-note warn">
                Not a recognized S3/GCS link — fill the details below manually.
              </p>
            ))}
          {/* The setup handoff, said out loud. Without it the only evidence a
              sign-in worked was a dropdown quietly changing value. */}
          {connected && (
            <p className="mount-note ok" role="status">
              <code>{connected}</code> connected — pick a folder path and add the mount.
            </p>
          )}
        </div>

        <div className={"mount-grid" + (flash ? " mount-grid--flash" : "")}>
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
              value={subpath}
              onChange={(e) => onPathChange(e.target.value)}
            />
          </Field>
          {/* Bottom-aligned by the grid, not by a blank <Field label=" ">. That
              hack rendered a whitespace-only <label for> on the button, which
              overrode its text and left it with an EMPTY accessible name. */}
          <button
            type="submit"
            className="btn btn-primary mount-grid-submit"
            disabled={busy || !nameValid || !remote}
          >
            {busy ? "Mounting…" : "Add & mount"}
          </button>
        </div>
      </form>

      {spec && (
        <p className="mount-spec">
          Mounts <code>{spec}</code>
          {nameValid ? (
            <>
              {" "}
              as folder <code>{trimmedName}</code>
            </>
          ) : trimmedName ? (
            <span className="warn"> — name can’t contain / \ : or start with “.”</span>
          ) : (
            <>
              {" "}
              as folder <code>…</code>
            </>
          )}
        </p>
      )}

      {/* The ONE explainer this section gets. It used to carry three stacked
          paragraphs plus an essay about how the Remote dropdown is grouped. */}
      <p className="mount-note">
        Mount a specific <b>bucket/prefix</b> rather than a whole bucket — narrow mounts browse
        and search much faster. An entry marked <b>+</b> is set up as part of adding the mount.
      </p>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <ProviderPicker onPick={onPickProvider} />
    </section>
  );
}
