import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useAgent, useAgentAction, useDeleteAgent, useUpdateAgent } from "../api/agents";
import { useLogs } from "../api/logs";
import { useCreateTask, useTasks } from "../api/tasks";
import { AgentConversationPanel } from "../components/AgentConversationPanel";
import { AgentMessagingPanel } from "../components/AgentMessagingPanel";
import { AgentSkillsPanel } from "../components/AgentSkillsPanel";
import { AgentTerminal } from "../components/AgentTerminal";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { useSessionStore } from "../stores/sessionStore";

const DEFAULT_SECTION_STATE = {
  conversation: true,
  skills: false,
  ledger: false,
  logs: false,
  workspace: false,
};

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "stopped") return "text-[var(--text-secondary)]";
  return "text-[var(--warning)]";
}

function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || "agent";
}

export function AgentDetailPage() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { data: agent, isLoading } = useAgent(agentId);
  const { data: tasks } = useTasks();
  const { data: logs } = useLogs(agentId);
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const deleteAgent = useDeleteAgent();
  const updateAgent = useUpdateAgent();
  const createTask = useCreateTask();
  const [identityForm, setIdentityForm] = useState({
    friendly_name: "",
    name: "",
    slug: "",
  });
  const [systemPromptDraft, setSystemPromptDraft] = useState("");
  const [sectionState, setSectionState] = useState(DEFAULT_SECTION_STATE);
  const [nameTouched, setNameTouched] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const agentTasks = useMemo(
    () => (tasks ?? []).filter((task) => task.agent_id === agentId),
    [tasks, agentId],
  );
  const ledgerTasks = useMemo(
    () =>
      [...agentTasks].sort(
        (left, right) => new Date(right.queued_at).getTime() - new Date(left.queued_at).getTime(),
      ),
    [agentTasks],
  );

  useEffect(() => {
    if (!isLoading && agent === null) {
      navigate("/agents", { replace: true });
    }
  }, [agent, isLoading, navigate]);

  useEffect(() => {
    if (!agent) {
      return;
    }
    setIdentityForm({
      friendly_name: agent.friendly_name || agent.name,
      name: agent.name,
      slug: agent.slug,
    });
    setSystemPromptDraft(agent.system_prompt ?? "");
    setNameTouched(false);
    setSlugTouched(false);
  }, [agent]);

  useEffect(() => {
    if (!agentId) {
      return;
    }
    const raw = window.localStorage.getItem(`hermeshq.agentDetail.sections.${agentId}`);
    if (!raw) {
      setSectionState(DEFAULT_SECTION_STATE);
      return;
    }
    try {
      const parsed = JSON.parse(raw) as Partial<typeof DEFAULT_SECTION_STATE>;
      setSectionState({ ...DEFAULT_SECTION_STATE, ...parsed });
    } catch {
      setSectionState(DEFAULT_SECTION_STATE);
    }
  }, [agentId]);

  function toggleSection(section: keyof typeof DEFAULT_SECTION_STATE) {
    setSectionState((current) => {
      const next = { ...current, [section]: !current[section] };
      if (agentId) {
        window.localStorage.setItem(`hermeshq.agentDetail.sections.${agentId}`, JSON.stringify(next));
      }
      return next;
    });
  }

  function renderSectionShell(
    section: keyof typeof DEFAULT_SECTION_STATE,
    eyebrow: string,
    title: string,
    meta: string,
    children: ReactNode,
  ) {
    const isOpen = sectionState[section];
    return (
      <section className="panel-frame p-6">
        <button
          type="button"
          className="flex w-full items-end justify-between gap-4 border-b border-[var(--border)] pb-4 text-left"
          onClick={() => toggleSection(section)}
        >
          <div>
            <p className="panel-label">{eyebrow}</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">{title}</h3>
          </div>
          <div className="text-right">
            <p className="panel-label">{meta}</p>
            <p className="mt-2 font-mono text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
              {isOpen ? "Collapse" : "Expand"}
            </p>
          </div>
        </button>
        {isOpen ? <div className="mt-5">{children}</div> : null}
      </section>
    );
  }

  if (isLoading || !agent) {
    return <p className="panel-inline-status">[LOADING] agent profile</p>;
  }

  const currentAgent = agent;

  async function onDelete() {
    const confirmed = window.confirm(`Delete agent "${currentAgent.name}"? This will remove its workspace and task history.`);
    if (!confirmed) {
      return;
    }
    try {
      await deleteAgent.mutateAsync(currentAgent.id);
      navigate("/agents");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Delete request failed. Reload the page and try again.";
      window.alert(message);
    }
  }

  async function onSendInstruction(prompt: string) {
    if (currentAgent.status !== "running") {
      await startAgent.mutateAsync(currentAgent.id);
    }
    await createTask.mutateAsync({
      agent_id: currentAgent.id,
      title: "Chat message",
      prompt,
      priority: 5,
    });
  }

  async function onSaveIdentity() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        friendly_name: identityForm.friendly_name.trim() || currentAgent.name,
        name: identityForm.name.trim(),
        slug: identityForm.slug.trim(),
      },
    });
  }

  async function onSaveSystemPrompt() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        system_prompt: systemPromptDraft.trim() || null,
      },
    });
  }

  return (
    <div className="space-y-6">
      <section className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="panel-frame p-8">
          <p className="panel-label">{agent.slug}</p>
          <div className="mt-6 grid gap-8 md:grid-cols-[1fr_auto]">
            <div>
              <h2 className="text-[clamp(2.5rem,6vw,4.5rem)] leading-[0.95] text-[var(--text-display)]">
                {agent.friendly_name || agent.name}
              </h2>
              <p className="mt-3 text-sm uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                {agent.name} / {agent.slug}
              </p>
              <p className="mt-4 max-w-[34rem] text-base leading-7 text-[var(--text-secondary)]">
                {agent.description ?? "No operator description yet."}
              </p>
            </div>
            <div className="h-28 w-28 rounded-full border border-[var(--border-visible)]" />
          </div>

          <div className="mt-10 grid gap-6 border-t border-[var(--border)] pt-6 md:grid-cols-4">
            <div>
              <p className="panel-label">Status</p>
              <p className={`mt-2 text-lg uppercase tracking-[0.1em] ${statusTone(agent.status)}`}>
                {agent.status}
              </p>
            </div>
            <div>
              <p className="panel-label">Mode</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.run_mode}</p>
            </div>
            <div>
              <p className="panel-label">Tasks</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.total_tasks}</p>
            </div>
            <div>
              <p className="panel-label">Tokens</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.total_tokens_used}</p>
            </div>
          </div>

          <div className="mt-8 flex flex-wrap gap-3">
            <button className="panel-button-primary" onClick={() => startAgent.mutate(agent.id)}>
              Start runtime
            </button>
            <button className="panel-button-secondary" onClick={() => stopAgent.mutate(agent.id)}>
              Stop runtime
            </button>
            <Link className="panel-button-secondary" to={`/schedules?agentId=${agent.id}`}>
              Schedules
            </Link>
            {isAdmin ? (
              <button
                className="panel-button-secondary border-[var(--accent)] text-[var(--accent)]"
                onClick={onDelete}
                disabled={deleteAgent.isPending}
              >
                Delete agent
              </button>
            ) : null}
          </div>
        </div>

        <div className="panel-frame p-6">
          <p className="panel-label">Configuration</p>
          <div className="mt-6 border-b border-[var(--border)] pb-5">
            <label className="panel-field">
              <span className="panel-label">Friendly name</span>
              <input
                value={identityForm.friendly_name}
                onChange={(event) =>
                  setIdentityForm((current) => {
                    const friendlyName = event.target.value;
                    const next = { ...current, friendly_name: friendlyName };
                    if (!nameTouched) {
                      next.name = friendlyName.trim();
                    }
                    if (!slugTouched) {
                      next.slug = slugify(friendlyName.trim() || next.name.trim());
                    }
                    return next;
                  })
                }
                placeholder="Display name for humans"
              />
            </label>
            <label className="panel-field mt-4">
              <span className="panel-label">Technical name</span>
              <input
                value={identityForm.name}
                onChange={(event) => {
                  const nextName = event.target.value;
                  setNameTouched(true);
                  setIdentityForm((current) => {
                    const next = { ...current, name: nextName };
                    if (!slugTouched && !current.friendly_name.trim()) {
                      next.slug = slugify(nextName.trim());
                    }
                    return next;
                  });
                }}
                placeholder="Runtime/operator name"
              />
            </label>
            <label className="panel-field mt-4">
              <span className="panel-label">Slug</span>
              <input
                value={identityForm.slug}
                onChange={(event) => {
                  setSlugTouched(true);
                  setIdentityForm((current) => ({ ...current, slug: event.target.value }));
                }}
                placeholder="Unique short identifier"
              />
            </label>
            <div className="mt-4 flex items-center gap-3">
              <button
                type="button"
                className="panel-button-secondary"
                disabled={updateAgent.isPending}
                onClick={onSaveIdentity}
              >
                {updateAgent.isPending ? "[LOADING]" : "Save identity"}
              </button>
              <p className="panel-inline-status">Friendly name autocompletes technical name and slug until you override them manually.</p>
            </div>
          </div>
          <div className="mt-6 space-y-4">
            <div className="border-b border-[var(--border)] pb-5">
              <label className="panel-field">
                <span className="panel-label">System prompt</span>
                <textarea
                  rows={6}
                  value={systemPromptDraft}
                  onChange={(event) => setSystemPromptDraft(event.target.value)}
                  placeholder="Persistent operator instructions for this agent"
                />
              </label>
              <div className="mt-4 flex items-center gap-3">
                <button
                  type="button"
                  className="panel-button-secondary"
                  disabled={updateAgent.isPending}
                  onClick={onSaveSystemPrompt}
                >
                  {updateAgent.isPending ? "[LOADING]" : "Save system prompt"}
                </button>
                <p className="panel-inline-status">This updates the persistent runtime instructions for future tasks and TUI sessions.</p>
              </div>
            </div>
            <div className="panel-stat-row">
              <span>Model</span>
              <strong>{agent.model}</strong>
            </div>
            <div className="panel-stat-row">
              <span>Provider</span>
              <strong>{agent.provider}</strong>
            </div>
            <div className="panel-stat-row">
              <span>Secret ref</span>
              <strong>{agent.api_key_ref ?? "none"}</strong>
            </div>
            <div className="panel-stat-row">
              <span>Node</span>
              <strong>{agent.node?.name ?? "Local Runtime"}</strong>
            </div>
            <div className="panel-stat-row">
              <span>Workspace</span>
              <strong className="truncate text-right">{agent.workspace_path}</strong>
            </div>

            <AgentMessagingPanel agentId={agent.id} isAdmin={isAdmin} />
          </div>
        </div>
      </section>

      <AgentTerminal agentId={agent.id} mode={agent.run_mode} />

      {renderSectionShell(
        "conversation",
        "Conversation",
        "Talk to this agent",
        agent.status === "running" ? "Live runtime" : "Auto-start on send",
        <AgentConversationPanel
          tasks={agentTasks}
          agentStatus={agent.status}
          onSubmit={onSendInstruction}
          isSubmitting={createTask.isPending}
          embedded
        />,
      )}

      {renderSectionShell(
        "ledger",
        "Task history",
        "Runtime ledger",
        `${agentTasks.length} records`,
        <div className="mt-0">
          {ledgerTasks.map((task) => (
            <article key={task.id} className="grid gap-4 border-b border-[var(--border)] py-5 md:grid-cols-[0.7fr_1.3fr]">
              <div>
                <p className="panel-label">{task.status}</p>
                <p className="mt-2 text-sm text-[var(--text-primary)]">
                  {task.title ?? "Operator task"}
                </p>
                <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                  {new Date(task.queued_at).toLocaleString()}
                </p>
              </div>
              <div>
                <p className="text-sm leading-6 text-[var(--text-secondary)]">{task.prompt}</p>
                {task.response ? (
                  <pre className="mt-3 overflow-x-auto whitespace-pre-wrap border-t border-[var(--border)] pt-3 text-sm leading-6 text-[var(--text-primary)]">
                    {task.response}
                  </pre>
                ) : null}
              </div>
            </article>
          ))}
        </div>,
      )}

      {renderSectionShell(
        "skills",
        "Skills",
        "Hermes skill registry",
        `${agent.skills.length} assigned`,
        <AgentSkillsPanel agent={agent} embedded />,
      )}

      {renderSectionShell(
        "logs",
        "Logs",
        "Activity stream",
        `${logs?.length ?? 0} events`,
        <div className="mt-0">
          {(logs ?? []).map((entry) => (
            <article key={String(entry.id)} className="grid gap-3 border-b border-[var(--border)] py-4 md:grid-cols-[0.45fr_1.55fr]">
              <div>
                <p className="panel-label">{String(entry.event_type)}</p>
                <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                  {new Date(String(entry.created_at)).toLocaleString()}
                </p>
              </div>
              <div>
                <p className="text-sm text-[var(--text-primary)]">{String(entry.message ?? "")}</p>
              </div>
            </article>
          ))}
        </div>,
      )}

      {renderSectionShell(
        "workspace",
        "Workspace",
        "Filesystem and editor",
        agent.workspace_path,
        <WorkspacePanel agentId={agent.id} />,
      )}
    </div>
  );
}
