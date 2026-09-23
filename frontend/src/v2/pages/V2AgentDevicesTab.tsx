import { useState } from "react";

import { useEnrollDevice, useEnrolledDevices, useRevokeDevice } from "../../api/enrollment";
import type { Agent } from "../../types/api";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const deltaMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(deltaMs / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function V2AgentDevicesTab({ agent, isAdmin }: { agent: Agent; isAdmin: boolean }) {
  const { t } = useI18n();
  const { data: devices, isLoading } = useEnrolledDevices(agent.id);
  const enrollDevice = useEnrollDevice(agent.id);
  const revokeDevice = useRevokeDevice(agent.id);
  const [deviceName, setDeviceName] = useState("");
  const [failMode, setFailMode] = useState("fail-open");
  const [enrollResult, setEnrollResult] = useState<{ enroll_token: string; id: string } | null>(null);
  const [origin, setOrigin] = useState("");

  async function handleEnroll() {
    try {
      setOrigin(window.location.origin);
      const result = await enrollDevice.mutateAsync({
        device_name: deviceName || "New device",
        guard_fail_mode: failMode,
      });
      setEnrollResult({ enroll_token: result.enroll_token, id: result.id });
      v2toast.success(t("v2.deviceEnrolled"));
    } catch (e) {
      v2toast.error(extractErrorMessage(e, t("v2.deviceEnrollFailed")));
    }
  }

  async function handleRevoke(deviceId: string) {
    try {
      await revokeDevice.mutateAsync(deviceId);
      v2toast.success(t("v2.deviceRevoked"));
    } catch (e) {
      v2toast.error(extractErrorMessage(e, t("v2.deviceRevokeFailed")));
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <section className="v2-card">
        <div className="v2-card-header">
          <h2 className="v2-card-title">{t("v2.enrolledDevices")}</h2>
        </div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {isLoading ? <p style={{ color: "var(--v2-text-muted)" }}>…</p> : null}
          {!isLoading && (devices ?? []).length === 0 ? (
            <p style={{ color: "var(--v2-text-muted)" }}>{t("v2.noEnrolledDevices")}</p>
          ) : null}
          {(devices ?? []).map((device) => (
            <div
              key={device.id}
              className="v2-list-row"
              style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 10px", borderRadius: 8, background: "var(--v2-bg-sunken)" }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <strong style={{ fontSize: 13 }}>{device.name}</strong>
                  <span
                    className="v2-badge"
                    style={{
                      fontSize: 10,
                      padding: "2px 6px",
                      borderRadius: 4,
                      background:
                        device.status === "active"
                          ? "var(--v2-success-bg, rgba(34,197,94,.15))"
                          : "var(--v2-bg-sunken)",
                      color: device.status === "active" ? "var(--v2-success, #22c55e)" : "var(--v2-text-muted)",
                    }}
                  >
                    {device.status}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 14, fontSize: 11.5, color: "var(--v2-text-muted)", marginTop: 3 }}>
                  <span>fail-mode: {device.guard_fail_mode}</span>
                  <span>heartbeat: {relativeTime(device.last_heartbeat)}</span>
                  <span>sync: {relativeTime(device.last_sync_at)}</span>
                  {device.hermes_version ? <span>hermes {device.hermes_version}</span> : null}
                </div>
              </div>
              {isAdmin && device.status !== "revoked" ? (
                <button
                  className="v2-btn v2-btn-danger"
                  style={{ fontSize: 11, padding: "3px 10px" }}
                  onClick={() => void handleRevoke(device.id)}
                  disabled={revokeDevice.isPending}
                >
                  {t("v2.revoke")}
                </button>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {isAdmin ? (
        <section className="v2-card">
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.enrollNewDevice")}</h2>
          </div>
          <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="v2-input"
                placeholder={t("v2.deviceName")}
                value={deviceName}
                onChange={(e) => setDeviceName(e.target.value)}
              />
              <select className="v2-select" value={failMode} onChange={(e) => setFailMode(e.target.value)} style={{ maxWidth: 180 }}>
                <option value="fail-open">fail-open</option>
                <option value="fail-closed">fail-closed</option>
                <option value="fail-grace:120">fail-grace:120s</option>
              </select>
              <button className="v2-btn v2-btn-primary" onClick={() => void handleEnroll()} disabled={enrollDevice.isPending}>
                {t("v2.enroll")}
              </button>
            </div>

            {enrollResult ? (
              <div style={{ background: "var(--v2-bg-sunken)", borderRadius: 8, padding: 12, fontSize: 12.5, display: "flex", flexDirection: "column", gap: 8 }}>
                <p style={{ fontWeight: 600 }}>{t("v2.enrollInstructions")}</p>
                <code style={{ whiteSpace: "pre", overflowX: "auto", padding: 8, background: "var(--v2-bg)", borderRadius: 6 }}>
                  {`python enroll_device.py enroll ${origin} \\\n  --agent-id ${agent.id} \\\n  --name "My laptop" --fail-mode ${failMode}`}
                </code>
                <p style={{ color: "var(--v2-text-muted)" }}>{t("v2.enrollActivateHint")}</p>
                <code style={{ overflowX: "auto", padding: 8, background: "var(--v2-bg)", borderRadius: 6, userSelect: "all" }}>
                  {enrollResult.enroll_token}
                </code>
              </div>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
