import { useState } from "react";

import {
  useCreatePermissionPolicy,
  useDeletePermissionPolicy,
  usePermissionPolicies,
  useUpdatePermissionPolicy,
  type PermissionPolicy,
} from "../../api/permissionPolicies";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

export function PermissionPoliciesTab() {
  const { t } = useI18n();
  const { data: policies, isLoading } = usePermissionPolicies();
  const createPolicy = useCreatePermissionPolicy();
  const updatePolicy = useUpdatePermissionPolicy();
  const deletePolicy = useDeletePermissionPolicy();
  const [editing, setEditing] = useState<PermissionPolicy | null>(null);
  const [creating, setCreating] = useState(false);

  if (isLoading) {
    return (
      <div style={{ padding: 20 }}>
        <div className="v2-skeleton" style={{ height: 200 }} />
      </div>
    );
  }

  if (creating) {
    return (
      <PolicyEditor
        isNew
        onSave={async (data) => {
          try {
            await createPolicy.mutateAsync(data);
            v2toast.success(t("v2.policyCreated"));
            setCreating(false);
          } catch (e) {
            v2toast.error(extractErrorMessage(e, t("v2.policyCreateFailed")));
          }
        }}
        onCancel={() => setCreating(false)}
      />
    );
  }

  if (editing) {
    return (
      <PolicyEditor
        policy={editing}
        onSave={async (data) => {
          try {
            await updatePolicy.mutateAsync({ id: editing.id, payload: data });
            v2toast.success(t("v2.policyUpdated"));
            setEditing(null);
          } catch (e) {
            v2toast.error(extractErrorMessage(e, t("v2.policyUpdateFailed")));
          }
        }}
        onCancel={() => setEditing(null)}
      />
    );
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div>
          <h2 className="v2-page-title" style={{ fontSize: 20 }}>{t("v2.permissionPolicies")}</h2>
          <p className="v2-page-subtitle">{t("v2.permissionPoliciesDesc")}</p>
        </div>
        <button className="v2-btn v2-btn-primary" onClick={() => setCreating(true)}>
          {t("v2.createPolicy")}
        </button>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {(policies ?? []).map((policy) => (
          <section key={policy.id} className="v2-card">
            <div className="v2-card-body" style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                  <span style={{ fontWeight: 620, fontSize: 15 }}>{policy.name}</span>
                  {policy.is_system ? (
                    <span className="v2-badge" style={{ background: "var(--v2-accent-subtle)", color: "var(--v2-accent)", fontSize: 11, padding: "2px 8px", borderRadius: 4 }}>
                      {t("v2.system")}
                    </span>
                  ) : null}
                </div>
                {policy.description ? (
                  <p style={{ fontSize: 13, color: "var(--v2-text-muted)", marginBottom: 8 }}>{policy.description}</p>
                ) : null}
                <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--v2-text-muted)" }}>
                  <span>{t("v2.toolsAllowed")}: {policy.tool_rules?.allow?.length ?? 0}</span>
                  <span>{t("v2.commandsDenied")}: {policy.command_rules?.deny?.length ?? 0}</span>
                  <span>{t("v2.pathsProtected")}: {policy.path_rules?.deny_paths?.length ?? 0}</span>
                  {policy.network_rules?.deny_all ? <span style={{ color: "var(--v2-danger)" }}>{t("v2.networkBlocked")}</span> : null}
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="v2-btn v2-btn-ghost" style={{ fontSize: 12.5 }} onClick={() => setEditing(policy)}>
                  {t("v2.edit")}
                </button>
                {!policy.is_system ? (
                  <button
                    className="v2-btn v2-btn-danger"
                    style={{ fontSize: 12.5 }}
                    disabled={deletePolicy.isPending}
                    onClick={() => {
                      if (window.confirm(t("v2.deletePolicyConfirm"))) {
                        deletePolicy.mutate(policy.id);
                      }
                    }}
                  >
                    {t("v2.delete")}
                  </button>
                ) : null}
              </div>
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function PolicyEditor({
  policy,
  isNew,
  onSave,
  onCancel,
}: {
  policy?: PermissionPolicy;
  isNew?: boolean;
  onSave: (data: Record<string, unknown>) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const [name, setName] = useState(policy?.name ?? "");
  const [description, setDescription] = useState(policy?.description ?? "");
  const [allowTools, setAllowTools] = useState((policy?.tool_rules?.allow ?? ["*"]).join(", "));
  const [denyCommands, setDenyCommands] = useState((policy?.command_rules?.deny ?? []).join(", "));
  const [denyPaths, setDenyPaths] = useState((policy?.path_rules?.deny_paths ?? []).join(", "));
  const [requireApproval, setRequireApproval] = useState((policy?.approval_rules?.require_approval_for ?? []).join(", "));
  const [denyAllNet, setDenyAllNet] = useState(policy?.network_rules?.deny_all ?? false);

  function buildPayload() {
    return {
      name,
      description: description || null,
      tool_rules: { allow: allowTools.split(",").map((s) => s.trim()).filter(Boolean), deny: [] },
      path_rules: { allow_paths: ["/workspace/**"], deny_paths: denyPaths.split(",").map((s) => s.trim()).filter(Boolean) },
      command_rules: { allow: [], deny: denyCommands.split(",").map((s) => s.trim()).filter(Boolean) },
      network_rules: { allow_domains: [], deny_all: denyAllNet },
      approval_rules: { require_approval_for: requireApproval.split(",").map((s) => s.trim()).filter(Boolean), auto_approve_threshold: "medium" },
    };
  }

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h2 className="v2-page-title" style={{ fontSize: 20 }}>{isNew ? t("v2.newPolicy") : t("v2.editPolicy")}</h2>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, alignItems: "start" }}>
        <section className="v2-card">
          <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.identity")}</h2></div>
          <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.name")}</label>
              <input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} disabled={policy?.is_system} />
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.description")}</label>
              <input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
          </div>
        </section>

        <section className="v2-card">
          <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.toolsRules")}</h2></div>
          <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.allowedTools")} <span style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>(comma-separated, * for all)</span></label>
              <input className="v2-input v2-mono" value={allowTools} onChange={(e) => setAllowTools(e.target.value)} placeholder="read, bash, edit, write, grep, find, ls" />
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.networkAccess")}</label>
              <select className="v2-select" value={denyAllNet ? "blocked" : "open"} onChange={(e) => setDenyAllNet(e.target.value === "blocked")}>
                <option value="open">{t("v2.networkOpen")}</option>
                <option value="blocked">{t("v2.networkBlocked")}</option>
              </select>
            </div>
          </div>
        </section>

        <section className="v2-card">
          <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.commandRules")}</h2></div>
          <div className="v2-card-body">
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.deniedCommands")} <span style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>(comma-separated glob patterns)</span></label>
              <textarea className="v2-textarea" rows={3} value={denyCommands} onChange={(e) => setDenyCommands(e.target.value)} placeholder="rm -rf /, sudo *, curl * | sh" />
            </div>
          </div>
        </section>

        <section className="v2-card">
          <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.pathRules")}</h2></div>
          <div className="v2-card-body">
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.protectedPaths")} <span style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>(comma-separated glob patterns)</span></label>
              <textarea className="v2-textarea" rows={3} value={denyPaths} onChange={(e) => setDenyPaths(e.target.value)} placeholder="**/.env, **/node_modules/**, /etc/**" />
            </div>
          </div>
        </section>

        <section className="v2-card" style={{ gridColumn: "1 / -1" }}>
          <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.approvalRules")}</h2></div>
          <div className="v2-card-body">
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.requireApprovalFor")} <span style={{ fontSize: 11, color: "var(--v2-text-muted)" }}>(comma-separated, format: bash:sudo * or write:/system/**)</span></label>
              <input className="v2-input v2-mono" value={requireApproval} onChange={(e) => setRequireApproval(e.target.value)} placeholder="bash:sudo *, bash:rm *" />
            </div>
          </div>
        </section>
      </div>

      <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
        <button
          className="v2-btn v2-btn-primary"
          disabled={!name.trim()}
          onClick={() => onSave(buildPayload())}
        >
          {t("v2.save")}
        </button>
        <button className="v2-btn v2-btn-ghost" onClick={onCancel}>
          {t("v2.cancel")}
        </button>
      </div>
    </div>
  );
}
