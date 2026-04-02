import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useAgentAction, useAgents, useCreateAgent, useDeleteAgent } from "../api/agents";
import { useNodes } from "../api/nodes";
import { useSettings } from "../api/settings";

const emptyForm = {
  node_id: "",
  name: "",
  friendly_name: "",
  slug: "",
  description: "",
  run_mode: "hybrid",
  model: "",
  provider: "",
  api_key_ref: "",
  base_url: "",
  system_prompt: "",
};

function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || "agent";
}

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "stopped") return "text-[var(--text-secondary)]";
  return "text-[var(--warning)]";
}

export function AgentsPage() {
  const { data: nodes } = useNodes();
  const { data: agents } = useAgents();
  const { data: settings } = useSettings();
  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const restartAgent = useAgentAction("restart");

  const [form, setForm] = useState(emptyForm);
  const [nameTouched, setNameTouched] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);

  const activeNodeId = useMemo(() => nodes?.[0]?.id ?? "", [nodes]);

  useEffect(() => {
    setForm((current) => {
      if (current.name || current.friendly_name || current.slug || current.system_prompt || current.description) {
        return current;
      }
      return {
        ...current,
        node_id: current.node_id || activeNodeId,
        model: current.model || settings?.default_model || "",
        provider: current.provider || settings?.default_provider || "",
        api_key_ref: current.api_key_ref || settings?.default_api_key_ref || "",
        base_url: current.base_url || settings?.default_base_url || "",
      };
    });
  }, [activeNodeId, settings]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await createAgent.mutateAsync({
      ...form,
      node_id: form.node_id || activeNodeId,
      enabled_toolsets: [],
      disabled_toolsets: [],
      skills: [],
      team_tags: [],
      soul_md: "# Soul\n\nOperations-first runtime.",
    });
    setForm({
      ...emptyForm,
      node_id: activeNodeId,
      model: settings?.default_model ?? "",
      provider: settings?.default_provider ?? "",
      api_key_ref: settings?.default_api_key_ref ?? "",
      base_url: settings?.default_base_url ?? "",
    });
    setNameTouched(false);
    setSlugTouched(false);
  }

  async function onDelete(agentId: string, agentName: string) {
    const confirmed = window.confirm(`Delete agent "${agentName}"? This will remove its workspace and task history.`);
    if (!confirmed) {
      return;
    }
    try {
      await deleteAgent.mutateAsync(agentId);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Delete request failed. Reload the page and try again.";
      window.alert(message);
    }
  }

  return (
    <div className="grid gap-6">
      <div className="grid gap-6 xl:grid-cols-[0.72fr_1.28fr]">
      <section className="panel-frame p-6">
        <div className="space-y-3">
          <p className="panel-label">Create agent</p>
          <h2 className="text-3xl text-[var(--text-display)]">Local runtime bootstrap</h2>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            Start with a local agent profile. Workspaces, config files and runtime status are
            provisioned automatically.
          </p>
        </div>

        <form className="mt-8 space-y-5" onSubmit={onSubmit}>
          <label className="panel-field">
            <span className="panel-label">Node</span>
            <select
              value={form.node_id || activeNodeId}
              onChange={(event) => setForm((current) => ({ ...current, node_id: event.target.value }))}
            >
              {(nodes ?? []).map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                </option>
              ))}
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">Friendly name</span>
            <input
              value={form.friendly_name}
              onChange={(event) =>
                setForm((current) => {
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
              placeholder="What humans should call this agent"
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Name</span>
            <input
              value={form.name}
              onChange={(event) => {
                const nextName = event.target.value;
                setNameTouched(true);
                setForm((current) => {
                  const next = { ...current, name: nextName };
                  if (!slugTouched && !current.friendly_name.trim()) {
                    next.slug = slugify(nextName.trim());
                  }
                  return next;
                });
              }}
              placeholder="Technical operator name"
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Slug</span>
            <input
              value={form.slug}
              onChange={(event) => {
                setSlugTouched(true);
                setForm((current) => ({ ...current, slug: event.target.value }));
              }}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Model</span>
            <input
              value={form.model}
              onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
              placeholder={settings?.default_model ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Provider</span>
            <input
              value={form.provider}
              onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value }))}
              placeholder={settings?.default_provider ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Secret ref</span>
            <input
              value={form.api_key_ref}
              onChange={(event) => setForm((current) => ({ ...current, api_key_ref: event.target.value }))}
              placeholder={settings?.default_api_key_ref ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">Base URL</span>
            <input
              value={form.base_url}
              onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))}
              placeholder={settings?.default_base_url ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">System prompt</span>
            <textarea
              rows={4}
              value={form.system_prompt}
              onChange={(event) =>
                setForm((current) => ({ ...current, system_prompt: event.target.value }))
              }
            />
          </label>

          <button type="submit" className="panel-button-primary w-full" disabled={createAgent.isPending}>
            {createAgent.isPending ? "[LOADING]" : "Create agent"}
          </button>
        </form>
      </section>

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Fleet inventory</p>
            <h2 className="mt-2 text-3xl text-[var(--text-display)]">Agent matrix</h2>
          </div>
          <p className="panel-label">{agents?.length ?? 0} registered</p>
        </div>
        <div className="mt-2">
          {(agents ?? []).map((agent) => (
            <article key={agent.id} className="grid gap-5 border-b border-[var(--border)] py-5 xl:grid-cols-[1fr_auto]">
              <div className="grid gap-5 md:grid-cols-[1.1fr_0.8fr_0.8fr]">
                <div>
                  <p className="panel-label">{agent.slug}</p>
                  <Link to={`/agents/${agent.id}`} className="mt-2 block text-xl text-[var(--text-display)]">
                    {agent.friendly_name || agent.name}
                  </Link>
                  {agent.friendly_name && agent.friendly_name !== agent.name ? (
                    <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.name}</p>
                  ) : null}
                  <p className="mt-3 text-sm text-[var(--text-secondary)]">{agent.description ?? "No description"}</p>
                </div>
                <div>
                  <p className="panel-label">Runtime</p>
                  <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.model}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {agent.provider}
                  </p>
                </div>
                <div>
                  <p className="panel-label">Status</p>
                  <p className={`mt-2 text-sm uppercase tracking-[0.1em] ${statusTone(agent.status)}`}>
                    {agent.status}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    tasks {agent.total_tasks} / tokens {agent.total_tokens_used}
                  </p>
                </div>
              </div>
              <div className="grid min-w-[18rem] gap-2 md:grid-cols-2">
                <button className="panel-button-secondary w-full" onClick={() => startAgent.mutate(agent.id)}>
                  Start
                </button>
                <button className="panel-button-secondary w-full" onClick={() => stopAgent.mutate(agent.id)}>
                  Stop
                </button>
                <button className="panel-button-secondary w-full" onClick={() => restartAgent.mutate(agent.id)}>
                  Restart
                </button>
                <button
                  className="panel-button-secondary w-full border-[var(--accent)] text-[var(--accent)]"
                  onClick={() => onDelete(agent.id, agent.name)}
                  disabled={deleteAgent.isPending}
                >
                  Delete agent
                </button>
              </div>
            </article>
          ))}
        </div>
      </section>
      </div>
    </div>
  );
}
