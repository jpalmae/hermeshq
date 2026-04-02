import { FormEvent, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useCancelTask, useCreateTask, useTasks } from "../api/tasks";
import type { Task } from "../types/api";

const taskColumns = ["queued", "running", "completed", "cancelled"];

function statusTone(status: string) {
  if (status === "completed") return "text-[var(--success)]";
  if (status === "running" || status === "queued") return "text-[var(--warning)]";
  if (status === "failed") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
}

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

export function TasksPage() {
  const { data: agents } = useAgents();
  const { data: tasks } = useTasks();
  const createTask = useCreateTask();
  const cancelTask = useCancelTask();

  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");

  const grouped = useMemo(() => {
    const base = new Map<string, Task[]>();
    taskColumns.forEach((column) => base.set(column, []));
    (tasks ?? []).forEach((task) => {
      const column = base.get(task.status);
      if (column) {
        column.push(task);
      }
    });
    return base;
  }, [tasks]);

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

  return (
    <div className="space-y-6">
      <section className="grid gap-6 xl:grid-cols-[0.68fr_1.32fr]">
        <form className="panel-frame p-6" onSubmit={onSubmit}>
          <div className="space-y-3">
            <p className="panel-label">Dispatch</p>
            <h2 className="text-3xl text-[var(--text-display)]">Submit task</h2>
          </div>

          <div className="mt-8 space-y-5">
            <label className="panel-field">
              <span className="panel-label">Agent</span>
              <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                <option value="">Select runtime</option>
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
            </label>

            <label className="panel-field">
              <span className="panel-label">Title</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} />
            </label>

            <label className="panel-field">
              <span className="panel-label">Prompt</span>
              <textarea rows={6} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
            </label>

            <button type="submit" className="panel-button-primary w-full" disabled={createTask.isPending}>
              {createTask.isPending ? "[LOADING]" : "Send task"}
            </button>
          </div>
        </form>

        <div className="grid gap-4 xl:grid-cols-4">
          {taskColumns.map((column) => (
            <section key={column} className="panel-frame p-4">
              <div className="border-b border-[var(--border)] pb-3">
                <p className="panel-label">{column}</p>
                <p className="mt-2 text-xl text-[var(--text-display)]">
                  {grouped.get(column)?.length ?? 0}
                </p>
              </div>
              <div className="mt-3 space-y-3">
                {(grouped.get(column) ?? []).map((task) => (
                  <article key={task.id} className="border border-[var(--border)] p-4">
                    <p className={`panel-label ${statusTone(task.status)}`}>{task.status}</p>
                    <h3 className="mt-2 text-sm text-[var(--text-display)]">
                      {task.title ?? "Operator task"}
                    </h3>
                    <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                      {task.prompt}
                    </p>
                    {task.response ? (
                      <pre className="mt-3 whitespace-pre-wrap border-t border-[var(--border)] pt-3 text-xs leading-6 text-[var(--text-primary)]">
                        {task.response}
                      </pre>
                    ) : null}
                    {task.status === "running" || task.status === "queued" ? (
                      <button
                        className="panel-button-secondary mt-4 w-full"
                        onClick={() => cancelTask.mutate(task.id)}
                      >
                        Cancel
                      </button>
                    ) : null}
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}
