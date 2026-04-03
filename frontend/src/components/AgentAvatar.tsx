import type { Agent } from "../types/api";
import { resolveAssetUrl } from "../api/settings";

function initials(agent: Pick<Agent, "friendly_name" | "name">) {
  const source = (agent.friendly_name || agent.name || "").trim();
  const parts = source.split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = parts.map((part) => part[0]?.toUpperCase() ?? "").join("");
  return letters || "AG";
}

export function AgentAvatar({
  agent,
  sizeClass = "h-28 w-28",
  roundedClass = "rounded-full",
  className = "",
}: {
  agent: Pick<Agent, "friendly_name" | "name" | "avatar_url" | "has_avatar">;
  sizeClass?: string;
  roundedClass?: string;
  className?: string;
}) {
  const avatarUrl = resolveAssetUrl(agent.avatar_url);

  if (avatarUrl) {
    return (
      <img
        src={avatarUrl}
        alt={agent.friendly_name || agent.name}
        className={`${sizeClass} ${roundedClass} border border-[var(--border-visible)] object-cover ${className}`.trim()}
      />
    );
  }

  return (
    <div
      className={`${sizeClass} ${roundedClass} grid place-items-center border border-[var(--border-visible)] bg-[var(--org-icon-background)] text-[var(--text-display)] ${className}`.trim()}
    >
      <span className="font-mono text-sm uppercase tracking-[0.14em]">{initials(agent)}</span>
    </div>
  );
}
