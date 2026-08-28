import { useState } from "react";
import { Link } from "react-router-dom";

import { usePermissionPolicies, useTestPermission } from "../../api/permissionPolicies";
import { useUpdateAgent } from "../../api/agents";
import type { Agent } from "../../types/api";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

export function V2AgentPermissionsTab({ agent, isAdmin }: { agent: Agent; isAdmin: boolean }) {
  const { t } = useI18n();
  const { data: policies } = usePermissionPolicies();
  const testPermission = useTestPermission();
  const updateAgent = useUpdateAgent();
  const [testTool, setTestTool] = useState("bash");
  const [testInput, setTestInput] = useState("ls -la");
  const [testResult, setTestResult] = useState<{ allowed: boolean; reason: string | null; policy_name: string | null; requires_approval: boolean } | null>(null);

  const primaryId = agent.permission_policy_id ?? "";
  const chainedIds = ((agent as unknown as { permission_policy_ids?: string[] }).permission_policy_ids ?? []);
  const assignedIds = [primaryId, ...chainedIds].filter(Boolean);
  const assignedPolicies = (policies ?? []).filter((p) => assignedIds.includes(p.id));
  const availablePolicies = (policies ?? []).filter((p) => !assignedIds.includes(p.id));

  async function savePolicySets(nextPrimary: string, nextChained: string[]) {
    try {
      await updateAgent.mutateAsync({
        agentId: agent.id,
        payload: { permission_policy_id: nextPrimary || null, permission_policy_ids: nextChained },
      });
      v2toast.success(t("v2.policiesUpdated"));
    } catch (e) {
      v2toast.error(extractErrorMessage(e, t("v2.policyRemoveFailed")));
    }
  }

  async function handleAddPolicy(policyId: string, asPrimary: boolean) {
    const nextChained = asPrimary ? chainedIds : [...chainedIds, policyId];
    const nextPrimary = asPrimary ? policyId : primaryId;
    await savePolicySets(nextPrimary, nextChained);
  }

  async function handleRemovePolicy(policyId: string) {
    const nextPrimary = policyId === primaryId ? (chainedIds[0] ?? "") : primaryId;
    const nextChained = chainedIds.filter((id) => id !== policyId);
    await savePolicySets(nextPrimary, nextChained);
  }

  async function handleTest() {
    try {
      const result = await testPermission.mutateAsync({
        agentId: agent.id,
        tool: testTool,
        input: { command: testInput },
      });
      setTestResult(result);
    } catch (e) {
      v2toast.error(extractErrorMessage(e, t("v2.permissionTestFailed")));
    }
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, alignItems: "start" }}>
      {/* Current policies (chained) */}
      <section className="v2-card">
        <div className="v2-card-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h2 className="v2-card-title">{t("v2.assignedPolicy")}</h2>
          <Link to={`/v2/settings?tab=permissionPolicies`} className="v2-btn v2-btn-ghost" style={{ fontSize: 12 }}>
            {t("v2.editPolicy")}
          </Link>
        </div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {assignedPolicies.length > 0 ? (
            <>
              {assignedPolicies.length > 1 ? (
                <p className="v2-field-hint">{t("v2.chainHint")}</p>
              ) : null}
              {assignedPolicies.map((policy) => (
                <div key={policy.id} style={{ paddingBottom: 12, borderBottom: "1px solid var(--v2-border)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 620, fontSize: 14 }}>{policy.name}</span>
                    {policy.id === primaryId ? (
                      <span className="v2-badge" style={{ background: "var(--v2-accent-subtle)", color: "var(--v2-accent)", fontSize: 10, padding: "2px 6px", borderRadius: 4 }}>
                        {t("v2.primary")}
                      </span>
                    ) : (
                      <span className="v2-badge" style={{ background: "var(--v2-bg-sunken)", color: "var(--v2-text-muted)", fontSize: 10, padding: "2px 6px", borderRadius: 4 }}>
                        {t("v2.chained")}
                      </span>
                    )}
                    {policy.is_system ? (
                      <span className="v2-badge" style={{ background: "var(--v2-bg-sunken)", color: "var(--v2-text-muted)", fontSize: 10, padding: "2px 6px", borderRadius: 4 }}>
                        {t("v2.system")}
                      </span>
                    ) : null}
                    {isAdmin ? (
                      <button
                        className="v2-btn v2-btn-danger"
                        style={{ marginLeft: "auto", fontSize: 11, padding: "3px 8px" }}
                        onClick={() => void handleRemovePolicy(policy.id)}
                        disabled={updateAgent.isPending}
                      >
                        ×
                      </button>
                    ) : null}
                  </div>
                  {policy.description ? (
                    <p style={{ fontSize: 12.5, color: "var(--v2-text-muted)" }}>{policy.description}</p>
                  ) : null}
                  <div style={{ display: "flex", gap: 14, fontSize: 11.5, color: "var(--v2-text-muted)", marginTop: 4 }}>
                    <span>{t("v2.toolsAllowed")}: {policy.tool_rules?.allow?.length ?? 0}</span>
                    <span>{t("v2.commandsDenied")}: {policy.command_rules?.deny?.length ?? 0}</span>
                    <span>{t("v2.pathsProtected")}: {policy.path_rules?.deny_paths?.length ?? 0}</span>
                    {policy.network_rules?.deny_all ? <span style={{ color: "var(--v2-danger)" }}>{t("v2.networkBlocked")}</span> : null}
                  </div>
                </div>
              ))}

              {isAdmin && availablePolicies.length > 0 ? (
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <select className="v2-select" defaultValue="" onChange={(e) => { if (e.target.value) void handleAddPolicy(e.target.value, false); e.currentTarget.value = ""; }} disabled={updateAgent.isPending}>
                    <option value="">{t("v2.addChainedPolicy")}</option>
                    {availablePolicies.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
              ) : null}
            </>
          ) : (
            <div style={{ textAlign: "center", padding: "20px 0", color: "var(--v2-text-muted)" }}>
              <p style={{ marginBottom: 12 }}>{t("v2.noPolicyAssigned")}</p>
              {isAdmin && (policies ?? []).length > 0 ? (
                <select className="v2-select" defaultValue="" onChange={(e) => { if (e.target.value) void handleAddPolicy(e.target.value, true); e.currentTarget.value = ""; }} disabled={updateAgent.isPending}>
                  <option value="">{t("v2.assignPolicy")}</option>
                  {(policies ?? []).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              ) : null}
            </div>
          )}
        </div>
      </section>

      {/* Test panel */}
      <section className="v2-card">
        <div className="v2-card-header">
          <h2 className="v2-card-title">{t("v2.testPermission")}</h2>
        </div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.tool")}</label>
            <select className="v2-select" value={testTool} onChange={(e) => setTestTool(e.target.value)}>
              <option value="read">read</option>
              <option value="write">write</option>
              <option value="edit">edit</option>
              <option value="bash">bash</option>
              <option value="terminal">terminal</option>
              <option value="grep">grep</option>
              <option value="find">find</option>
              <option value="ls">ls</option>
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.input")}</label>
            <input className="v2-input v2-mono" value={testInput} onChange={(e) => setTestInput(e.target.value)} placeholder={testTool === "bash" ? "rm -rf /tmp" : "/workspace/file.txt"} />
          </div>
          <button className="v2-btn v2-btn-primary" onClick={() => void handleTest()} disabled={testPermission.isPending}>
            {testPermission.isPending ? t("v2.testing") : t("v2.test")}
          </button>

          {testResult ? (
            <div style={{ padding: "12px 16px", borderRadius: 8, background: testResult.allowed ? "var(--v2-success-subtle)" : "var(--v2-danger-subtle)", border: `1px solid ${testResult.allowed ? "var(--v2-success)" : "var(--v2-danger)"}` }}>
              <div style={{ fontSize: 13, fontWeight: 620, color: testResult.allowed ? "var(--v2-success)" : "var(--v2-danger)", marginBottom: 4 }}>
                {testResult.allowed ? t("v2.allowed") : t("v2.blocked")}
                {testResult.requires_approval ? ` · ${t("v2.requiresApproval")}` : ""}
              </div>
              {testResult.reason ? (
                <div style={{ fontSize: 12.5, color: "var(--v2-text-secondary)" }}>{testResult.reason}</div>
              ) : null}
              {testResult.policy_name ? (
                <div style={{ fontSize: 11, color: "var(--v2-text-muted)", marginTop: 4 }}>{t("v2.policy")}: {testResult.policy_name}</div>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
