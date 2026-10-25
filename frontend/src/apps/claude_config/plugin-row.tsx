// The two kinds of plugin row. Both share one anatomy: the name over a muted
// one-line description, one muted "marketplace · version" string (the version
// only when it is one), then the controls — an overflow menu and the chevron, and at the far right the one
// thing the row is for (the enable switch on Installed, Install on Discover).
import type { AvailablePlugin, Plugin } from "./api";
import { Icon, KebabMenu, ListRow, Pill, Toggle3 } from "./bits";
import { PluginContentsPanel } from "./plugin-contents";

const SEMVER = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
const COMMIT = /^[0-9a-f]{7,40}$/i;

/** How a plugin's version reads. `row` is for the list line, `detail` for the
 *  panel. A git-sourced plugin's "version" is a commit hash: a hash is not a
 *  release, so the row stays quiet about it and the panel says what it is. */
export function versionLabel(version?: string | null): { row: string; detail: string } {
  const v = (version ?? "").trim();
  if (!v) return { row: "", detail: "" };
  if (SEMVER.test(v)) return { row: `v${v}`, detail: `v${v}` };
  if (COMMIT.test(v)) return { row: "", detail: `commit ${v.slice(0, 7)}` };
  return { row: "", detail: v };
}

/** The one muted string at a row's right edge: the marketplace, then the
 *  version when it is a release. */
export function metaText(marketplace: string, version?: string | null): string {
  return [marketplace, versionLabel(version).row].filter(Boolean).join(" · ");
}

function Details({
  id,
  version,
  shareCommand,
  onCopy,
  children,
}: {
  id: string;
  version?: string | null;
  shareCommand: string;
  onCopy: (command: string) => void;
  children?: React.ReactNode;
}) {
  const detail = versionLabel(version).detail;
  return (
    <>
      <dl className="cc-lrow-dl">
        <dt className="cc-lrow-dt">Plugin</dt>
        <dd className="cc-lrow-dd cc-mono">{id}</dd>
        {detail && (
          <>
            <dt className="cc-lrow-dt">Version</dt>
            <dd className="cc-lrow-dd">{detail}</dd>
          </>
        )}
      </dl>
      <p>
        <button type="button" className="btn" onClick={() => onCopy(shareCommand)}>
          <Icon name="copy" />
          Copy install command
        </button>
      </p>
      {children}
    </>
  );
}

export function InstalledPluginRow({
  plugin: p,
  enabled,
  busy,
  onToggle,
  onUpdate,
  onUninstall,
  onCopy,
}: {
  plugin: Plugin;
  enabled: boolean;
  busy: boolean;
  onToggle: (next: boolean) => void;
  onUpdate: () => void;
  onUninstall: () => void;
  onCopy: (command: string) => void;
}) {
  return (
    <ListRow
      stacked
      name={p.name}
      pills={!p.installed ? <Pill>not installed</Pill> : null}
      secondary={p.description}
      secondaryTitle={p.description}
      meta={<span className="cc-lrow-meta">{metaText(p.marketplace, p.version)}</span>}
      actions={
        <KebabMenu
          label={`Actions for ${p.name}`}
          items={[
            ...(p.installed
              ? [{ label: busy ? "Updating…" : "Update", disabled: busy, onClick: onUpdate }]
              : []),
            { label: "Copy install command", onClick: () => onCopy(p.shareCommand) },
            ...(p.installed
              ? [
                  "separator" as const,
                  { label: "Uninstall", danger: true, disabled: busy, onClick: onUninstall },
                ]
              : []),
          ]}
        />
      }
      // The chevron opens the id and what the plugin CONTAINS, read off disk
      // on expand. A plugin that is recorded but not installed has no files to
      // read, so it shows only the id.
      details={
        <Details id={p.id} version={p.version} shareCommand={p.shareCommand} onCopy={onCopy}>
          {p.installed && <PluginContentsPanel id={p.id} />}
        </Details>
      }
      end={<Toggle3 label={`Enable ${p.name}`} value={enabled} onChange={onToggle} />}
    />
  );
}

export function DiscoverPluginRow({
  plugin: p,
  busy,
  onInstall,
  onCopy,
}: {
  plugin: AvailablePlugin;
  busy: boolean;
  onInstall: () => void;
  onCopy: (command: string) => void;
}) {
  return (
    <ListRow
      stacked
      name={p.name}
      secondary={p.description}
      secondaryTitle={p.description}
      meta={<span className="cc-lrow-meta">{metaText(p.marketplace, p.version)}</span>}
      // A catalog description runs to a paragraph, and deciding whether to
      // install is exactly when it matters — so the row shows as much as fits
      // and the panel shows all of it, with who wrote it and what it is filed
      // under.
      details={
        <Details
          id={p.id}
          version={p.version}
          shareCommand={`claude plugin install ${p.id}`}
          onCopy={onCopy}
        >
          {p.description && <p>{p.description}</p>}
          {(p.author || p.category || p.keywords.length > 0) && (
            <dl className="cc-lrow-dl">
              {p.author && (
                <>
                  <dt className="cc-lrow-dt">Author</dt>
                  <dd className="cc-lrow-dd">{p.author}</dd>
                </>
              )}
              {p.category && (
                <>
                  <dt className="cc-lrow-dt">Category</dt>
                  <dd className="cc-lrow-dd">{p.category}</dd>
                </>
              )}
              {p.keywords.length > 0 && (
                <>
                  <dt className="cc-lrow-dt">Keywords</dt>
                  <dd className="cc-lrow-dd">{p.keywords.join(", ")}</dd>
                </>
              )}
            </dl>
          )}
        </Details>
      }
      end={
        p.installed ? (
          <span className="cc-lrow-meta">Installed</span>
        ) : (
          <button
            type="button"
            className="cc-btn-sm"
            disabled={busy}
            title={`claude plugin install ${p.id}`}
            onClick={onInstall}
          >
            {busy ? "Installing…" : "Install"}
          </button>
        )
      }
    />
  );
}
