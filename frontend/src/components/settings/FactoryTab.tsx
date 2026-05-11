import { FormEvent, useEffect, useMemo, useState } from "react";

import { useIntegrationDraftFile } from "../../api/integrationFactory";
import { useI18n } from "../../lib/i18n";

interface FactoryTabProps {
  integrationDrafts: any[] | undefined;
  createIntegrationDraft: any;
  updateIntegrationDraft: any;
  deleteIntegrationDraft: any;
  saveIntegrationDraftFile: any;
  deleteIntegrationDraftFile: any;
  validateIntegrationDraft: any;
  publishIntegrationDraft: any;
  integrationDraftFiles: any;
  queryClient: any;
}

export default function FactoryTab({
  integrationDrafts,
  createIntegrationDraft,
  updateIntegrationDraft,
  deleteIntegrationDraft,
  saveIntegrationDraftFile,
  deleteIntegrationDraftFile,
  validateIntegrationDraft,
  publishIntegrationDraft,
  integrationDraftFiles,
  queryClient,
}: FactoryTabProps) {
  const { t } = useI18n();

  const [draftSlug, setDraftSlug] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftTemplate, setDraftTemplate] = useState<"rest-api" | "empty">("rest-api");
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const [selectedDraftPath, setSelectedDraftPath] = useState<string | null>(null);
  const [draftMetaName, setDraftMetaName] = useState("");
  const [draftMetaDescription, setDraftMetaDescription] = useState("");
  const [draftMetaVersion, setDraftMetaVersion] = useState("0.1.0");
  const [draftNotes, setDraftNotes] = useState("");
  const [draftEditorContent, setDraftEditorContent] = useState("");
  const [newDraftFilePath, setNewDraftFilePath] = useState("");

  const selectedDraft = useMemo(
    () => (integrationDrafts ?? []).find((draft) => draft.id === selectedDraftId) ?? null,
    [integrationDrafts, selectedDraftId],
  );

  const { data: selectedDraftFile } = useIntegrationDraftFile(
    selectedDraftId,
    selectedDraftPath,
    Boolean(selectedDraftId),
  );

  useEffect(() => {
    if (!selectedDraft) {
      setDraftMetaName("");
      setDraftMetaDescription("");
      setDraftMetaVersion("0.1.0");
      setDraftNotes("");
      return;
    }
    setDraftMetaName(selectedDraft.name);
    setDraftMetaDescription(selectedDraft.description);
    setDraftMetaVersion(selectedDraft.version);
    setDraftNotes(selectedDraft.notes ?? "");
    if (!selectedDraftPath || !selectedDraft.files.some((file) => file.path === selectedDraftPath)) {
      setSelectedDraftPath(selectedDraft.files[0]?.path ?? null);
    }
  }, [selectedDraft, selectedDraftPath]);

  useEffect(() => {
    if (selectedDraftFile) {
      setDraftEditorContent(selectedDraftFile.content);
    } else if (!selectedDraftPath) {
      setDraftEditorContent("");
    }
  }, [selectedDraftFile, selectedDraftPath]);

  async function submitIntegrationDraft(event: FormEvent) {
    event.preventDefault();
    const created = await createIntegrationDraft.mutateAsync({
      slug: draftSlug,
      name: draftName,
      description: draftDescription,
      template: draftTemplate,
      version: "0.1.0",
    });
    setDraftSlug("");
    setDraftName("");
    setDraftDescription("");
    setDraftTemplate("rest-api");
    setSelectedDraftId(created.id);
    setSelectedDraftPath(created.files[0]?.path ?? null);
  }

  async function saveDraftMetadata() {
    if (!selectedDraftId) {
      return;
    }
    await updateIntegrationDraft.mutateAsync({
      draftId: selectedDraftId,
      name: draftMetaName,
      description: draftMetaDescription,
      version: draftMetaVersion,
      notes: draftNotes,
    });
  }

  async function saveDraftFile(path: string) {
    if (!selectedDraftId) {
      return;
    }
    await saveIntegrationDraftFile.mutateAsync({
      draftId: selectedDraftId,
      path,
      content: draftEditorContent,
    });
  }

  async function createOrReplaceDraftFile() {
    if (!selectedDraftId || !newDraftFilePath.trim()) {
      return;
    }
    await saveIntegrationDraftFile.mutateAsync({
      draftId: selectedDraftId,
      path: newDraftFilePath.trim(),
      content: "",
    });
    setSelectedDraftPath(newDraftFilePath.trim());
    setNewDraftFilePath("");
  }

  async function removeSelectedDraftFile() {
    if (!selectedDraftId || !selectedDraftPath) {
      return;
    }
    await deleteIntegrationDraftFile.mutateAsync({ draftId: selectedDraftId, path: selectedDraftPath });
    setSelectedDraftPath(null);
  }

  async function runDraftValidation() {
    if (!selectedDraftId) {
      return;
    }
    await validateIntegrationDraft.mutateAsync(selectedDraftId);
  }

  async function publishSelectedDraft() {
    if (!selectedDraftId) {
      return;
    }
    await publishIntegrationDraft.mutateAsync(selectedDraftId);
  }

  async function removeSelectedDraft() {
    if (!selectedDraftId) {
      return;
    }
    await deleteIntegrationDraft.mutateAsync(selectedDraftId);
    setSelectedDraftId(null);
    setSelectedDraftPath(null);
  }

  return (
    <section className="grid gap-6 xl:grid-cols-[0.68fr_1.32fr]">
      <div className="grid gap-6">
        <form className="panel-frame p-6" onSubmit={submitIntegrationDraft}>
          <p className="panel-label">{t("settings.integrationFactory")}</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.createIntegrationDraft")}</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {t("settings.integrationFactoryCreateCopy")}
          </p>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftSlug")}</span>
              <input value={draftSlug} onChange={(event) => setDraftSlug(event.target.value)} placeholder="customer-api" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftName")}</span>
              <input value={draftName} onChange={(event) => setDraftName(event.target.value)} placeholder="Customer API" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftDescription")}</span>
              <textarea rows={4} value={draftDescription} onChange={(event) => setDraftDescription(event.target.value)} />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftTemplate")}</span>
              <select value={draftTemplate} onChange={(event) => setDraftTemplate(event.target.value as "rest-api" | "empty")}>
                <option value="rest-api">{t("settings.integrationDraftTemplateRestApi")}</option>
                <option value="empty">{t("settings.integrationDraftTemplateEmpty")}</option>
              </select>
            </label>
            <button className="panel-button-primary w-full" type="submit" disabled={createIntegrationDraft.isPending}>
              {createIntegrationDraft.isPending ? t("common.loading") : t("settings.createIntegrationDraft")}
            </button>
          </div>
        </form>

        <div className="panel-frame p-6">
          <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">{t("settings.integrationDrafts")}</p>
              <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.integrationFactory")}</h2>
            </div>
            <p className="panel-label">{t("settings.integrationDraftCount", { count: integrationDrafts?.length ?? 0 })}</p>
          </div>
          <div className="mt-4 space-y-3">
            {(integrationDrafts ?? []).length ? (
              (integrationDrafts ?? []).map((draft) => (
                <button
                  key={draft.id}
                  type="button"
                  onClick={() => {
                    setSelectedDraftId(draft.id);
                    setSelectedDraftPath(draft.files[0]?.path ?? null);
                  }}
                  className={`w-full border p-4 text-left ${
                    draft.id === selectedDraftId
                      ? "rounded-2xl border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)]"
                      : "rounded-2xl border-[var(--border)] bg-[var(--surface-raised)]"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="panel-label">{draft.slug}</p>
                      <p className="mt-2 text-sm text-[var(--text-display)]">{draft.name}</p>
                    </div>
                    <span className="rounded-full border border-[var(--border)] px-3 py-1 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      {draft.status}
                    </span>
                  </div>
                  <p className="mt-2 text-sm text-[var(--text-secondary)]">{draft.description || t("settings.integrationDraftNoDescription")}</p>
                </button>
              ))
            ) : (
              <p className="panel-inline-status">{t("settings.integrationDraftsEmpty")}</p>
            )}
          </div>
        </div>
      </div>

      <div className="panel-frame p-6">
        {selectedDraft ? (
          <div className="grid gap-6">
            <div className="flex flex-wrap items-start justify-between gap-4 border-b border-[var(--border)] pb-4">
              <div>
                <p className="panel-label">{selectedDraft.slug}</p>
                <h2 className="mt-2 text-2xl text-[var(--text-display)]">{selectedDraft.name}</h2>
                <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                  {t("settings.integrationFactoryDetailCopy")}
                </p>
              </div>
              <div className="flex flex-wrap gap-3">
                <button type="button" className="panel-button-secondary" onClick={() => void runDraftValidation()} disabled={validateIntegrationDraft.isPending}>
                  {validateIntegrationDraft.isPending ? t("common.loading") : t("settings.validateIntegrationDraft")}
                </button>
                <button type="button" className="panel-button-primary" onClick={() => void publishSelectedDraft()} disabled={publishIntegrationDraft.isPending}>
                  {publishIntegrationDraft.isPending ? t("common.loading") : t("settings.publishIntegrationDraft")}
                </button>
                <button type="button" className="panel-button-secondary" onClick={() => void removeSelectedDraft()} disabled={deleteIntegrationDraft.isPending}>
                  {t("settings.deleteIntegrationDraft")}
                </button>
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[0.5fr_0.5fr]">
              <div className="space-y-4">
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftName")}</span>
                  <input value={draftMetaName} onChange={(event) => setDraftMetaName(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftDescription")}</span>
                  <textarea rows={4} value={draftMetaDescription} onChange={(event) => setDraftMetaDescription(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftVersion")}</span>
                  <input value={draftMetaVersion} onChange={(event) => setDraftMetaVersion(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftNotes")}</span>
                  <textarea rows={4} value={draftNotes} onChange={(event) => setDraftNotes(event.target.value)} />
                </label>
                <button type="button" className="panel-button-secondary w-full" onClick={() => void saveDraftMetadata()} disabled={updateIntegrationDraft.isPending}>
                  {updateIntegrationDraft.isPending ? t("common.loading") : t("settings.saveIntegrationDraftMeta")}
                </button>
              </div>
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <p className="panel-label">{t("settings.integrationDraftStatus")}</p>
                <div className="mt-4 space-y-2 text-sm text-[var(--text-secondary)]">
                  <p>{t("settings.integrationDraftTemplateLabel", { value: selectedDraft.template })}</p>
                  <p>{t("settings.integrationDraftPluginSlug", { value: selectedDraft.plugin_slug ?? t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftSkillIdentifier", { value: selectedDraft.skill_identifier ?? t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftProfiles", { value: selectedDraft.supported_profiles.join(", ") || t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftPublishedPackage", { value: selectedDraft.published_package_slug ?? t("agent.none") })}</p>
                </div>
                {selectedDraft.last_validation ? (
                  <div className="mt-4 border-t border-[var(--border)] pt-4">
                    <p className="panel-label">{t("settings.integrationDraftValidation")}</p>
                    <div className="mt-3 space-y-2">
                      {selectedDraft.last_validation.checks.map((check, index) => (
                        <div key={`${check.code}-${index}`} className="rounded-xl border border-[var(--border)] px-3 py-2 text-sm text-[var(--text-secondary)]">
                          <p className="font-mono text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{check.level} / {check.code}</p>
                          <p className="mt-1">{check.message}</p>
                          {check.path ? <p className="mt-1 font-mono text-xs text-[var(--text-disabled)]">{check.path}</p> : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[0.36fr_0.64fr]">
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="panel-label">{t("settings.integrationDraftFiles")}</p>
                  <p className="text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{selectedDraft.files.length}</p>
                </div>
                <div className="mt-4 space-y-2">
                  {selectedDraft.files.map((file) => (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => setSelectedDraftPath(file.path)}
                      className={`w-full rounded-xl border px-3 py-2 text-left text-sm ${
                        file.path === selectedDraftPath
                          ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--text-display)]"
                          : "border-[var(--border)] bg-[transparent] text-[var(--text-secondary)]"
                      }`}
                    >
                      <p className="font-mono">{file.path}</p>
                      <p className="mt-1 text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{file.size} bytes</p>
                    </button>
                  ))}
                </div>
                <div className="mt-4 border-t border-[var(--border)] pt-4">
                  <label className="panel-field">
                    <span className="panel-label">{t("settings.integrationDraftNewFile")}</span>
                    <input value={newDraftFilePath} onChange={(event) => setNewDraftFilePath(event.target.value)} placeholder="plugin/helpers.py" />
                  </label>
                  <button type="button" className="panel-button-secondary mt-3 w-full" onClick={() => void createOrReplaceDraftFile()} disabled={saveIntegrationDraftFile.isPending}>
                    {t("settings.integrationDraftCreateFile")}
                  </button>
                </div>
              </div>

              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="panel-label">{t("settings.integrationDraftEditor")}</p>
                    <p className="mt-1 font-mono text-xs text-[var(--text-disabled)]">{selectedDraftPath ?? t("settings.integrationDraftSelectFile")}</p>
                  </div>
                  <div className="flex gap-3">
                    <button type="button" className="panel-button-secondary" onClick={() => selectedDraftPath ? void saveDraftFile(selectedDraftPath) : undefined} disabled={!selectedDraftPath || saveIntegrationDraftFile.isPending}>
                      {saveIntegrationDraftFile.isPending ? t("common.loading") : t("settings.saveIntegrationDraftFile")}
                    </button>
                    <button type="button" className="panel-button-secondary" onClick={() => void removeSelectedDraftFile()} disabled={!selectedDraftPath || deleteIntegrationDraftFile.isPending}>
                      {t("settings.deleteIntegrationDraftFile")}
                    </button>
                  </div>
                </div>
                <textarea
                  className="mt-4 min-h-[32rem] font-mono text-sm"
                  value={draftEditorContent}
                  onChange={(event) => setDraftEditorContent(event.target.value)}
                  placeholder={t("settings.integrationDraftEditorPlaceholder")}
                  disabled={!selectedDraftPath}
                />
              </div>
            </div>
          </div>
        ) : (
          <p className="panel-inline-status">{t("settings.integrationDraftSelectPrompt")}</p>
        )}
      </div>
    </section>
  );
}
