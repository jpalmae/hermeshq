import { resolveApiBase } from "../lib/apiBase";

const BASE = `${resolveApiBase()}/oidc-providers`;

export interface OidcProviderRead {
  id: string;
  slug: string;
  name: string;
  client_id: string;
  discovery_url: string;
  scopes: string;
  enabled: boolean;
  auto_provision: boolean;
  allowed_domains: string | null;
  icon_slug: string | null;
}

export interface OidcProviderCreate {
  slug: string;
  name: string;
  client_id: string;
  client_secret: string;
  discovery_url: string;
  scopes?: string;
  enabled?: boolean;
  auto_provision?: boolean;
  allowed_domains?: string | null;
  icon_slug?: string | null;
}

export interface OidcProviderUpdate {
  name?: string;
  client_id?: string;
  client_secret?: string;
  discovery_url?: string;
  scopes?: string;
  enabled?: boolean;
  auto_provision?: boolean;
  allowed_domains?: string | null;
  icon_slug?: string | null;
}

const authHeaders = () => {
  const token = localStorage.getItem("hermeshq.token");
  return { Authorization: `Bearer ${token}` };
};

export async function listOidcProviders(): Promise<OidcProviderRead[]> {
  const res = await fetch(BASE, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to list OIDC providers");
  return res.json();
}

export async function createOidcProvider(data: OidcProviderCreate): Promise<OidcProviderRead> {
  const res = await fetch(BASE, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create OIDC provider");
  return res.json();
}

export async function updateOidcProvider(
  id: string,
  data: OidcProviderUpdate,
): Promise<OidcProviderRead> {
  const res = await fetch(`${BASE}/${id}`, {
    method: "PATCH",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update OIDC provider");
  return res.json();
}

export async function deleteOidcProvider(id: string): Promise<void> {
  const res = await fetch(`${BASE}/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete OIDC provider");
}
