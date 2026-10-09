import { useI18n } from "../../lib/i18n";
import { useEnrolledDevices } from "../../api/enrollment";
import type { Agent } from "../../types/api";
import { SectionShell } from "./SectionShell";

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const deltaMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(deltaMs / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

const STATUS_COLORS: Record<string, string> = {
  active: "text-emerald-600 dark:text-emerald-400",
  pending: "text-amber-600 dark:text-amber-400",
  revoked: "text-red-600 dark:text-red-400",
  stale: "text-amber-600 dark:text-amber-400",
};

export function DevicesSection({
  agent,
  isOpen,
  onToggle,
}: {
  agent: Agent;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const { data: devices, isLoading } = useEnrolledDevices(agent.id);

  const active = (devices ?? []).filter((d) => d.status === "active");
  const total = devices?.length ?? 0;

  return (
    <SectionShell
      eyebrow={t("agent.desktopDevices")}
      title={t("agent.desktopDevicesTitle")}
      meta={t("agent.desktopDevicesMeta", { total, active: active.length })}
      isOpen={isOpen}
      onToggle={onToggle}
    >
      <div className="space-y-3 pt-4">
        {isLoading ? (
          <p className="text-sm text-[var(--text-secondary)]">…</p>
        ) : total === 0 ? (
          <p className="text-sm text-[var(--text-secondary)]">{t("agent.desktopDevicesEmpty")}</p>
        ) : (
          <ul className="divide-y divide-[var(--border)]">
            {(devices ?? []).map((device) => (
              <li key={device.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-3">
                <span className="text-sm font-semibold">{device.name}</span>
                <span className={`text-xs font-semibold uppercase tracking-wide ${STATUS_COLORS[device.status] ?? ""}`}>
                  {device.status}
                </span>
                <span className="text-xs text-[var(--text-secondary)]">
                  {t("agent.deviceFailMode")}: <span className="font-mono">{device.guard_fail_mode}</span>
                </span>
                <span className="text-xs text-[var(--text-secondary)]">
                  {t("agent.deviceHeartbeat")}: {relativeTime(device.last_heartbeat)}
                </span>
                <span className="text-xs text-[var(--text-secondary)]">
                  {t("agent.deviceLastSync")}: {relativeTime(device.last_sync_at)}
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel-sunken,var(--panel))] p-4">
          <p className="panel-label">{t("agent.desktopEnrollHint")}</p>
          <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
            {t("agent.desktopEnrollHintBody")}{" "}
            <a
              className="font-semibold text-[var(--accent,#3b5bfd)] underline underline-offset-2"
              href={`/v2/agents/${agent.id}?tab=devices`}
            >
              {t("agent.desktopEnrollHintLink")}
            </a>
          </p>
        </div>
      </div>
    </SectionShell>
  );
}
