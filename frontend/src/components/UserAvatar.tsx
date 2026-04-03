import type { User } from "../types/api";
import { resolveAssetUrl } from "../api/settings";

function initials(user: Pick<User, "display_name" | "username">) {
  const source = (user.display_name || user.username || "").trim();
  const parts = source.split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = parts.map((part) => part[0]?.toUpperCase() ?? "").join("");
  return letters || "OP";
}

export function UserAvatar({
  user,
  sizeClass = "h-14 w-14",
  roundedClass = "rounded-full",
  className = "",
}: {
  user: Pick<User, "display_name" | "username" | "avatar_url" | "has_avatar">;
  sizeClass?: string;
  roundedClass?: string;
  className?: string;
}) {
  const avatarUrl = resolveAssetUrl(user.avatar_url);

  if (avatarUrl) {
    return (
      <img
        src={avatarUrl}
        alt={user.display_name || user.username}
        className={`${sizeClass} ${roundedClass} border border-[var(--border-visible)] object-cover ${className}`.trim()}
      />
    );
  }

  return (
    <div
      className={`${sizeClass} ${roundedClass} grid place-items-center border border-[var(--border-visible)] bg-[var(--org-icon-background)] text-[var(--text-display)] ${className}`.trim()}
    >
      <span className="font-mono text-sm uppercase tracking-[0.14em]">{initials(user)}</span>
    </div>
  );
}
