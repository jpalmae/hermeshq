import { FormEvent, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useCancelTask, useCreateTask, useTasks, useUpdateTaskBoard } from "../api/tasks";
import { useI18n } from "../lib/i18n";
import type { Task } from "../types/api";

const kanbanColumns = ["inbox", "planned", "running", "blocked", "review", "done", "failed"] as const;

function statusTone(status: string) {
  if (status === "completed") return "text-[var(--success)]";
  if (status === "running" || status === "queued") return "text-[var(--warning)]";
  if (status === "failed") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
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
  const [composerCollapsed, setComposerCollapsed] = useState(() => {
    if (typeof window === "undefined") {
      return false;
    }
    return window.localStorage.getItem("hermeshq.tasks.composerCollapsed") === "true";
  });

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

  function toggleComposer() {
    setComposerCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem("hermeshq.tasks.composerCollapsed", String(next));
      return next;
    });
  }

  return (
    <div className="space-y-6">
      <section className={`grid gap-6 ${composerCollapsed ? "lg:grid-cols-[112px_minmax(0,1fr)]" : "lg:grid-cols-[minmax(22rem,0.5fr)_minmax(0,1.5fr)]"}`}>
        <form className={`panel-frame ${composerCollapsed ? "p-4" : "p-6"}`} onSubmit={onSubmit}>
          {composerCollapsed ? (
            <div className="flex min-h-[20rem] flex-col items-center gap-6 py-4">
              <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={toggleComposer}>
                »
              </button>
              <h2 className="mt-3 font-display text-[2.35rem] leading-none tracking-[0.08em] text-[var(--text-display)]">ST</h2>
              <p className="text-center text-xs uppercase tracking-[0.14em] text-[var(--text-disabled)]">
                {t("tasks.expandComposer")}
              </p>
            </div>
          ) : (
            <>
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-3">
                  <p className="panel-label">{t("tasks.dispatch")}</p>
                  <h2 className="text-3xl text-[var(--text-display)]">{t("tasks.submitTask")}</h2>
                  <p className="text-sm leading-6 text-[var(--text-secondary)]">
                    {t("tasks.boardCopy")}
                  </p>
                </div>
                <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={toggleComposer}>
                  «
                </button>
              </div>

              <div className="mt-8 space-y-5">
                <label className="panel-field">
                  <span className="panel-label">{t("tasks.agent")}</span>
                  <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
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
                  <input value={title} onChange={(event) => setTitle(event.target.value)} />
                </label>

                <label className="panel-field">
                  <span className="panel-label">{t("tasks.prompt")}</span>
                  <textarea rows={6} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
                </label>

                <button type="submit" className="panel-button-primary w-full" disabled={createTask.isPending}>
                  {createTask.isPending ? t("common.loading") : t("tasks.sendTask")}
                </button>
              </div>
            </>
          )}
        </form>

        <section className="panel-frame p-6">
          <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">{t("tasks.kanban")}</p>
              <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("tasks.boardTitle")}</h2>
            </div>
            <label className="panel-field !mt-0 min-w-[16rem]">
              <span className="panel-label">{t("tasks.filterAgent")}</span>
              <select value={boardAgentId} onChange={(event) => setBoardAgentId(event.target.value)}>
                <option value="">{t("tasks.allAgents")}</option>
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="mt-6 grid gap-4 xl:grid-cols-7">
            {kanbanColumns.map((column) => (
              <section
                key={column}
                className="rounded-[1.25rem] border border-[var(--border)] bg-[var(--surface-raised)] p-4"
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  if (draggedTaskId) {
                    void moveTask(draggedTaskId, column);
                  }
                  setDraggedTaskId(null);
                }}
              >
                <div className="border-b border-[var(--border)] pb-3">
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
                        className="cursor-grab rounded-[1rem] border border-[var(--border)] bg-[var(--black)] p-4 active:cursor-grabbing"
                        draggable
                        onDragStart={() => setDraggedTaskId(task.id)}
                        onDragEnd={() => setDraggedTaskId(null)}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="panel-label">
                                {t("tasks.boardState")}: {t(`tasks.column.${task.board_column}`)}
                              </span>
                              <span className={`panel-label ${statusTone(task.status)}`}>
                                {t("tasks.runtimeState")}: {task.status}
                              </span>
                            </div>
                            <h3 className="mt-2 text-sm text-[var(--text-display)]">
                              {task.title ?? t("tasks.operatorTask")}
                            </h3>
                          </div>
                          {task.board_manual ? (
                            <span className="rounded-full border border-[var(--border)] px-2 py-1 text-[10px] uppercase tracking-[0.14em] text-[var(--text-secondary)]">
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
                        <div className="mt-4 space-y-3">
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
      </section>
    </div>
  );
}
