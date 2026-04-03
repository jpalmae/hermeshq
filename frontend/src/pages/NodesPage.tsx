import { useNodes } from "../api/nodes";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

export function NodesPage() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: nodes } = useNodes(isAdmin);

  if (currentUser && !isAdmin) {
    return (
      <section className="panel-frame p-6">
        <p className="panel-label">{t("nav.nodes")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("nodes.adminRequired")}</h2>
        <p className="mt-4 max-w-[42rem] text-sm leading-6 text-[var(--text-secondary)]">
          {t("nodes.adminCopy")}
        </p>
      </section>
    );
  }

  return (
    <div className="panel-frame p-6">
      <div className="border-b border-[var(--border)] pb-4">
        <p className="panel-label">{t("nav.nodes")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("nodes.runtimeInventory")}</h2>
      </div>
      <div className="mt-2">
        {(nodes ?? []).map((node) => (
          <article key={node.id} className="grid gap-4 border-b border-[var(--border)] py-5 md:grid-cols-4">
            <div>
              <p className="panel-label">{node.node_type}</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{node.name}</p>
            </div>
            <div>
              <p className="panel-label">{t("nodes.hostname")}</p>
              <p className="mt-2 text-sm text-[var(--text-primary)]">{node.hostname}</p>
            </div>
            <div>
              <p className="panel-label">{t("nodes.capacity")}</p>
              <p className="mt-2 text-sm text-[var(--text-primary)]">{t("nodes.agentsCount", { count: node.max_agents })}</p>
            </div>
            <div className="text-left md:text-right">
              <p className="panel-label">{t("nodes.status")}</p>
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
