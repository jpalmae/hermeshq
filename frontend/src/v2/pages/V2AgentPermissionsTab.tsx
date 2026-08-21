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

  const assignedPolicy = policies?.find((p) => p.id === agent.permission_policy_id);

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

  async function handleRemovePolicy() {
    try {
      await updateAgent.mutateAsync({ agentId: agent.id, payload: { permission_policy_id: null } });
      v2toast.success(t("v2.policyRemoved"));
    } catch (e) {
      v2toast.error(extractErrorMessage(e, t("v2.policyRemoveFailed")));
    }
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, alignItems: "start" }}>
      {/* Current policy summary */}
      <section className="v2-card">
        <div className="v2-card-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h2 className="v2-card-title">{t("v2.assignedPolicy")}</h2>
          {assignedPolicy ? (
            <Link to={`/v2/settings?tab=permissionPolicies`} className="v2-btn v2-btn-ghost" style={{ fontSize: 12 }}>
              {t("v2.editPolicy")}
            </Link>
          ) : null}
        </div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {assignedPolicy ? (
            <>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={{ fontWeight: 620, fontSize: 15 }}>{assignedPolicy.name}</span>
                  {assignedPolicy.is_system ? (
                    <span className="v2-badge" style={{ background: "var(--v2-accent-subtle)", color: "var(--v2-accent)", fontSize: 11, padding: "2px 8px", borderRadius: 4 }}>
                      {t("v2.system")}
                    </span>
                  ) : null}
                </div>
                {assignedPolicy.description ? (
                  <p style={{ fontSize: 13, color: "var(--v2-text-muted)" }}>{assignedPolicy.description}</p>
                ) : null}
              </div>

              {/* Rules summary */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <RuleSummary label={t("v2.toolsAllowed")} items={assignedPolicy.tool_rules?.allow ?? []} />
                <RuleSummary label={t("v2.commandsDenied")} items={assignedPolicy.command_rules?.deny ?? []} danger />
                <RuleSummary label={t("v2.pathsProtected")} items={assignedPolicy.path_rules?.deny_paths ?? []} danger />
                <div style={{ gridColumn: "1 / -1" }}>
                  <div style={{ fontSize: 11, fontWeight: 620, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--v2-text-muted)", marginBottom: 6 }}>
                    {t("v2.networkAccess")}
                  </div>
                  <div style={{ fontSize: 12.5, color: assignedPolicy.network_rules?.deny_all ? "var(--v2-danger)" : "var(--v2-text-secondary)" }}>
                    {assignedPolicy.network_rules?.deny_all
                      ? `${t("v2.networkBlocked")} — ${(assignedPolicy.network_rules?.allow_domains ?? []).join(", ") || t("v2.noDomains")}`
                      : t("v2.networkOpen")}
                  </div>
                </div>
              </div>

              {isAdmin ? (
                <button className="v2-btn v2-btn-danger" style={{ alignSelf: "flex-start", fontSize: 12.5, marginTop: 4 }} onClick={() => void handleRemovePolicy()} disabled={updateAgent.isPending}>
                  {t("v2.removePolicy")}
                </button>
              ) : null}
            </>
          ) : (
            <div style={{ textAlign: "center", padding: "20px 0", color: "var(--v2-text-muted)" }}>
              <p style={{ marginBottom: 12 }}>{t("v2.noPolicyAssigned")}</p>
              {isAdmin ? (
                <Link to={`/v2/settings?tab=permissionPolicies`} className="v2-btn v2-btn-primary" style={{ fontSize: 12.5 }}>
                  {t("v2.assignPolicy")}
                </Link>
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

function RuleSummary({ label, items, danger }: { label: string; items: string[]; danger?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 620, textTransform: "uppercase", letterSpacing: "0.06em", color: danger ? "var(--v2-danger)" : "var(--v2-text-muted)", marginBottom: 6 }}>
        {label}
      </div>
      {items.length === 0 ? (
        <div style={{ fontSize: 12.5, color: "var(--v2-text-muted)" }}>—</div>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {items.map((item) => (
            <code key={item} style={{ fontSize: 11, padding: "2px 6px", background: "var(--v2-bg-sunken)", borderRadius: 4, fontFamily: "var(--v2-font-mono)" }}>
              {item}
            </code>
          ))}
        </div>
      )}
    </div>
  );
}
