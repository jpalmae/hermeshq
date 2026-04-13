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
  variantClass = "",
  className = "",
}: {
  agent: Pick<Agent, "friendly_name" | "name" | "avatar_url" | "has_avatar">;
  sizeClass?: string;
  roundedClass?: string;
  variantClass?: string;
  className?: string;
}) {
  const avatarUrl = resolveAssetUrl(agent.avatar_url);
  const visualClass = `${sizeClass} ${roundedClass} ${variantClass} border border-[var(--border-visible)] ${className}`.trim();

  if (avatarUrl) {
    return (
      <img
        src={avatarUrl}
        alt={agent.friendly_name || agent.name}
        className={`${visualClass} object-cover`.trim()}
      />
    );
  }

  return (
    <div
      className={`${visualClass} grid place-items-center bg-[var(--org-icon-background)] text-[var(--text-display)]`.trim()}
    >
      <span className="font-mono text-sm uppercase tracking-[0.14em]">{initials(agent)}</span>
    </div>
  );
}
