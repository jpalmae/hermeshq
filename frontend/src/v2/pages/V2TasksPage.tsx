import { useMemo, useState, useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useAgents } from "../../api/agents";
import { useCancelTask, useDeleteTask, useTasks, useUpdateTaskBoard } from "../../api/tasks";
import { useCreateTask } from "../../api/tasks";
import { useRealtimeStore } from "../../stores/realtimeStore";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

const COLUMNS = ["inbox", "assigned", "in_progress", "review", "done", "blocked", "archived"] as const;

function taskTone(status: string): "success" | "error" | "warn" | "info" | "neutral" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "error";
  if (status === "running") return "info";
  if (status === "queued") return "warn";
  return "neutral";
}

function excerpt(text: string | null | undefined, max = 110): string {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
}

export function V2TasksPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { data: agents } = useAgents();
  const { data: tasks } = useTasks();
  const createTask = useCreateTask();
  const cancelTask = useCancelTask();
  const deleteTask = useDeleteTask();
  const updateBoard = useUpdateTaskBoard();
  const events = useRealtimeStore((state) => state.events);

  useEffect(() => {
    const latest = events[0];
    if (!latest) return;
    if (latest.type?.startsWith("task.") && latest.type !== "task.progress") {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    }
  }, [events, queryClient]);

  const COLUMN_LABELS: Record<string, string> = {
    inbox: t("v2.inbox"),
    assigned: t("v2.assigned"),
    in_progress: t("v2.inProgress"),
    review: t("v2.review"),
    done: t("v2.done"),
    blocked: t("v2.blocked"),
    archived: t("v2.archived"),
  };

  const [agentFilter, setAgentFilter] = useState("");
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [agentId, setAgentId] = useState("");

  const agentsById = useMemo(() => new Map((agents ?? []).map((a) => [a.id, a])), [agents]);

  const grouped = useMemo(() => {
    const map = new Map<string, typeof tasks>();
    for (const col of COLUMNS) map.set(col, []);
    for (const task of tasks ?? []) {
      if (agentFilter && task.agent_id !== agentFilter) continue;
      const col = task.board_column ?? "inbox";
      map.set(col, [...(map.get(col) ?? []), task]);
    }
    return map;
  }, [tasks, agentFilter]);

  const draggedTaskColumn = draggedId ? (tasks ?? []).find((t) => t.id === draggedId)?.board_column : null;

  async function handleMove(taskId: string, column: string) {
    try {
      await updateBoard.mutateAsync({ taskId, payload: { board_column: column } });
      v2toast.success(t("v2.movedTo", { column: COLUMN_LABELS[column] ?? column }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.moveFailed")));
    }
  }

  async function handleCancel(taskId: string) {
    try {
      await cancelTask.mutateAsync(taskId);
      v2toast.success(t("v2.taskCancelled"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.cancelFailed")));
    }
  }

  async function handleDelete(taskId: string) {
    try {
      await deleteTask.mutateAsync(taskId);
      v2toast.success(t("v2.taskDeleted"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.deleteFailed")));
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!prompt.trim() || !agentId) return;
    try {
      await createTask.mutateAsync({ agent_id: agentId, prompt: prompt.trim() });
      setPrompt("");
      v2toast.success(t("v2.taskDispatched"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.dispatchFailed")));
    }
  }

  return (
    <div>
      <div className="v2-page-header">
        <div>
          <h1 className="v2-page-title">{t("v2.taskBoard")}</h1>
          <p className="v2-page-subtitle">{t("v2.dragCardsOrDispatch")}</p>
        </div>
        <select
          className="v2-select"
          style={{ width: 200 }}
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
        >
          <option value="">{t("v2.allAgents")}</option>
          {(agents ?? []).map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.friendly_name || agent.name}
            </option>
          ))}
        </select>
      </div>

      <form onSubmit={handleSubmit} className="v2-card" style={{ padding: 14, marginBottom: 20, display: "flex", gap: 10, alignItems: "center" }}>
        <select
          className="v2-select"
          style={{ width: 220 }}
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          required
        >
          <option value="">{t("v2.selectAgent")}</option>
          {(agents ?? []).map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.friendly_name || agent.name}
            </option>
          ))}
        </select>
        <input
          className="v2-input"
          style={{ flex: 1 }}
          placeholder={t("v2.whatShouldAgentDo")}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          required
        />
        <button type="submit" className="v2-btn v2-btn-primary" disabled={createTask.isPending || !prompt.trim() || !agentId}>
          {createTask.isPending ? t("v2.sending") : t("v2.dispatch")}
        </button>
      </form>

      <div className="v2-board">
        {COLUMNS.map((column) => {
          const columnTasks = grouped.get(column) ?? [];
          const isDropTarget = draggedId && draggedTaskColumn !== column;
          return (
            <section
              key={column}
              className="v2-board-column"
              data-drop-target={isDropTarget || undefined}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (draggedId) void handleMove(draggedId, column);
                setDraggedId(null);
              }}
            >
              <div className="v2-board-column-header">
                <span className="v2-board-column-title">{COLUMN_LABELS[column]}</span>
                <span className="v2-board-column-count">{columnTasks.length}</span>
              </div>
              <div className="v2-board-column-body">
                {columnTasks.length === 0 ? (
                  <p style={{ fontSize: 12, color: "var(--v2-text-muted)", textAlign: "center", padding: "16px 0" }}>{t("v2.noTasks")}</p>
                ) : (
                  columnTasks.map((task) => {
                    const agent = agentsById.get(task.agent_id);
                    return (
                      <article
                        key={task.id}
                        className="v2-board-card"
                        draggable
                        onDragStart={() => setDraggedId(task.id)}
                        onDragEnd={() => setDraggedId(null)}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
                          <div className="v2-board-card-title">{task.title ?? excerpt(task.prompt, 48)}</div>
                          <span className={`v2-pill`} data-tone={taskTone(task.status)}>
                            <span className="v2-pill-dot" />
                            {task.status}
                          </span>
                        </div>
                        <div className="v2-board-card-text">{excerpt(task.prompt)}</div>
                        <div className="v2-board-card-meta">
                          <span className="v2-mono" style={{ color: "var(--v2-text-muted)", fontSize: 11 }}>
                            {agent?.friendly_name || agent?.name || task.agent_id.slice(0, 8)}
                          </span>
                        </div>
                        <div className="v2-board-card-meta" style={{ marginTop: 10 }}>
                          <select
                            className="v2-select"
                            style={{ fontSize: 11.5, padding: "3px 6px", width: "auto" }}
                            value={task.board_column ?? "inbox"}
                            onChange={(e) => void handleMove(task.id, e.target.value)}
                          >
                            {COLUMNS.map((col) => (
                              <option key={col} value={col}>{COLUMN_LABELS[col]}</option>
                            ))}
                          </select>
                          {task.status === "running" || task.status === "queued" ? (
                            <button className="v2-btn v2-btn-ghost" style={{ padding: "2px 8px", fontSize: 11.5 }} onClick={() => void handleCancel(task.id)}>
                              {t("v2.cancel2")}
                            </button>
                          ) : (
                            <button className="v2-btn v2-btn-ghost" style={{ padding: "2px 8px", fontSize: 11.5, color: "var(--v2-danger)" }} onClick={() => void handleDelete(task.id)}>
                              {t("v2.delete")}
                            </button>
                          )}
                        </div>
                      </article>
                    );
                  })
                )}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
