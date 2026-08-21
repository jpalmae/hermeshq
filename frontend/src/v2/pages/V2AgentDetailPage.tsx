import { FormEvent, useMemo, useRef, useState, useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useAgent, useAgentAction, useUpdateAgent, useDeleteAgent } from "../../api/agents";
import { useProviders } from "../../api/providers";
import { useSecrets } from "../../api/secrets";
import { useHermesVersions } from "../../api/hermesVersions";
import { useLogs } from "../../api/logs";
import { useRuntimeLedger } from "../../api/runtimeLedger";
import { useTasks, useCreateTask } from "../../api/tasks";
import { AgentMessagingPanel } from "../../components/AgentMessagingPanel";
import { AgentAvatar } from "../../components/AgentAvatar";
import { AgentM365ScopesPanel } from "../../components/AgentM365ScopesPanel";
import { AgentSkillsPanel } from "../../components/AgentSkillsPanel";
import { AgentTerminal } from "../../components/AgentTerminal";
import { WorkspacePanel } from "../../components/WorkspacePanel";
import { MarkdownText } from "../../components/MarkdownText";
import { useSessionStore } from "../../stores/sessionStore";
import { v2toast, extractErrorMessage } from "../toast";
import { V2AgentIntegrationsTab } from "./V2AgentIntegrationsTab";
import { V2AgentConfigTab } from "./V2AgentConfigTab";
import { V2AgentPermissionsTab } from "./V2AgentPermissionsTab";
import { useI18n } from "../../lib/i18n";

type DetailTab = "conversation" | "config" | "channels" | "integrations" | "terminal" | "skills" | "workspace" | "ledger" | "activity" | "permissions";

function statusTone(status: string): "success" | "error" | "warn" | "neutral" {
  if (status === "running") return "success";
  if (status === "error") return "error";
  if (status === "starting" || status === "paused") return "warn";
  return "neutral";
}

function taskTone(status: string): "success" | "error" | "warn" | "info" | "neutral" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "error";
  if (status === "running") return "info";
  if (status === "queued") return "warn";
  return "neutral";
}

function severityTone(severity: string): "error" | "warn" | "info" | "neutral" {
  if (severity === "error" || severity === "critical") return "error";
  if (severity === "warning") return "warn";
  if (severity === "info") return "info";
  return "neutral";
}

