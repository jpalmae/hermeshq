import { useNodes } from "../api/nodes";

export function NodesPage() {
  const { data: nodes } = useNodes();

  return (
    <div className="panel-frame p-6">
      <div className="border-b border-[var(--border)] pb-4">
        <p className="panel-label">Nodes</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">Runtime inventory</h2>
      </div>
      <div className="mt-2">
        {(nodes ?? []).map((node) => (
          <article key={node.id} className="grid gap-4 border-b border-[var(--border)] py-5 md:grid-cols-4">
            <div>
              <p className="panel-label">{node.node_type}</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{node.name}</p>
            </div>
            <div>
              <p className="panel-label">Hostname</p>
              <p className="mt-2 text-sm text-[var(--text-primary)]">{node.hostname}</p>
            </div>
            <div>
              <p className="panel-label">Capacity</p>
              <p className="mt-2 text-sm text-[var(--text-primary)]">{node.max_agents} agents</p>
            </div>
            <div className="text-left md:text-right">
              <p className="panel-label">Status</p>
              <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--success)]">
                {node.status}
              </p>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

