import type { ActivityLogEntry } from "../../types/api";

export const DEFAULT_SECTION_STATE = {
  configuration: false,
  conversation: true,
  integrations: false,
  "m365-scopes": false,
  skills: false,
  ledger: false,
  logs: false,
  workspace: false,
  devices: false,
};

export type SectionKey = keyof typeof DEFAULT_SECTION_STATE;
export type SectionState = typeof DEFAULT_SECTION_STATE;

export type ActivityEntry = ActivityLogEntry & { grouped_count?: number };

export type IdentityForm = { friendly_name: string; name: string; slug: string };

export type FallbackDraft = {
  provider: string | null;
  model: string | null;
  api_key_ref: string | null;
  base_url: string | null;
};

export type AuxiliaryDraft = Record<string, FallbackDraft>;

export type IntegrationResult = {
  success: boolean;
  message: string;
  details?: Record<string, unknown> | null;
};

export function asText(value: unknown) {
  return typeof value === "string" ? value : "";
}

export function groupActivityEntries(entries: ActivityEntry[]) {
  const grouped: ActivityEntry[] = [];
  let index = 0;

  while (index < entries.length) {
    const current = entries[index];
    const eventType = asText(current.event_type);
    const taskId = asText(current.task_id);
    if (eventType !== "agent.output" || !taskId) {
      grouped.push(current);
      index += 1;
      continue;
    }

    const run: ActivityEntry[] = [current];
    let nextIndex = index + 1;
    while (nextIndex < entries.length) {
      const candidate = entries[nextIndex];
      if (
        asText(candidate.event_type) === "agent.output"
        && asText(candidate.task_id) === taskId
      ) {
        run.push(candidate);
        nextIndex += 1;
        continue;
      }
      break;
    }

    if (run.length === 1) {
      grouped.push(current);
    } else {
      grouped.push({
        ...current,
        message: [...run].reverse().map((entry) => asText(entry.message)).join(""),
        grouped_count: run.length,
      });
    }
    index = nextIndex;
  }

  return grouped;
}

export function statusBadgeTone(status: string) {
  if (status === "running") return "border-[color-mix(in_srgb,var(--success)_45%,transparent)] bg-[color-mix(in_srgb,var(--success)_16%,transparent)] text-[var(--success)]";
  if (status === "stopped") return "border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_76%,transparent)] text-[var(--text-secondary)]";
  if (status === "error" || status === "failed") return "border-[color-mix(in_srgb,var(--danger)_45%,transparent)] bg-[color-mix(in_srgb,var(--danger)_14%,transparent)] text-[var(--danger)]";
  return "border-[color-mix(in_srgb,var(--warning)_45%,transparent)] bg-[color-mix(in_srgb,var(--warning)_14%,transparent)] text-[var(--warning)]";
}

export function ledgerChannelLabel(channel: string) {
  return channel
    .replace(/_/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

export function ledgerDirectionLabel(direction: string, t: (key: string) => string) {
  if (direction === "inbound") return t("agent.directionInbound");
  if (direction === "outbound") return t("agent.directionOutbound");
  return t("agent.directionSystem");
}

export function ledgerChannelTone(channel: string) {
  if (channel === "talk_to_agent") {
    return "border-[color-mix(in_srgb,var(--primary)_42%,transparent)] bg-[color-mix(in_srgb,var(--primary)_16%,transparent)] text-[var(--primary)]";
  }
  if (channel === "telegram") {
    return "border-[color-mix(in_srgb,var(--warning)_42%,transparent)] bg-[color-mix(in_srgb,var(--warning)_14%,transparent)] text-[var(--warning)]";
  }
  if (channel === "tui") {
    return "border-[color-mix(in_srgb,var(--success)_42%,transparent)] bg-[color-mix(in_srgb,var(--success)_14%,transparent)] text-[var(--success)]";
  }
  if (channel === "schedule") {
    return "border-[color-mix(in_srgb,var(--text-secondary)_55%,transparent)] bg-[color-mix(in_srgb,var(--surface)_76%,transparent)] text-[var(--text-secondary)]";
  }
  if (channel === "agent_to_agent") {
    return "border-[color-mix(in_srgb,var(--accent)_42%,transparent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent)]";
  }
  return "border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)]";
}

export function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || `agent-${Date.now()}`;
}

export function formatHermesVersionLabel(version: string | null | undefined, detectedVersion: string | null | undefined) {
  if (!version || version === "bundled") {
    return detectedVersion ? `Bundled runtime (${detectedVersion})` : "Bundled runtime";
  }
  return detectedVersion ? `${version} (${detectedVersion})` : version;
}

export const approvalModeOptions = [
  { value: "inherit", labelKey: "agent.interactionModeInherit" },
  { value: "off", labelKey: "agent.approvalModeOff" },
  { value: "on-request", labelKey: "agent.approvalModeOnRequest" },
  { value: "on-failure", labelKey: "agent.approvalModeOnFailure" },
];

export const toolProgressModeOptions = [
  { value: "inherit", labelKey: "agent.interactionModeInherit" },
  { value: "on", labelKey: "agent.toolProgressModeOn" },
  { value: "off", labelKey: "agent.toolProgressModeOff" },
];

export const gatewayNotificationsModeOptions = [
  { value: "inherit", labelKey: "agent.interactionModeInherit" },
  { value: "all", labelKey: "agent.gatewayNotificationsModeAll" },
  { value: "result", labelKey: "agent.gatewayNotificationsModeResult" },
  { value: "off", labelKey: "agent.gatewayNotificationsModeOff" },
];
