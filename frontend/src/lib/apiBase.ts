const envBase = import.meta.env.VITE_API_BASE_URL?.trim();

export function resolveApiBase(): string {
  if (envBase) {
    return envBase.replace(/\/$/, "");
  }

  if (typeof window !== "undefined") {
    return "/api";
  }

  return "http://localhost:8000/api";
}

export function resolveApiRoot(): string {
  const apiBase = resolveApiBase();
  if (/^https?:\/\//.test(apiBase)) {
    return apiBase.replace(/\/api$/, "");
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return "http://localhost:8000";
}

export function resolveWsRoot(): string {
  if (typeof window !== "undefined") {
    return window.location.origin.replace(/^http/, "ws");
  }
  return "ws://localhost:8000";
}
