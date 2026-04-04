const envBase = import.meta.env.VITE_API_BASE_URL?.trim();

export function resolveApiBase(): string {
  if (envBase) {
    return envBase.replace(/\/$/, "");
  }

  if (typeof window !== "undefined") {
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;
    return `${protocol}//${hostname}:8000/api`;
  }

  return "http://localhost:8000/api";
}

export function resolveApiRoot(): string {
  return resolveApiBase().replace(/\/api$/, "");
}

export function resolveWsRoot(): string {
  return resolveApiRoot().replace(/^http/, "ws");
}
