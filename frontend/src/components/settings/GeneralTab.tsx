import { useState, useEffect, type FormEvent, useMemo } from "react";
import { useI18n } from "../../lib/i18n";
import { resolveAssetUrl } from "../../api/settings";

interface InstanceBackupSummary {
  schema_version: string;
  app_version: string;
  source_hostname: string;
  counts: Record<string, number>;
  warnings: string[];
}

interface GeneralTabProps {
  settings: any;
  updateSettings: any;
  uploadLogo: any;
  uploadFavicon: any;
  deleteLogo: any;
  deleteFavicon: any;
  uploadTuiSkin: any;
  deleteTuiSkin: any;
  createInstanceBackup: any;
  validateInstanceBackup: any;
  restoreInstanceBackup: any;
  restoreJob: any;
  queryClient: any;
}

export default function GeneralTab({
  settings,
  updateSettings,
  uploadLogo,
  uploadFavicon,
  deleteLogo,
  deleteFavicon,
  uploadTuiSkin,
  deleteTuiSkin,
  createInstanceBackup,
  validateInstanceBackup,
  restoreInstanceBackup,
  restoreJob,
  queryClient,
}: GeneralTabProps) {
  const { t } = useI18n();

  /* ───── branding state ───── */
  const [appName, setAppName] = useState("");
  const [appShortName, setAppShortName] = useState("");
  const [themeMode, setThemeMode] = useState<"dark" | "light" | "system" | "enterprise" | "sixmanager" | "sixmanager-light">("dark");
  const [defaultLocale, setDefaultLocale] = useState<"en" | "es">("en");

  /* ───── backup state ───── */
  const [backupPassphrase, setBackupPassphrase] = useState("");
  const [backupIncludeActivityLogs, setBackupIncludeActivityLogs] = useState(false);
  const [backupIncludeTaskHistory, setBackupIncludeTaskHistory] = useState(false);
  const [backupIncludeTerminalSessions, setBackupIncludeTerminalSessions] = useState(false);
  const [backupIncludeMessagingSessions, setBackupIncludeMessagingSessions] = useState(false);
  const [backupImportFile, setBackupImportFile] = useState<File | null>(null);
  const [backupImportPassphrase, setBackupImportPassphrase] = useState("");
  const [backupRestoreMode, setBackupRestoreMode] = useState<"replace" | "merge">("replace");
  const [activeRestoreJobId, setActiveRestoreJobId] = useState<string | null>(null);
  const [lastBackupFilename, setLastBackupFilename] = useState<string | null>(null);
  const [lastBackupDownloadUrl, setLastBackupDownloadUrl] = useState<string | null>(null);

  /* ───── derived ───── */
  const logoUrl = useMemo(() => resolveAssetUrl(settings?.logo_url), [settings?.logo_url]);
  const faviconUrl = useMemo(() => resolveAssetUrl(settings?.favicon_url), [settings?.favicon_url]);

  /* ───── sync from settings ───── */
  useEffect(() => {
    setAppName(settings?.app_name ?? "");
    setAppShortName(settings?.app_short_name ?? "");
    setThemeMode(settings?.theme_mode ?? "dark");
    setDefaultLocale(settings?.default_locale ?? "en");
  }, [settings]);

  /* ───── cleanup download URL ───── */
  useEffect(() => {
    return () => {
      if (lastBackupDownloadUrl) {
        window.URL.revokeObjectURL(lastBackupDownloadUrl);
      }
    };
  }, [lastBackupDownloadUrl]);

  /* ───── invalidate on restore success ───── */
  useEffect(() => {
    if (restoreJob?.status !== "succeeded") return;
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["settings"] }),
      queryClient.invalidateQueries({ queryKey: ["branding", "public"] }),
      queryClient.invalidateQueries({ queryKey: ["agents"] }),
      queryClient.invalidateQueries({ queryKey: ["users"] }),
      queryClient.invalidateQueries({ queryKey: ["secrets"] }),
      queryClient.invalidateQueries({ queryKey: ["providers"] }),
      queryClient.invalidateQueries({ queryKey: ["hermes-versions"] }),
      queryClient.invalidateQueries({ queryKey: ["integration-packages"] }),
      queryClient.invalidateQueries({ queryKey: ["integration-drafts"] }),
      queryClient.invalidateQueries({ queryKey: ["templates"] }),
    ]);
  }, [queryClient, restoreJob?.id, restoreJob?.status]);

  /* ───── handlers ───── */
  async function submitBranding(event: FormEvent) {
    event.preventDefault();
    await updateSettings.mutateAsync({
      app_name: appName || null,
      app_short_name: appShortName || null,
      theme_mode: themeMode,
      default_locale: defaultLocale,
    });
  }

  async function onLogoSelected(file: File | null) {
    if (!file) return;
    try { await uploadLogo.mutateAsync(file); }
    catch (error) { window.alert(error instanceof Error ? error.message : "Logo upload failed"); }
  }

  async function onFaviconSelected(file: File | null) {
    if (!file) return;
    try { await uploadFavicon.mutateAsync(file); }
    catch (error) { window.alert(error instanceof Error ? error.message : "Favicon upload failed"); }
  }

  async function onTuiSkinSelected(file: File | null) {
    if (!file) return;
    try { await uploadTuiSkin.mutateAsync(file); }
    catch (error) { window.alert(error instanceof Error ? error.message : "TUI skin upload failed"); }
  }

  async function runBackupCreate() {
    try {
      const result = await createInstanceBackup.mutateAsync({
        passphrase: backupPassphrase,
        include_activity_logs: backupIncludeActivityLogs,
        include_task_history: backupIncludeTaskHistory,
        include_terminal_sessions: backupIncludeTerminalSessions,
        include_messaging_sessions: backupIncludeMessagingSessions,
      });
      if (lastBackupDownloadUrl) window.URL.revokeObjectURL(lastBackupDownloadUrl);
      setLastBackupFilename(result.filename);
      setLastBackupDownloadUrl(window.URL.createObjectURL(result.blob));
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Backup creation failed");
    }
  }

  async function runBackupValidation() {
    if (!backupImportFile) { window.alert("Select a backup file first."); return; }
    await validateInstanceBackup.mutateAsync({ file: backupImportFile, passphrase: backupImportPassphrase || undefined });
  }

  async function runBackupRestore() {
    if (!backupImportFile) { window.alert("Select a backup file first."); return; }
    const confirmed = window.confirm(
      backupRestoreMode === "replace"
        ? "Replace the current instance state with this backup?"
        : "Merge this backup into the current instance state?",
    );
    if (!confirmed) return;
    const job = await restoreInstanceBackup.mutateAsync({
      file: backupImportFile, passphrase: backupImportPassphrase, mode: backupRestoreMode,
    });
    setActiveRestoreJobId(job.id);
  }

  function renderBackupSummary(summary: InstanceBackupSummary | null | undefined) {
    if (!summary) return null;
    const countEntries = Object.entries(summary.counts ?? {}).sort((a, b) => a[0].localeCompare(b[0]));
    return (
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)]/50 p-4">
        <div className="flex flex-wrap items-center gap-3 text-xs uppercase tracking-[0.22em] text-[var(--text-secondary)]">
          <span>Schema {summary.schema_version}</span>
          <span>App {summary.app_version}</span>
          <span>{summary.source_hostname}</span>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {countEntries.map(([key, value]) => (
            <div key={key} className="rounded-2xl border border-[var(--border)] bg-[var(--surface-panel)] px-4 py-3">
              <p className="panel-label">{key.replaceAll("_", " ")}</p>
              <p className="mt-1 text-lg text-[var(--text-display)]">{value}</p>
            </div>
          ))}
        </div>
        {summary.warnings.length ? (
          <div className="mt-4 space-y-2 text-sm text-[var(--warning)]">
            {summary.warnings.map((warning) => <p key={warning}>{warning}</p>)}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <>
    {/* ── Branding ── */}
    <section className="grid gap-6 xl:grid-cols-2">
      <form className="panel-frame p-6" onSubmit={submitBranding}>
        <p className="panel-label">{t("settings.branding")}</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.instanceIdentity")}</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">{t("settings.identityCopy")}</p>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">{t("settings.appName")}</span>
            <input value={appName} onChange={(e) => setAppName(e.target.value)} placeholder="HermesHQ" />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("settings.shortName")}</span>
            <input value={appShortName} onChange={(e) => setAppShortName(e.target.value)} placeholder="HQ" />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("settings.theme")}</span>
            <select value={themeMode} onChange={(e) => setThemeMode(e.target.value as typeof themeMode)}>
              <option value="dark">{t("common.dark")}</option>
              <option value="light">{t("common.light")}</option>
              <option value="system">{t("common.system")}</option>
              <option value="enterprise">{t("common.enterprise")}</option>
              <option value="sixmanager">{t("common.sixmanager")}</option>
              <option value="sixmanager-light">{t("common.sixmanagerLight")}</option>
            </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("settings.language")}</span>
            <select value={defaultLocale} onChange={(e) => setDefaultLocale(e.target.value as "en" | "es")}>
              <option value="en">{t("common.english")}</option>
              <option value="es">{t("common.spanish")}</option>
            </select>
          </label>
          <button className="panel-button-primary w-full" type="submit">{t("settings.saveBranding")}</button>
        </div>
      </form>

      {/* ── Assets ── */}
      <section className="panel-frame p-6">
        <p className="panel-label">{t("settings.activeBranding")}</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.assets")}</h2>
        <div className="mt-6 space-y-5">
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">{t("settings.logo")}</p>
            <div className="mt-3 flex items-center gap-3">
              {logoUrl ? <img src={logoUrl} alt={settings?.app_name ?? "Logo"} className="h-12 w-auto max-w-[8rem] object-contain" /> : <p className="text-sm text-[var(--text-secondary)]">{t("settings.noLogo")}</p>}
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <label className="panel-button-secondary cursor-pointer">Upload PNG<input className="hidden" type="file" accept="image/png" onChange={(e) => void onLogoSelected(e.target.files?.[0] ?? null)} /></label>
              <button type="button" className="panel-button-secondary" onClick={async () => { try { await deleteLogo.mutateAsync(); } catch (error) { window.alert(error instanceof Error ? error.message : "Logo removal failed"); } }} disabled={!settings?.has_logo}>{t("settings.removeLogo")}</button>
            </div>
          </div>
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">{t("settings.favicon")}</p>
            <div className="mt-3 flex items-center gap-3">
              {faviconUrl ? <img src={faviconUrl} alt="Favicon" className="h-10 w-10 rounded border border-[var(--border)] object-contain" /> : <p className="text-sm text-[var(--text-secondary)]">{t("settings.noFavicon")}</p>}
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <label className="panel-button-secondary cursor-pointer">Upload PNG/ICO<input className="hidden" type="file" accept="image/png,.ico,image/x-icon,image/vnd.microsoft.icon" onChange={(e) => void onFaviconSelected(e.target.files?.[0] ?? null)} /></label>
              <button type="button" className="panel-button-secondary" onClick={async () => { try { await deleteFavicon.mutateAsync(); } catch (error) { window.alert(error instanceof Error ? error.message : "Favicon removal failed"); } }} disabled={!settings?.has_favicon}>{t("settings.removeFavicon")}</button>
            </div>
          </div>
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">{t("settings.tuiSkin")}</p>
            <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{t("settings.tuiSkinCopy")}</p>
            <div className="mt-3 flex items-center gap-3">
              {settings?.has_tui_skin ? <div><p className="text-sm text-[var(--text-display)]">{String(settings?.default_tui_skin ?? "unset")}</p><p className="mt-1 text-xs uppercase tracking-[0.24em] text-[var(--text-secondary)]">{String(settings?.tui_skin_filename ?? "")}</p></div> : <p className="text-sm text-[var(--text-secondary)]">{t("settings.noTuiSkin")}</p>}
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <label className="panel-button-secondary cursor-pointer">{t("settings.uploadTuiSkin")}<input className="hidden" type="file" accept=".yaml,.yml,text/yaml,application/yaml" onChange={(e) => void onTuiSkinSelected(e.target.files?.[0] ?? null)} /></label>
              <button type="button" className="panel-button-secondary" onClick={async () => { try { await deleteTuiSkin.mutateAsync(); } catch (error) { window.alert(error instanceof Error ? error.message : "TUI skin removal failed"); } }} disabled={!settings?.has_tui_skin}>{t("settings.removeTuiSkin")}</button>
            </div>
          </div>
          <div className="border-b border-[var(--border)] pb-3"><p className="panel-label">Current app name</p><p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.app_name ?? "HermesHQ")}</p></div>
          <div className="pb-3"><p className="panel-label">Current short name</p><p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.app_short_name ?? settings?.app_name ?? "HermesHQ")}</p></div>
          <div className="pb-3"><p className="panel-label">Current default theme</p><p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.theme_mode ?? "dark")}</p></div>
          <div className="pb-3"><p className="panel-label">{t("settings.currentTuiSkin")}</p><p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.default_tui_skin ?? t("settings.hermesDefaultSkin"))}</p></div>
        </div>
      </section>
    </section>

    {/* ── Backup / Restore ── */}
    <section className="mt-6 grid gap-6 xl:grid-cols-2">
      <section className="panel-frame p-6">
        <p className="panel-label">Backup</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Create instance backup</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Export the operational state of this HermesHQ instance into a portable archive. Secrets are encrypted with the passphrase you provide here.
        </p>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">Backup passphrase</span>
            <input type="password" value={backupPassphrase} onChange={(e) => setBackupPassphrase(e.target.value)} placeholder="Required to encrypt secrets" />
          </label>
          <label className="panel-field">
            <span className="panel-label">Optional history</span>
            <div className="backup-option-list">
              <label className="backup-option"><input type="checkbox" checked={backupIncludeActivityLogs} onChange={(e) => setBackupIncludeActivityLogs(e.target.checked)} /><span className="backup-option-copy">Include activity logs</span></label>
              <label className="backup-option"><input type="checkbox" checked={backupIncludeTaskHistory} onChange={(e) => setBackupIncludeTaskHistory(e.target.checked)} /><span className="backup-option-copy">Include task, conversation and inter-agent message history</span></label>
              <label className="backup-option"><input type="checkbox" checked={backupIncludeTerminalSessions} onChange={(e) => setBackupIncludeTerminalSessions(e.target.checked)} /><span className="backup-option-copy">Include terminal transcripts</span></label>
              <label className="backup-option"><input type="checkbox" checked={backupIncludeMessagingSessions} onChange={(e) => setBackupIncludeMessagingSessions(e.target.checked)} /><span className="backup-option-copy">Include messaging session stores</span></label>
            </div>
          </label>
          <button type="button" className="panel-button-primary w-full" onClick={() => void runBackupCreate()} disabled={createInstanceBackup.isPending || backupPassphrase.trim().length < 8}>
            {createInstanceBackup.isPending ? "Creating backup..." : "Create backup"}
          </button>
          <p className="text-xs leading-6 text-[var(--text-secondary)]">
            The archive includes app settings, providers, users, secrets, agents, messaging channels, schedules, templates, integration drafts, branding assets, user and agent assets, uploaded integration packages, and agent workspaces.
          </p>
          {lastBackupFilename ? (
            <div className="space-y-2 text-xs leading-6 text-[var(--success)]">
              <p>Backup prepared as <strong>{lastBackupFilename}</strong>.</p>
              {lastBackupDownloadUrl ? <a className="inline-flex items-center text-[var(--accent)] underline underline-offset-4" href={lastBackupDownloadUrl} download={lastBackupFilename}>Download again</a> : null}
            </div>
          ) : null}
        </div>
      </section>

      <section className="panel-frame p-6">
        <p className="panel-label">Restore</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Validate or restore backup</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Validate a backup archive before restoring it. Use <strong>replace</strong> for disaster recovery into a fresh instance, or <strong>merge</strong> to upsert data into the current instance.
        </p>
        <div className="mt-6 space-y-4">
          <label className="panel-field"><span className="panel-label">Backup archive</span><input type="file" accept=".zip,application/zip" onChange={(e) => setBackupImportFile(e.target.files?.[0] ?? null)} /></label>
          <label className="panel-field"><span className="panel-label">Passphrase</span><input type="password" value={backupImportPassphrase} onChange={(e) => setBackupImportPassphrase(e.target.value)} placeholder="Required to decrypt secrets" /></label>
          <label className="panel-field"><span className="panel-label">Restore mode</span>
            <select value={backupRestoreMode} onChange={(e) => setBackupRestoreMode(e.target.value as "replace" | "merge")}>
              <option value="replace">Replace current instance</option>
              <option value="merge">Merge into current instance</option>
            </select>
          </label>
          <div className="flex flex-wrap gap-3">
            <button type="button" className="panel-button-secondary" onClick={() => void runBackupValidation()} disabled={validateInstanceBackup.isPending || !backupImportFile}>
              {validateInstanceBackup.isPending ? "Validating..." : "Validate backup"}
            </button>
            <button type="button" className="panel-button-primary" onClick={() => void runBackupRestore()} disabled={restoreInstanceBackup.isPending || restoreJob?.status === "queued" || restoreJob?.status === "running" || !backupImportFile || backupImportPassphrase.trim().length < 8}>
              {restoreInstanceBackup.isPending || restoreJob?.status === "queued" || restoreJob?.status === "running" ? "Restoring..." : "Restore backup"}
            </button>
          </div>
          {validateInstanceBackup.data ? (
            <div className="space-y-4">
              <div className={`rounded-2xl border px-4 py-3 text-sm ${validateInstanceBackup.data.valid ? "border-[var(--success)]/30 text-[var(--text-display)]" : "border-[var(--danger)]/30 text-[var(--danger)]"}`}>
                {validateInstanceBackup.data.valid ? `Backup ${validateInstanceBackup.data.filename} is valid.` : `Backup ${validateInstanceBackup.data.filename} failed validation.`}
              </div>
              {validateInstanceBackup.data.errors.length ? <div className="space-y-2 text-sm text-[var(--danger)]">{validateInstanceBackup.data.errors.map((error: string) => <p key={error}>{error}</p>)}</div> : null}
              {renderBackupSummary(validateInstanceBackup.data.summary)}
            </div>
          ) : null}
          {restoreJob ? (
            <div className="space-y-4">
              <div className={`rounded-2xl border px-4 py-3 text-sm ${restoreJob.status === "failed" ? "border-[var(--danger)]/30 text-[var(--danger)]" : restoreJob.status === "succeeded" ? "border-[var(--success)]/30 text-[var(--text-display)]" : "border-[var(--accent)]/30 text-[var(--text-display)]"}`}>
                {restoreJob.status === "queued" ? `Restore job queued (${restoreJob.mode}).` : restoreJob.status === "running" ? `Restore running (${restoreJob.mode}).` : restoreJob.status === "succeeded" ? `Restore completed in ${restoreJob.mode} mode.` : `Restore failed in ${restoreJob.mode} mode.`}
              </div>
              <div className="space-y-1 text-sm text-[var(--text-secondary)]">
                <p><strong>Status:</strong> {restoreJob.status}</p>
                {restoreJob.current_step ? <p><strong>Current step:</strong> {restoreJob.current_step}</p> : null}
                {restoreJob.error ? <p className="text-[var(--danger)]"><strong>Error:</strong> {restoreJob.error}</p> : null}
              </div>
              {renderBackupSummary(restoreJob.summary)}
              {restoreJob.warnings.length ? <div className="space-y-2 text-sm text-[var(--warning)]">{restoreJob.warnings.map((warning: string) => <p key={warning}>{warning}</p>)}</div> : null}
            </div>
          ) : null}
        </div>
      </section>
    </section>
    </>
  );
}
