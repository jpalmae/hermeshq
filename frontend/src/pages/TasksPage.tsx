import { FormEvent, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useCancelTask, useCreateTask, useTasks, useUpdateTaskBoard } from "../api/tasks";
import { useI18n } from "../lib/i18n";
import type { Task } from "../types/api";

import infoIcon from "../assets/icon/info.png";

const kanbanColumns = ["inbox", "planned", "running", "blocked", "review", "done", "failed"] as const;

function statusTone(status: string) {
  if (status === "completed") return "text-[var(--success)]";
  if (status === "running" || status === "queued") return "text-[var(--warning)]";
  if (status === "failed") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
}

function statusBadgeTone(status: string) {
  if (status === "completed") return "border-[color-mix(in_srgb,var(--success)_45%,transparent)] bg-[color-mix(in_srgb,var(--success)_14%,transparent)] text-[var(--success)]";
  if (status === "running" || status === "queued") return "border-[color-mix(in_srgb,var(--warning)_45%,transparent)] bg-[color-mix(in_srgb,var(--warning)_14%,transparent)] text-[var(--warning)]";
  if (status === "failed" || status === "cancelled") return "border-[color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] text-[var(--accent)]";
  return "border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_76%,transparent)] text-[var(--text-secondary)]";
}

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

function excerpt(value: string, max = 180) {
  if (value.length <= max) return value;
  return `${value.slice(0, max).trim()}…`;
}

