import { useState, type FormEvent } from "react";
import { useI18n } from "../../lib/i18n";

interface ExternalAccessTabProps {
  agents: any[] | undefined;
  mcpAccessTokens: any[] | undefined;
  createMcpAccessToken: any;
  updateMcpAccessToken: any;
  revokeMcpAccessToken: any;
}

export default function ExternalAccessTab({
  agents,
  mcpAccessTokens,
  createMcpAccessToken,
  updateMcpAccessToken,
  revokeMcpAccessToken,
}: ExternalAccessTabProps) {
  const { t } = useI18n();

  const [mcpTokenName, setMcpTokenName] = useState("");
  const [mcpTokenDescription, setMcpTokenDescription] = useState("");
  const [mcpClientName, setMcpClientName] = useState("");
  const [mcpAllowedAgentIds, setMcpAllowedAgentIds] = useState<string[]>([]);
  const [mcpScopes, setMcpScopes] = useState<string[]>(["agents:list", "agents:invoke", "tasks:read"]);
  const [mcpExpiresAt, setMcpExpiresAt] = useState("");
  const [lastCreatedMcpToken, setLastCreatedMcpToken] = useState<string | null>(null);

  function toggleMcpAgent(agentId: string) {
    setMcpAllowedAgentIds((current) =>
      current.includes(agentId) ? current.filter((id) => id !== agentId) : [...current, agentId],
    );
  }

  function toggleMcpScope(scope: string) {
    setMcpScopes((current) =>
      current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope],
    );
  }

  async function submitMcpAccessToken(event: FormEvent) {
    event.preventDefault();
    const result = await createMcpAccessToken.mutateAsync({
      name: mcpTokenName.trim(),
      description: mcpTokenDescription.trim() || null,
      client_name: mcpClientName.trim() || null,
      allowed_agent_ids: mcpAllowedAgentIds,
      scopes: mcpScopes,
      expires_at: mcpExpiresAt ? new Date(mcpExpiresAt).toISOString() : null,
    });
    setLastCreatedMcpToken(result.token);
    setMcpTokenName("");
    setMcpTokenDescription("");
    setMcpClientName("");
    setMcpAllowedAgentIds([]);
    setMcpScopes(["agents:list", "agents:invoke", "tasks:read"]);
    setMcpExpiresAt("");
  }

  return (
    <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
      <form className="panel-frame p-6" onSubmit={submitMcpAccessToken}>
        <p className="panel-label">Enterprise MCP</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Create MCP credential</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Generate a scoped token for external MCP clients. The token is shown once and can only access the agents selected here.
        </p>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">Credential name</span>
            <input value={mcpTokenName} onChange={(event) => setMcpTokenName(event.target.value)} placeholder="Claude Code finance team" />
          </label>
          <label className="panel-field">
            <span className="panel-label">Client name</span>
            <input value={mcpClientName} onChange={(event) => setMcpClientName(event.target.value)} placeholder="Claude Code, Codex, Desktop" />
          </label>
          <label className="panel-field">
            <span className="panel-label">Description</span>
            <textarea value={mcpTokenDescription} onChange={(event) => setMcpTokenDescription(event.target.value)} placeholder="Who owns this access and why it exists" />
          </label>
          <label className="panel-field">
            <span className="panel-label">Expires at</span>
            <input type="datetime-local" value={mcpExpiresAt} onChange={(event) => setMcpExpiresAt(event.target.value)} />
          </label>
          <div>
            <p className="panel-label">Scopes</p>
            <div className="mt-3 grid gap-2">
              {[
                ["agents:list", "List authorized agents"],
                ["agents:invoke", "Send tasks to agents"],
                ["tasks:read", "Read task status and responses"],
              ].map(([scope, label]) => (
                <label key={scope} className="backup-option">
                  <input type="checkbox" checked={mcpScopes.includes(scope)} onChange={() => toggleMcpScope(scope)} />
                  <span className="backup-option-copy">{label}</span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <p className="panel-label">Authorized agents</p>
            <div className="mt-3 max-h-[18rem] space-y-2 overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)]/40 p-3">
              {(agents ?? []).filter((agent) => !agent.is_archived).map((agent) => (
                <label key={agent.id} className="backup-option">
                  <input type="checkbox" checked={mcpAllowedAgentIds.includes(agent.id)} onChange={() => toggleMcpAgent(agent.id)} />
                  <span className="backup-option-copy">
                    {(agent.friendly_name || agent.name || agent.slug)} · {agent.status}
                  </span>
                </label>
              ))}
              {!agents?.length ? (
                <p className="text-sm text-[var(--text-secondary)]">No agents available.</p>
              ) : null}
            </div>
          </div>
          <button
            className="panel-button-primary w-full"
            type="submit"
            disabled={createMcpAccessToken.isPending || !mcpTokenName.trim() || !mcpAllowedAgentIds.length || !mcpScopes.length}
          >
            {createMcpAccessToken.isPending ? "Creating credential..." : "Create MCP credential"}
          </button>
          {lastCreatedMcpToken ? (
            <div className="rounded-2xl border border-[var(--warning)]/40 bg-[var(--surface-raised)] p-4">
              <p className="panel-label">Token shown once</p>
              <p className="mt-2 break-all font-mono text-xs text-[var(--text-display)]">{lastCreatedMcpToken}</p>
              <p className="mt-3 text-xs leading-5 text-[var(--text-secondary)]">
                Configure the MCP client with endpoint <span className="font-mono">/mcp</span> and this bearer token.
              </p>
            </div>
          ) : null}
        </div>
      </form>

      <section className="panel-frame p-6">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Active credentials</p>
            <h2 className="mt-2 text-2xl text-[var(--text-display)]">MCP access registry</h2>
          </div>
          <p className="panel-label">{mcpAccessTokens?.length ?? 0} credentials</p>
        </div>
        <div className="mt-5 space-y-4">
          {(mcpAccessTokens ?? []).map((token) => (
            <article key={token.id} className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="panel-label">{token.client_name || "MCP client"}</p>
                  <h3 className="mt-1 text-lg text-[var(--text-display)]">{token.name}</h3>
                  <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{token.description || "No description"}</p>
                </div>
                <span className={`rounded-full border px-3 py-1 text-xs ${token.is_active ? "border-[var(--success)]/40 text-[var(--success)]" : "border-[var(--danger)]/40 text-[var(--danger)]"}`}>
                  {token.is_active ? "active" : "revoked"}
                </span>
              </div>
              <div className="mt-4 grid gap-3 text-sm text-[var(--text-secondary)] sm:grid-cols-2">
                <p><strong>Prefix:</strong> <span className="font-mono">{token.token_prefix}</span></p>
                <p><strong>Agents:</strong> {token.allowed_agent_ids.length}</p>
                <p><strong>Scopes:</strong> {token.scopes.join(", ")}</p>
                <p><strong>Last used:</strong> {token.last_used_at ? new Date(token.last_used_at).toLocaleString() : "never"}</p>
                <p><strong>Expires:</strong> {token.expires_at ? new Date(token.expires_at).toLocaleString() : "never"}</p>
              </div>
              <div className="mt-4 flex flex-wrap gap-3">
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void updateMcpAccessToken.mutateAsync({ tokenId: token.id, payload: { is_active: !token.is_active } })}
                >
                  {token.is_active ? "Disable" : "Enable"}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => {
                    if (window.confirm(`Revoke MCP credential ${token.name}?`)) {
                      void revokeMcpAccessToken.mutateAsync(token.id);
                    }
                  }}
                >
                  Revoke
                </button>
              </div>
            </article>
          ))}
          {!mcpAccessTokens?.length ? (
            <p className="text-sm text-[var(--text-secondary)]">No MCP credentials have been created yet.</p>
          ) : null}
        </div>
      </section>
    </section>
  );
}