export function V2AgentDetailPage() {
  const { t } = useI18n();
  const { agentId = "" } = useParams();
  const navigate = useNavigate();
  const { data: agent, isLoading } = useAgent(agentId);
  const { data: tasks } = useTasks(agentId);
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const updateAgent = useUpdateAgent();
  const deleteAgent = useDeleteAgent();
  const createTask = useCreateTask();
  const { data: providers } = useProviders(true);
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { data: secrets } = useSecrets(isAdmin);
  const { data: hermesVersions } = useHermesVersions(isAdmin);

  const [tab, setTab] = useState<DetailTab>("conversation");
  const [prompt, setPrompt] = useState("");
  const feedRef = useRef<HTMLDivElement | null>(null);
  const nearBottomRef = useRef(true);

  const { data: logsPages } = useLogs(agentId, 60);
  const activityLogs = useMemo(() => logsPages?.pages.flatMap((p) => p.items) ?? [], [logsPages]);

  const { data: runtimeLedger } = useRuntimeLedger(agentId);

  const sortedTasks = useMemo(
    () => [...(tasks ?? [])].sort((a, b) => new Date(a.queued_at).getTime() - new Date(b.queued_at).getTime()),
    [tasks],
  );

  useEffect(() => {
    const node = feedRef.current;
    if (node && nearBottomRef.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [sortedTasks.length]);

  function handleScroll() {
    const node = feedRef.current;
    if (!node) return;
    nearBottomRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  }

  async function handleStart() {
    if (!agent) return;
    const name = agent.friendly_name || agent.name;
    try {
      await startAgent.mutateAsync(agent.id);
      v2toast.success(t("v2.agentStarted", { name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.failedToStart", { name })));
    }
  }

  async function handleStop() {
    if (!agent) return;
    const name = agent.friendly_name || agent.name;
    try {
      await stopAgent.mutateAsync(agent.id);
      v2toast.success(t("v2.agentStopped", { name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.failedToStop", { name })));
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim() || !agent) return;
    const text = prompt.trim();
    setPrompt("");
    try {
      if (agent.status !== "running") {
        await startAgent.mutateAsync(agent.id);
      }
      await createTask.mutateAsync({
        agent_id: agent.id,
        prompt: text,
        metadata: { conversation: true, thread_id: `console_${agent.id}` },
      });
      nearBottomRef.current = true;
      v2toast.success(t("v2.messageSent"));
    } catch (error) {
      setPrompt(text);
      v2toast.error(extractErrorMessage(error, t("v2.sendFailed")));
    }
  }

  async function handleDelete() {
    if (!agent) return;
    const name = agent.friendly_name || agent.name;
    const isArchived = agent.is_archived;
    const confirmed = window.confirm(
      isArchived
        ? t("v2.permanentlyDeleteConfirm", { name })
        : t("v2.archiveConfirm", { name }),
    );
    if (!confirmed) return;
    try {
      await deleteAgent.mutateAsync(agent.id);
      v2toast.success(isArchived ? t("v2.agentPermanentlyDeleted", { name }) : t("v2.agentArchivedToast", { name }));
      navigate("/v2/agents");
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.deleteFailed")));
    }
  }

  async function handleRestore() {
    if (!agent) return;
    try {
      await updateAgent.mutateAsync({ agentId: agent.id, payload: { is_archived: false } });
      v2toast.success(t("v2.agentRestored", { name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.restoreFailed")));
    }
  }

  if (isLoading) {
    return (
      <div style={{ padding: 40 }}>
        <div className="v2-skeleton" style={{ height: 32, width: 260, marginBottom: 12 }} />
        <div className="v2-skeleton" style={{ height: 400 }} />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="v2-empty">
        <p className="v2-empty-title">{t("v2.agentNotFound")}</p>
        <div className="v2-empty-action">
          <Link to="/v2/agents" className="v2-btn v2-btn-secondary">{t("v2.backToAgentsBtn")}</Link>
        </div>
      </div>
    );
  }

  const name = agent.friendly_name || agent.name;
  const isRunning = agent.status === "running";

  const isPi = agent.runtime_type === "pi";

  const TABS: Array<{ id: DetailTab; label: string }> = [
    { id: "conversation", label: t("v2.conversation") },
    { id: "config", label: t("v2.config") },
    { id: "channels", label: t("v2.channels") },
    { id: "integrations", label: t("v2.integrations") },
    { id: "terminal", label: t("v2.terminal") },
    ...(isPi ? [{ id: "permissions" as DetailTab, label: t("v2.permissions") }] : []),
    { id: "skills", label: t("v2.skills") },
    { id: "workspace", label: t("v2.workspace") },
    { id: "ledger", label: t("v2.ledger") },
    { id: "activity", label: t("v2.activity") },
  ];

  return (
    <div>
      <div style={{ marginBottom: 20 }}>
        <Link to="/v2/agents" style={{ fontSize: 13, color: "var(--v2-text-secondary)", textDecoration: "none" }}>
          {t("v2.backToAgentsShort")}
        </Link>
      </div>

      <div className="v2-page-header" style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <AgentAvatar agent={agent} sizeClass="h-11 w-11" roundedClass="rounded-lg" />
          <div>
            <h1 className="v2-page-title" style={{ fontSize: 22 }}>{name}</h1>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
              <span className="v2-pill" data-tone={statusTone(agent.status)}>
                <span className="v2-pill-dot" />
                {agent.status}
              </span>
              <span className="v2-mono" style={{ color: "var(--v2-text-muted)", fontSize: 11.5 }}>
                {agent.model ?? agent.provider}
              </span>
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {agent.is_archived ? (
            <button className="v2-btn v2-btn-secondary" onClick={() => void handleRestore()} disabled={updateAgent.isPending}>
              {t("v2.restore")}
            </button>
          ) : isRunning ? (
            <button className="v2-btn v2-btn-secondary" onClick={() => void handleStop()} disabled={stopAgent.isPending}>
              {t("v2.stop")}
            </button>
          ) : (
            <button className="v2-btn v2-btn-primary" onClick={() => void handleStart()} disabled={startAgent.isPending}>
              {t("v2.start")}
            </button>
          )}
          {isAdmin ? (
            <button className="v2-btn v2-btn-danger" onClick={() => void handleDelete()} disabled={deleteAgent.isPending}>
              {agent.is_archived ? t("v2.deletePermanently") : t("v2.archive")}
            </button>
          ) : null}
        </div>
      </div>

      {agent.is_archived ? (
        <div className="v2-card" style={{ padding: "12px 16px", marginBottom: 16, borderColor: "var(--v2-warning)", background: "var(--v2-warning-subtle)" }}>
          <p style={{ fontSize: 13, color: "var(--v2-warning)", fontWeight: 550 }}>
            {t("v2.archivedWarning")}
          </p>
        </div>
      ) : null}

      <div style={{ display: "flex", gap: 4, marginBottom: 20, borderBottom: "1px solid var(--v2-border)", overflowX: "auto" }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            style={{
              padding: "9px 14px",
              fontSize: 13,
              fontWeight: tab === t.id ? 620 : 500,
              fontFamily: "var(--v2-font-sans)",
              color: tab === t.id ? "var(--v2-text)" : "var(--v2-text-muted)",
              background: "transparent",
              border: "none",
              borderBottom: tab === t.id ? "2px solid var(--v2-accent)" : "2px solid transparent",
              cursor: "pointer",
              whiteSpace: "nowrap",
              marginBottom: -1,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "conversation" ? (
        <section className="v2-card" style={{ display: "flex", flexDirection: "column", height: "62vh" }}>
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.conversation")}</h2>
            <span className="v2-mono" style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>
              {sortedTasks.length} {t("v2.messages")}
            </span>
          </div>
          <div
            ref={feedRef}
            onScroll={handleScroll}
            className="v2-chat"
            style={{ flex: 1, overflowY: "auto", padding: 16 }}
          >
            {sortedTasks.length === 0 ? (
              <div className="v2-empty" style={{ margin: "auto" }}>
                <p className="v2-empty-title">{t("v2.noMessagesYet")}</p>
                <p className="v2-empty-text">{t("v2.sendFirstMessage")} {name}.</p>
              </div>
            ) : (
              sortedTasks.flatMap((task) => {
                const items = [
                  <div key={`${task.id}-u`} className="v2-chat-msg" data-role="user">
                    {task.prompt}
                    <div className="v2-chat-meta">
                      <span>{task.status}</span>
                    </div>
                  </div>,
                ];
                const response = task.response || task.error_message || (task.status === "running" ? t("v2.runningLoading") : task.status === "queued" ? t("v2.queued") : "");
                if (response) {
                  items.push(
                    <div key={`${task.id}-a`} className="v2-chat-msg" data-role={task.status === "failed" ? "system" : "assistant"}>
                      {task.status === "failed" ? (
                        <>
                          {task.error_message}
                          <div className="v2-chat-meta">
                            <button
                              className="v2-btn v2-btn-ghost"
                              style={{ padding: "1px 8px", fontSize: 11, color: "var(--v2-danger)" }}
                              onClick={() => {
                                setPrompt(task.prompt);
                                nearBottomRef.current = true;
                              }}
                            >
                              {t("v2.retry")} ↻
                            </button>
                          </div>
                        </>
                      ) : (
                        <>
                          <MarkdownText>{response}</MarkdownText>
                          <div className="v2-chat-meta">
                            <span className="v2-pill" data-tone={taskTone(task.status)} style={{ fontSize: 10 }}>
                              {task.status}
                            </span>
                          </div>
                        </>
                      )}
                    </div>,
                  );
                }
                return items;
              })
            )}
          </div>
          <form onSubmit={handleSubmit} style={{ padding: 12, borderTop: "1px solid var(--v2-border)", display: "flex", gap: 10 }}>
            <input
              className="v2-input"
              style={{ flex: 1 }}
              placeholder={t("v2.messageNamePlaceholder", { name })}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            <button type="submit" className="v2-btn v2-btn-primary" disabled={createTask.isPending || !prompt.trim()}>
              {t("v2.send")}
            </button>
          </form>
        </section>
      ) : null}

      {tab === "config" ? (
        <V2AgentConfigTab
          agent={agent}
          isAdmin={isAdmin}
          providers={providers ?? []}
          secrets={secrets ?? []}
          hermesVersions={hermesVersions ?? []}
          updateAgent={updateAgent}
        />
      ) : null}

      {tab === "channels" ? (
        <section className="v2-card">
          <div className="v2-card-body">
            <AgentMessagingPanel agentId={agent.id} isAdmin={isAdmin} />
          </div>
        </section>
      ) : null}

      {tab === "integrations" ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <V2AgentIntegrationsTab agent={agent} isAdmin={isAdmin} />
          <section className="v2-card">
            <div className="v2-card-body">
              <AgentM365ScopesPanel agentId={agent.id} />
            </div>
          </section>
        </div>
      ) : null}

      {tab === "terminal" ? (
        <section className="v2-card" style={{ overflow: "hidden" }}>
          <AgentTerminal
            agentId={agent.id}
            mode={agent.run_mode ?? "hermes"}
            runtimeProfile={agent.runtime_profile ?? "standard"}
            archived={agent.is_archived}
          />
        </section>
      ) : null}

      {tab === "permissions" && isPi ? (
        <V2AgentPermissionsTab agent={agent} isAdmin={isAdmin} />
      ) : null}

      {tab === "skills" ? (
        <section className="v2-card">
          <div className="v2-card-body">
            <AgentSkillsPanel agent={agent} embedded />
          </div>
        </section>
      ) : null}

      {tab === "workspace" ? (
        <section className="v2-card">
          <div className="v2-card-body">
            <WorkspacePanel agentId={agent.id} />
          </div>
        </section>
      ) : null}

      {tab === "ledger" ? (
        <section className="v2-card">
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.runtimeLedger")}</h2>
            <span className="v2-mono" style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>
              {(runtimeLedger ?? []).length} {t("v2.entries")}
            </span>
          </div>
          <table className="v2-table">
            <thead>
              <tr>
                <th>{t("v2.time")}</th>
                <th>{t("v2.channel")}</th>
                <th>{t("v2.direction")}</th>
                <th>{t("v2.type")}</th>
                <th>{t("v2.content")}</th>
              </tr>
            </thead>
            <tbody>
              {(runtimeLedger ?? []).length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    <div className="v2-empty">
                      <p className="v2-empty-title">{t("v2.noLedgerEntries")}</p>
                    </div>
                  </td>
                </tr>
              ) : (
                (runtimeLedger ?? []).map((entry) => (
                  <tr key={entry.id}>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <span className="v2-mono" style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>
                        {entry.created_at ? new Date(entry.created_at).toLocaleString() : "—"}
                      </span>
                    </td>
                    <td>
                      <span className="v2-pill" data-tone="neutral">{entry.channel?.replace(/_/g, " ") ?? "—"}</span>
                    </td>
                    <td>
                      <span className="v2-mono" style={{ fontSize: 11.5 }}>{entry.direction ?? "—"}</span>
                    </td>
                    <td>
                      <span className="v2-mono" style={{ fontSize: 11.5 }}>{entry.entry_type ?? "—"}</span>
                    </td>
                    <td style={{ maxWidth: 380 }}>
                      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {entry.title ?? entry.content ?? "—"}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      ) : null}

      {tab === "activity" ? (
        <section className="v2-card">
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.activityLogTitle")}</h2>
          </div>
          <table className="v2-table">
            <thead>
              <tr>
                <th>{t("v2.time")}</th>
                <th>{t("v2.event")}</th>
                <th>{t("v2.message")}</th>
                <th>{t("v2.severity")}</th>
              </tr>
            </thead>
            <tbody>
              {activityLogs.length === 0 ? (
                <tr>
                  <td colSpan={4}>
                    <div className="v2-empty">
                      <p className="v2-empty-title">{t("v2.noActivity")}</p>
                    </div>
                  </td>
                </tr>
              ) : (
                activityLogs.map((log) => (
                  <tr key={log.id}>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <span className="v2-mono" style={{ fontSize: 11.5, color: "var(--v2-text-muted)" }}>
                        {log.created_at ? new Date(log.created_at).toLocaleString() : "—"}
                      </span>
                    </td>
                    <td>
                      <span className="v2-mono" style={{ fontSize: 11.5 }}>{log.event_type}</span>
                    </td>
                    <td style={{ maxWidth: 380 }}>
                      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {log.message ?? "—"}
                      </div>
                    </td>
                    <td>
                      <span className="v2-pill" data-tone={severityTone(log.severity ?? "")}>
                        {log.severity ?? "info"}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