export function TasksPage() {
  const { data: agents } = useAgents();
  const { data: tasks } = useTasks();
  const { t, formatDateTime } = useI18n();
  const createTask = useCreateTask();
  const cancelTask = useCancelTask();
  const updateTaskBoard = useUpdateTaskBoard();

  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [boardAgentId, setBoardAgentId] = useState("");
  const [draggedTaskId, setDraggedTaskId] = useState<string | null>(null);
  const [infoHovered, setInfoHovered] = useState(false);

  const agentsById = useMemo(
    () => new Map((agents ?? []).map((agent) => [agent.id, agent])),
    [agents],
  );

  const filteredTasks = useMemo(
    () => (tasks ?? []).filter((task) => !boardAgentId || task.agent_id === boardAgentId),
    [boardAgentId, tasks],
  );

  const grouped = useMemo(() => {
    const base = new Map<string, Task[]>();
    kanbanColumns.forEach((column) => base.set(column, []));
    for (const task of filteredTasks) {
      const column = task.board_column || "inbox";
      const bucket = base.get(column);
      if (bucket) {
        bucket.push(task);
      } else {
        base.get("inbox")?.push(task);
      }
    }
    for (const column of kanbanColumns) {
      base.get(column)?.sort((left, right) => right.board_order - left.board_order);
    }
    return base;
  }, [filteredTasks]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await createTask.mutateAsync({
      agent_id: agentId || agents?.[0]?.id,
      title,
      prompt,
      priority: 5,
    });
    setTitle("");
    setPrompt("");
  }

  async function moveTask(taskId: string, boardColumn: string) {
    await updateTaskBoard.mutateAsync({
      taskId,
      payload: {
        board_column: boardColumn,
        board_order: Date.now(),
      },
    });
  }

  return (
    <div className="tasks-page grid gap-6 lg:grid-cols-[250px_minmax(0,1fr)]">
      {/* Composer — sticky en desktop, full-width en mobile */}
      <div className="lg:sticky lg:top-8 lg:max-h-[calc(100vh-4rem)] lg:overflow-y-auto">
        <form className="tasks-composer panel-frame p-6" onSubmit={onSubmit}>
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-2">
              <p className="panel-label">{t("tasks.dispatch")}</p>
              <div className="flex items-baseline gap-2">
                <h2 className="text-xl md:text-2xl lg:text-3xl text-[var(--text-display)]">{t("tasks.submitTask")}</h2>
                <div className="relative">
                  <button
                    type="button"
                    className="transition-opacity hover:opacity-70"
                    onMouseEnter={() => setInfoHovered(true)}
                    onMouseLeave={() => setInfoHovered(false)}
                  >
                    <img src={infoIcon} alt="Info" className="app-shell-nav-icon h-4 w-4" />
                  </button>
                  {infoHovered && (
                    <div className="absolute left-[-30px] top-10 lg:fixed lg:left-[450px] lg:right-auto lg:top-30 z-10 w-48 rounded-lg border border-[var(--border)] bg-[var(--surface-raised)] p-3 text-xs leading-5 text-[var(--text-secondary)]">
                      {t("tasks.boardCopy")}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className="mt-8 space-y-5">
            <label className="panel-field">
              <span className="panel-label">{t("tasks.agent")}</span>
              <select value={agentId} onChange={(event) => setAgentId(event.target.value)} className="text-xs md:text-sm">
                <option value="">{t("tasks.selectRuntime")}</option>
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
            </label>

            <label className="panel-field">
              <span className="panel-label">{t("tasks.title")}</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} className="text-xs md:text-sm" />
            </label>

            <label className="panel-field">
              <span className="panel-label">{t("tasks.prompt")}</span>
              <textarea rows={6} value={prompt} onChange={(event) => setPrompt(event.target.value)} className="text-xs md:text-sm" />
            </label>

            <button type="submit" className="panel-button-primary w-full" disabled={createTask.isPending}>
              {createTask.isPending ? t("common.loading") : t("tasks.sendTask")}
            </button>
          </div>
        </form>
      </div>

      {/* Board */}
      <section className="tasks-board panel-frame p-6  pb-[32rem]">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">{t("tasks.kanban")}</p>
            <h2 className="mt-2 text-xl md:text-2xl lg:text-3xl text-[var(--text-display)]">{t("tasks.boardTitle")}</h2>
          </div>
          <label className="panel-field !mt-0 min-w-[16rem]">
            <span className="panel-label">{t("tasks.filterAgent")}</span>
            <select value={boardAgentId} onChange={(event) => setBoardAgentId(event.target.value)} className="text-xs md:text-sm">
              <option value="">{t("tasks.allAgents")}</option>
              {(agents ?? []).map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agentLabel(agent)}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-6 grid gap-4 grid-cols-1">
          {kanbanColumns.map((column) => (
            <section
              key={column}
              className="tasks-column rounded-[1.25rem] border border-[var(--border)] bg-[var(--surface-raised)] p-4"
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                if (draggedTaskId) {
                  void moveTask(draggedTaskId, column);
                }
                setDraggedTaskId(null);
              }}
            >
              <div className="tasks-column-header border-b border-[var(--border)] pb-3">
                <p className="panel-label">{t(`tasks.column.${column}`)}</p>
                <p className="mt-2 text-xl text-[var(--text-display)]">
                  {grouped.get(column)?.length ?? 0}
                </p>
              </div>
              <div className="mt-3 space-y-3">
                {(grouped.get(column) ?? []).map((task) => {
                  const agent = agentsById.get(task.agent_id);
                  return (
                    <article
                      key={task.id}
                      className="tasks-card cursor-grab rounded-[1rem] border border-[var(--border)] bg-[var(--black)] p-4 active:cursor-grabbing"
                      draggable
                      onDragStart={() => setDraggedTaskId(task.id)}
                      onDragEnd={() => setDraggedTaskId(null)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="tasks-card-badge panel-label rounded-full border border-[var(--border)] px-2.5 py-1">
                              {t("tasks.boardState")}: {t(`tasks.column.${task.board_column}`)}
                            </span>
                            <span className={`tasks-card-badge panel-label rounded-full border px-2.5 py-1 ${statusBadgeTone(task.status)}`}>
                              {t("tasks.runtimeState")}: {task.status}
                            </span>
                          </div>
                          <h3 className="mt-2 text-sm text-[var(--text-display)]">
                            {task.title ?? t("tasks.operatorTask")}
                          </h3>
                        </div>
                        {task.board_manual ? (
                          <span className="tasks-card-badge rounded-full border border-[var(--border)] px-2 py-1 text-[10px] uppercase tracking-[0.14em] text-[var(--text-secondary)]">
                            {t("tasks.manual")}
                          </span>
                        ) : null}
                      </div>
                      {agent ? (
                        <p className="mt-3 text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
                          {agentLabel(agent)}
                        </p>
                      ) : null}
                      <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                        {excerpt(task.prompt)}
                      </p>
                      {task.response ? (
                        <p className="mt-3 border-t border-[var(--border)] pt-3 text-sm leading-6 text-[var(--text-primary)]">
                          {excerpt(task.response, 140)}
                        </p>
                      ) : null}
                      {task.error_message ? (
                        <p className="mt-3 border-t border-[var(--border)] pt-3 text-sm leading-6 text-[var(--accent)]">
                          {excerpt(task.error_message, 140)}
                        </p>
                      ) : null}
                      <p className="mt-3 text-xs text-[var(--text-disabled)]">
                        {formatDateTime(task.completed_at ?? task.started_at ?? task.queued_at)}
                      </p>
                      <div className="tasks-card-actions mt-4 space-y-3">
                        <label className="panel-field !mt-0">
                          <span className="panel-label">{t("tasks.moveTo")}</span>
                          <select
                            value={task.board_column}
                            onChange={(event) => void moveTask(task.id, event.target.value)}
                            disabled={updateTaskBoard.isPending}
                          >
                            {kanbanColumns.map((item) => (
                              <option key={item} value={item}>
                                {t(`tasks.column.${item}`)}
                              </option>
                            ))}
                          </select>
                        </label>
                        {task.status === "running" || task.status === "queued" ? (
                          <button
                            className="panel-button-secondary w-full"
                            onClick={() => cancelTask.mutate(task.id)}
                            type="button"
                          >
                            {t("tasks.cancel")}
                          </button>
                        ) : null}
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}