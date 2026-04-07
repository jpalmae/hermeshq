import { useMemo, useState } from "react";

import { useUpdateAgent } from "../api/agents";
import { useAgentSkills, useDeleteInstalledSkill, useSkillCatalog } from "../api/skills";
import { useI18n } from "../lib/i18n";
import type { Agent } from "../types/api";

function dedupeSkills(skills: string[]) {
  return [...new Set(skills.map((skill) => skill.trim()).filter(Boolean))];
}

export function AgentSkillsPanel({ agent, embedded = false }: { agent: Agent; embedded?: boolean }) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const updateAgent = useUpdateAgent();
  const deleteInstalledSkill = useDeleteInstalledSkill();
  const { data: agentSkills, isLoading: isSkillsLoading } = useAgentSkills(agent.id);
  const { data: catalog, isFetching: isSearching } = useSkillCatalog(query, 10);

  const assigned = useMemo(() => dedupeSkills(agentSkills?.assigned ?? agent.skills ?? []), [agent.skills, agentSkills?.assigned]);
  const installed = agentSkills?.installed ?? [];

  async function saveSkills(nextSkills: string[]) {
    await updateAgent.mutateAsync({
      agentId: agent.id,
      payload: { skills: dedupeSkills(nextSkills) },
    });
  }

  async function addSkill(identifier: string) {
    if (assigned.includes(identifier)) {
      return;
    }
    await saveSkills([...assigned, identifier]);
  }

  async function removeSkill(identifier: string) {
    await saveSkills(assigned.filter((skill) => skill !== identifier));
  }

  async function deleteSkill(path: string) {
    await deleteInstalledSkill.mutateAsync({ agentId: agent.id, path });
  }

  return (
    <section className={embedded ? "" : "panel-frame p-6"}>
      {embedded ? null : (
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">{t("agent.skills")}</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("agent.skillRegistry")}</h3>
          </div>
          <p className="panel-label">{t("agent.installedCount", { count: installed.length })}</p>
        </div>
      )}

      <div className={`${embedded ? "mt-0" : "mt-5"} space-y-5`}>
        <div>
          <p className="panel-label">{t("agent.assignedToAgent")}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {assigned.length ? (
              assigned.map((identifier) => (
                <button
                  key={identifier}
                  type="button"
                  className="rounded-full border border-[var(--border-visible)] px-3 py-2 font-mono text-xs uppercase tracking-[0.08em] text-[var(--text-primary)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent)]"
                  onClick={() => removeSkill(identifier)}
                  disabled={updateAgent.isPending || deleteInstalledSkill.isPending}
                  title={t("agent.removeSkillTitle")}
                >
                  {identifier}
                </button>
              ))
            ) : (
              <p className="panel-inline-status">
                {t("agent.noSkillsAssigned")}
              </p>
            )}
          </div>
        </div>

        <div className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
          <div>
            <label className="panel-field">
              <span className="panel-label">{t("agent.searchCatalog")}</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("agent.searchCatalogPlaceholder")}
              />
            </label>
            <div className="mt-4 space-y-3">
              {query.trim().length === 0 ? (
                <p className="panel-inline-status">{t("agent.searchTypeHint")}</p>
              ) : isSearching ? (
                <p className="panel-inline-status">{t("agent.searchLoading")}</p>
              ) : catalog?.skills?.length ? (
                catalog.skills.map((skill) => (
                  <article key={skill.identifier} className="border border-[var(--border)] p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="text-sm text-[var(--text-display)]">{skill.name}</p>
                        <p className="mt-1 font-mono text-xs uppercase tracking-[0.08em] text-[var(--text-secondary)]">
                          {skill.identifier}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="panel-button-secondary !min-h-0 !px-4 !py-2"
                        onClick={() => addSkill(skill.identifier)}
                        disabled={updateAgent.isPending || deleteInstalledSkill.isPending || assigned.includes(skill.identifier)}
                      >
                        {assigned.includes(skill.identifier) ? t("agent.assigned") : t("agent.add")}
                      </button>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                      {skill.description || t("agent.noCatalogDescription")}
                    </p>
                  </article>
                ))
              ) : (
                <p className="panel-inline-status">{t("agent.emptyCatalog")}</p>
              )}
            </div>
          </div>

          <div>
            <p className="panel-label">{t("agent.installedInHome")}</p>
            <div className="mt-4 space-y-3">
              {isSkillsLoading ? (
                <p className="panel-inline-status">{t("agent.readingInstallation")}</p>
              ) : installed.length ? (
                installed.map((skill) => (
                  <article key={`${skill.path ?? skill.name}-${skill.name}`} className="border border-[var(--border)] p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="text-sm text-[var(--text-display)]">{skill.name}</p>
                        <p className="mt-1 font-mono text-xs uppercase tracking-[0.08em] text-[var(--text-secondary)]">
                          {skill.path ?? t("agent.installed")}
                        </p>
                      </div>
                      <div className="flex items-center gap-3">
                        <p className="panel-label">{skill.managed ? t("agent.managed") : t("agent.local")}</p>
                        {skill.path ? (
                          <button
                            type="button"
                            className="panel-button-secondary !min-h-0 !px-4 !py-2"
                            onClick={() => deleteSkill(skill.path!)}
                            disabled={deleteInstalledSkill.isPending || updateAgent.isPending}
                            title={t("agent.deleteInstalledSkillTitle")}
                          >
                            {t("agent.deleteInstalledSkill")}
                          </button>
                        ) : null}
                      </div>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                      {skill.description || t("agent.noInstalledDescription")}
                    </p>
                  </article>
                ))
              ) : (
                <p className="panel-inline-status">
                  {t("agent.emptyInstalled")}
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
