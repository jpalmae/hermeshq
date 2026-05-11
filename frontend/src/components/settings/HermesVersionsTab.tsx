import { useState, useEffect } from "react";
import { useI18n } from "../../lib/i18n";
import {
  useUpstreamHermesVersions,
  useCreateHermesVersionFromUpstream,
  useCreateHermesVersion,
  useUpdateHermesVersion,
  useInstallHermesVersion,
  useUninstallHermesVersion,
  useDeleteHermesVersionCatalogEntry,
} from "../../api/hermesVersions";
import type { HermesVersion } from "../../types/api";

interface HermesVersionsTabProps {
  hermesVersions: HermesVersion[] | undefined;
}

export default function HermesVersionsTab({ hermesVersions }: HermesVersionsTabProps) {
  const { t } = useI18n();

  const [newHermesVersion, setNewHermesVersion] = useState("");
  const [newHermesReleaseTag, setNewHermesReleaseTag] = useState("");
  const [newHermesDescription, setNewHermesDescription] = useState("");
  const [hermesVersionDrafts, setHermesVersionDrafts] = useState<
    Record<string, { release_tag: string; description: string }>
  >({});
  const [upstreamRefreshToken, setUpstreamRefreshToken] = useState(0);

  /* ── hooks (self-contained, no longer depend on parent state) ── */
  const { data: upstreamHermesVersions, isFetching: upstreamHermesVersionsLoading } =
    useUpstreamHermesVersions(true, upstreamRefreshToken);

  const createHermesVersion = useCreateHermesVersion();
  const updateHermesVersion = useUpdateHermesVersion();
  const installHermesVersion = useInstallHermesVersion();
  const uninstallHermesVersion = useUninstallHermesVersion();
  const deleteHermesVersionCatalogEntry = useDeleteHermesVersionCatalogEntry();
  const createHermesVersionFromUpstream = useCreateHermesVersionFromUpstream();

  useEffect(() => {
    setHermesVersionDrafts(
      Object.fromEntries(
        (hermesVersions ?? []).map((version) => [
          version.version,
          {
            release_tag: version.release_tag ?? "",
            description: version.description ?? "",
          },
        ]),
      ),
    );
  }, [hermesVersions]);

  async function submitHermesVersionCatalog(event: React.FormEvent) {
    event.preventDefault();
    await createHermesVersion.mutateAsync({
      version: newHermesVersion.trim(),
      release_tag: newHermesReleaseTag.trim() || null,
      description: newHermesDescription.trim() || null,
    });
    setNewHermesVersion("");
    setNewHermesReleaseTag("");
    setNewHermesDescription("");
  }

  async function saveHermesVersion(version: string) {
    const draft = hermesVersionDrafts[version];
    if (!draft) {
      return;
    }
    await updateHermesVersion.mutateAsync({
      version,
      payload: {
        release_tag: draft.release_tag.trim() || null,
        description: draft.description.trim() || null,
      },
    });
  }

  async function addUpstreamHermesVersion(releaseTag: string) {
    await createHermesVersionFromUpstream.mutateAsync({
      release_tag: releaseTag,
      description: null,
    });
  }

  return (
    <section className="grid gap-6">
      <section className="panel-frame p-6">
        <p className="panel-label">Hermes Agent</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Versions</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Install Hermes Agent releases once per instance and pin agents to a tested version.
        </p>
        <section className="mt-6 border border-[var(--border)] bg-[var(--surface-raised)] p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="panel-label">Upstream releases</p>
              <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                HermesHQ now queries the Hermes Agent repository directly so you can add real tags instead of typing guessed values.
              </p>
            </div>
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => setUpstreamRefreshToken((current) => current + 1)}
              disabled={upstreamHermesVersionsLoading}
            >
              {upstreamHermesVersionsLoading ? "Refreshing..." : "Refresh upstream tags"}
            </button>
          </div>
          <div className="mt-4 space-y-3">
            {(upstreamHermesVersions ?? []).slice(0, 12).map((release) => (
              <article key={release.release_tag} className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <p className="panel-label">tag</p>
                    <h3 className="mt-2 text-base text-[var(--text-display)]">{release.release_tag}</h3>
                    <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                      {release.detected_version ? `Detected package version ${release.detected_version}` : "Package version could not be derived yet."}
                    </p>
                    <p className="mt-2 font-mono text-xs text-[var(--text-disabled)]">{release.commit_sha?.slice(0, 12)}</p>
                    {release.catalog_versions?.length ? (
                      <p className="mt-2 text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">
                        In catalog as {release.catalog_versions.join(", ")}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex flex-col items-end gap-2">
                    <p className="panel-label">{release.already_in_catalog ? "cataloged" : "upstream only"}</p>
                    <button
                      type="button"
                      className="panel-button-secondary"
                      disabled={createHermesVersionFromUpstream.isPending || release.already_in_catalog}
                      onClick={() => void addUpstreamHermesVersion(release.release_tag)}
                    >
                      {createHermesVersionFromUpstream.isPending ? "Adding..." : "Add to catalog"}
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>
        <form className="mt-6 border border-[var(--border)] bg-[var(--surface-raised)] p-4" onSubmit={submitHermesVersionCatalog}>
          <p className="panel-label">Manual catalog entry</p>
          <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
            Keep this only for advanced cases. HermesHQ now validates the release tag against upstream before saving.
          </p>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <label className="panel-field">
              <span className="panel-label">Version label</span>
              <input value={newHermesVersion} onChange={(event) => setNewHermesVersion(event.target.value)} placeholder="0.11.0-canary" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Release tag</span>
              <input value={newHermesReleaseTag} onChange={(event) => setNewHermesReleaseTag(event.target.value)} placeholder="v2026.4.23" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Description</span>
              <input value={newHermesDescription} onChange={(event) => setNewHermesDescription(event.target.value)} placeholder="Manual alias for a validated upstream release" />
            </label>
          </div>
          <div className="mt-4 flex items-center gap-3">
            <button className="panel-button-primary" type="submit" disabled={createHermesVersion.isPending}>
              {createHermesVersion.isPending ? "Adding..." : "Add manual entry"}
            </button>
            <p className="panel-inline-status">Manual entries now fail early if the release tag does not exist in upstream.</p>
          </div>
        </form>
        <div className="mt-6 space-y-4">
          {(hermesVersions ?? []).map((version) => (
            <article key={version.version} className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="panel-label">{version.source}</p>
                  <h3 className="mt-2 text-lg text-[var(--text-display)]">
                    {version.version === "bundled" ? "Bundled runtime" : version.version}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                    {version.description ?? "Hermes Agent runtime"}
                  </p>
                  <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {version.release_tag ?? (version.detected_version ? `detected ${version.detected_version}` : "no release tag")}
                  </p>
                  {version.detected_version_warning ? (
                    <p className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
                      {version.detected_version_warning}
                    </p>
                  ) : null}
                </div>
                <div className="text-right">
                  <p className="panel-label">{version.installed ? "installed" : "available"}</p>
                  {version.is_default ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--accent)]">default</p>
                  ) : null}
                  {version.in_use_by_agents ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      pinned by {version.in_use_by_agents}
                    </p>
                  ) : null}
                </div>
              </div>
              {version.version !== "bundled" ? (
                <div className="mt-4 grid gap-4 md:grid-cols-2">
                  <label className="panel-field">
                    <span className="panel-label">Release tag</span>
                    <input
                      value={hermesVersionDrafts[version.version]?.release_tag ?? ""}
                      onChange={(event) =>
                        setHermesVersionDrafts((current) => ({
                          ...current,
                          [version.version]: {
                            ...(current[version.version] ?? { release_tag: "", description: "" }),
                            release_tag: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>
                  <label className="panel-field">
                    <span className="panel-label">Description</span>
                    <input
                      value={hermesVersionDrafts[version.version]?.description ?? ""}
                      onChange={(event) =>
                        setHermesVersionDrafts((current) => ({
                          ...current,
                          [version.version]: {
                            ...(current[version.version] ?? { release_tag: "", description: "" }),
                            description: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>
                </div>
              ) : null}
              <div className="mt-4 flex flex-wrap gap-3">
                {version.version !== "bundled" ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={updateHermesVersion.isPending}
                    onClick={() => void saveHermesVersion(version.version)}
                  >
                    {updateHermesVersion.isPending ? "Saving..." : "Save metadata"}
                  </button>
                ) : null}
                {version.version !== "bundled" && !version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={installHermesVersion.isPending}
                    onClick={() => void installHermesVersion.mutateAsync(version.version)}
                  >
                    {installHermesVersion.isPending ? "Installing..." : "Install"}
                  </button>
                ) : null}
                {version.version !== "bundled" && version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={uninstallHermesVersion.isPending || version.is_default || version.in_use_by_agents > 0}
                    onClick={() => void uninstallHermesVersion.mutateAsync(version.version)}
                  >
                    {uninstallHermesVersion.isPending ? "Removing..." : "Uninstall"}
                  </button>
                ) : null}
                {version.version !== "bundled" && !version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={deleteHermesVersionCatalogEntry.isPending || version.is_default || version.in_use_by_agents > 0}
                    onClick={() => void deleteHermesVersionCatalogEntry.mutateAsync(version.version)}
                  >
                    {deleteHermesVersionCatalogEntry.isPending ? "Deleting..." : "Delete catalog entry"}
                  </button>
                ) : null}
              </div>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}
