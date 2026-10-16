import { useState, type FormEvent } from "react";
import type { UseMutationResult } from "@tanstack/react-query";
import { useUsers } from "../../api/users";
import type { Agent, PublicChatApiKey, PublicChatApiKeyCreated } from "../../types/api";

interface PublicChatKeysTabProps {
  agents: Agent[] | undefined;
  publicChatKeys: PublicChatApiKey[] | undefined;
  createPublicChatKey: UseMutationResult<PublicChatApiKeyCreated, Error, Record<string, unknown>>;
  updatePublicChatKey: UseMutationResult<PublicChatApiKey, Error, { keyId: string; payload: Record<string, unknown> }>;
  deletePublicChatKey: UseMutationResult<string, Error, string>;
  permanentlyDeletePublicChatKey: UseMutationResult<string, Error, string>;
}

export default function PublicChatKeysTab({
  agents,
  publicChatKeys,
  createPublicChatKey,
  updatePublicChatKey,
  deletePublicChatKey,
  permanentlyDeletePublicChatKey,
}: PublicChatKeysTabProps) {

  const [label, setLabel] = useState("");
  const [agentId, setAgentId] = useState("");
  const [allowedDomains, setAllowedDomains] = useState("");
  const [requestsPerMonth, setRequestsPerMonth] = useState(1000);
  const [tokensPerMonth, setTokensPerMonth] = useState(100_000);
  const [boundUserId, setBoundUserId] = useState("");
  const [trustSenderId, setTrustSenderId] = useState(false);
  const { data: users } = useUsers();
  const [lastCreatedKey, setLastCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [snippetCopied, setSnippetCopied] = useState(false);

  // Widget appearance (for creation)
  const [widgetTitle, setWidgetTitle] = useState("");
  const [widgetTheme, setWidgetTheme] = useState<string>("auto");
  const [widgetAccent, setWidgetAccent] = useState("#6366f1");
  const [widgetPosition, setWidgetPosition] = useState<string>("right");

  // Registry UI state
  const [expandedSnippet, setExpandedSnippet] = useState<string | null>(null);
  const [snippetKeyInput, setSnippetKeyInput] = useState<Record<string, string>>({});
  const [editingKeyId, setEditingKeyId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<{
    widget_title: string;
    widget_theme: string;
    widget_accent: string;
    widget_position: string;
  }>({ widget_title: "", widget_theme: "auto", widget_accent: "#6366f1", widget_position: "right" });

  const activeAgents = (agents ?? []).filter((a) => !a.is_archived);

  const widgetCustomized =
    widgetTitle.trim() !== "" || widgetTheme !== "auto" || widgetAccent !== "#6366f1" || widgetPosition !== "right";

  function assignableUsers(forAgentId: string) {
    return (users ?? []).filter((u) => u.is_active && u.assigned_agent_ids.includes(forAgentId));
  }

  function userDisplayName(id: string | null): string {
    if (!id) return "—";
    const user = (users ?? []).find((u) => u.id === id);
    return user ? user.display_name || user.username : id;
  }

  function buildSnippet(apiKeyPrefix: string, appearance: { widget_title: string | null; widget_theme: string; widget_accent: string; widget_position: string }, rawKey?: string): string {
    const key = rawKey || `${apiKeyPrefix}...`;
    const attrs = [
      `  src="${window.location.origin}/api/public/chat/widget.js"`,
      `  data-api-key="${key}"`,
    ];
    if (appearance.widget_title?.trim()) attrs.push(`  data-title="${appearance.widget_title.trim()}"`);
    if (appearance.widget_theme !== "auto") attrs.push(`  data-theme="${appearance.widget_theme}"`);
    if (appearance.widget_accent !== "#6366f1") attrs.push(`  data-accent-color="${appearance.widget_accent}"`);
    if (appearance.widget_position !== "right") attrs.push(`  data-position="${appearance.widget_position}"`);
    return `<script\n${attrs.join("\n")}>\n</script>`;
  }

  function buildSnippetForNew(apiKey: string): string {
    return buildSnippet("", { widget_title: widgetTitle, widget_theme: widgetTheme, widget_accent: widgetAccent, widget_position: widgetPosition }, apiKey);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const domains = allowedDomains.split(",").map((d) => d.trim()).filter(Boolean);
    const result = await createPublicChatKey.mutateAsync({
      label: label.trim(),
      agent_id: agentId,
      allowed_domains: domains,
      requests_per_month: requestsPerMonth,
      tokens_per_month: tokensPerMonth,
      widget_title: widgetTitle.trim() || null,
      widget_theme: widgetTheme,
      widget_accent: widgetAccent,
      widget_position: widgetPosition,
      bound_user_id: boundUserId || null,
      trust_sender_id: trustSenderId,
    });
    setLastCreatedKey(result.raw_key);
    setBoundUserId("");
    setTrustSenderId(false);
    setCopied(false);
    setSnippetCopied(false);
    setLabel("");
    setAgentId("");
    setAllowedDomains("*");
    setRequestsPerMonth(1000);
    setTokensPerMonth(100_000);
  }

  function copyKey() {
    if (!lastCreatedKey) return;
    navigator.clipboard.writeText(lastCreatedKey);
    setCopied(true);
  }

  function copySnippet() {
    if (!lastCreatedKey) return;
    navigator.clipboard.writeText(buildSnippetForNew(lastCreatedKey));
    setSnippetCopied(true);
  }

  function copyKeySnippet(key: PublicChatApiKey) {
    const fullKey = snippetKeyInput[key.id]?.trim() || `${key.key_prefix}...`;
    navigator.clipboard.writeText(buildSnippet(key.key_prefix, key, fullKey));
  }

  function startEditing(key: PublicChatApiKey) {
    setEditingKeyId(key.id);
    setEditForm({
      widget_title: key.widget_title || "",
      widget_theme: key.widget_theme,
      widget_accent: key.widget_accent,
      widget_position: key.widget_position,
    });
  }

  async function saveEditing() {
    if (!editingKeyId) return;
    await updatePublicChatKey.mutateAsync({
      keyId: editingKeyId,
      payload: {
        widget_title: editForm.widget_title.trim() || null,
        widget_theme: editForm.widget_theme,
        widget_accent: editForm.widget_accent,
        widget_position: editForm.widget_position,
      },
    });
    setEditingKeyId(null);
  }

  function agentDisplayName(id: string): string {
    const agent = (agents ?? []).find((a) => a.id === id);
    if (!agent) return id;
    return agent.friendly_name || agent.name || agent.slug;
  }

  const selectClass = "w-full rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2.5 text-sm text-[var(--text-display)] outline-none";

  return (
    <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
      <div className="space-y-6">
        {/* API Key creation form */}
        <form className="panel-frame p-6" onSubmit={handleSubmit}>
          <p className="panel-label">Widget API</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">Create API key</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            Generate a key to embed the chat widget on an external website. The key is shown once after creation.
          </p>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">Label</span>
              <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Website chat — acme.com" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Agent</span>
              <select value={agentId} onChange={(e) => { setAgentId(e.target.value); setBoundUserId(""); }} className={selectClass}>
                <option value="">Select an agent…</option>
                {activeAgents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.friendly_name || agent.name || agent.slug} · {agent.status}
                  </option>
                ))}
              </select>
            </label>
            <label className="panel-field">
              <span className="panel-label">Allowed domains</span>
              <input value={allowedDomains} onChange={(e) => setAllowedDomains(e.target.value)} placeholder="*, example.com, app.example.com" />
              <span className="mt-1 block text-xs text-[var(--text-secondary)]">Comma-separated. Use * to allow any domain.</span>
            </label>
            <label className="panel-field">
              <span className="panel-label">Bound user (server-side only)</span>
              <select
                value={boundUserId}
                onChange={(e) => setBoundUserId(e.target.value)}
                className={selectClass}
                disabled={!agentId || widgetCustomized || trustSenderId}
              >
                <option value="">Anonymous (no M365 access)</option>
                {assignableUsers(agentId).map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.display_name || user.username}
                  </option>
                ))}
              </select>
              <span className="mt-1 block text-xs text-[var(--warning)]">
                Anyone holding this key acts with this user&apos;s Microsoft 365 permissions. Use it only from a trusted server
                (e.g. a Teams bridge), never in a browser widget.
                {widgetCustomized ? " Disabled because widget appearance is customized." : ""}
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={trustSenderId}
                disabled={widgetCustomized || !!boundUserId}
                onChange={(e) => setTrustSenderId(e.target.checked)}
                className="mt-1"
              />
              <span>
                Trust sender ID from the server (per-user identity). The bridge sends the Teams user ID when it opens a session and
                each person gets their own Microsoft 365 permissions. Only enable on a key used by a trusted server.
              </span>
            </label>
            <div className="grid grid-cols-2 gap-4">
              <label className="panel-field">
                <span className="panel-label">Requests / month</span>
                <input type="number" min={1} value={requestsPerMonth} onChange={(e) => setRequestsPerMonth(Number(e.target.value))} />
              </label>
              <label className="panel-field">
                <span className="panel-label">Tokens / month</span>
                <input type="number" min={1} value={tokensPerMonth} onChange={(e) => setTokensPerMonth(Number(e.target.value))} />
              </label>
            </div>
            <button
              className="panel-button-primary w-full"
              type="submit"
              disabled={createPublicChatKey.isPending || !label.trim() || !agentId}
            >
              {createPublicChatKey.isPending ? "Creating…" : "Create API key"}
            </button>
            {lastCreatedKey ? (
              <div className="rounded-2xl border border-[var(--warning)]/40 bg-[var(--surface-raised)] p-4">
                <p className="panel-label">Key shown once</p>
                <div className="mt-2 flex items-start gap-2">
                  <p className="min-w-0 flex-1 break-all font-mono text-xs text-[var(--text-display)]">{lastCreatedKey}</p>
                  <button type="button" className="panel-button-secondary flex-shrink-0 !px-3 !py-1.5 text-xs" onClick={copyKey}>
                    {copied ? "Copied!" : "Copy"}
                  </button>
                </div>
                <div className="mt-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-3">
                  <p className="text-xs font-medium text-[var(--text-secondary)]">Embed snippet</p>
                  <pre className="mt-1.5 whitespace-pre-wrap break-all text-[11px] leading-5 text-[var(--text-display)]">{buildSnippetForNew(lastCreatedKey)}</pre>
                  <button type="button" className="panel-button-secondary mt-2 text-xs" onClick={copySnippet}>
                    {snippetCopied ? "Copied!" : "Copy snippet"}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </form>

        {/* Widget appearance for new keys */}
        <div className="panel-frame p-6">
          <p className="panel-label">Appearance</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">Widget customization</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            Customize how the chat widget looks. These settings are saved with each API key.
          </p>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">Title</span>
              <input value={widgetTitle} onChange={(e) => setWidgetTitle(e.target.value)} placeholder="My AI Assistant" />
              <span className="mt-1 block text-xs text-[var(--text-secondary)]">Shown in the widget header. Defaults to the agent name.</span>
            </label>
            <div className="grid grid-cols-2 gap-4">
              <label className="panel-field">
                <span className="panel-label">Theme</span>
                <select value={widgetTheme} onChange={(e) => setWidgetTheme(e.target.value)} className={selectClass}>
                  <option value="auto">Auto (system)</option>
                  <option value="light">Light</option>
                  <option value="dark">Dark</option>
                </select>
              </label>
              <label className="panel-field">
                <span className="panel-label">Position</span>
                <select value={widgetPosition} onChange={(e) => setWidgetPosition(e.target.value)} className={selectClass}>
                  <option value="right">Bottom right</option>
                  <option value="left">Bottom left</option>
                </select>
              </label>
            </div>
            <label className="panel-field">
              <span className="panel-label">Accent color</span>
              <div className="mt-1 flex items-center gap-3">
                <input type="color" value={widgetAccent} onChange={(e) => setWidgetAccent(e.target.value)} className="h-10 w-14 cursor-pointer rounded-lg border border-[var(--border)] bg-transparent p-1" />
                <input value={widgetAccent} onChange={(e) => setWidgetAccent(e.target.value)} className="w-28 font-mono text-sm" maxLength={7} />
                <span className="flex h-10 items-center rounded-xl px-4 text-xs font-medium text-white" style={{ background: widgetAccent }}>Preview</span>
              </div>
            </label>
          </div>
        </div>
      </div>

      {/* Registry */}
      <section className="panel-frame p-6">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Active keys</p>
            <h2 className="mt-2 text-2xl text-[var(--text-display)]">API key registry</h2>
          </div>
          <p className="panel-label">{publicChatKeys?.length ?? 0} keys</p>
        </div>
        <div className="mt-5 space-y-4">
          {(publicChatKeys ?? []).map((key) => (
            <article key={key.id} className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="panel-label">{agentDisplayName(key.agent_id)}</p>
                  <h3 className="mt-1 text-lg text-[var(--text-display)]">{key.label}</h3>
                </div>
                <span className={`rounded-full border px-3 py-1 text-xs ${key.is_active ? "border-[var(--success)]/40 text-[var(--success)]" : "border-[var(--danger)]/40 text-[var(--danger)]"}`}>
                  {key.is_active ? "active" : "inactive"}
                </span>
              </div>
              <div className="mt-4 grid gap-3 text-sm text-[var(--text-secondary)] sm:grid-cols-2">
                <p><strong>Prefix:</strong> <span className="font-mono">{key.key_prefix}</span></p>
                <p><strong>Domains:</strong> {key.allowed_domains.length ? key.allowed_domains.join(", ") : "any"}</p>
                <p><strong>Requests/mo:</strong> {key.requests_per_month.toLocaleString()}</p>
                <p><strong>Tokens/mo:</strong> {key.tokens_per_month.toLocaleString()}</p>
                <p>
                  <strong>Bound user:</strong>{" "}
                  <select
                    value={key.bound_user_id ?? ""}
                    onChange={(e) =>
                      updatePublicChatKey.mutate({ keyId: key.id, payload: { bound_user_id: e.target.value || null } })
                    }
                    className={selectClass}
                    disabled={updatePublicChatKey.isPending}
                  >
                    <option value="">{key.bound_user_id ? `Anonymous (was ${userDisplayName(key.bound_user_id)})` : "Anonymous"}</option>
                    {assignableUsers(key.agent_id).map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.display_name || user.username}
                      </option>
                    ))}
                  </select>
                </p>
                <p>
                  <strong>Trusted sender ID:</strong>{" "}
                  <input
                    type="checkbox"
                    checked={key.trust_sender_id}
                    disabled={updatePublicChatKey.isPending || !!key.bound_user_id}
                    onChange={(e) =>
                      updatePublicChatKey.mutate({ keyId: key.id, payload: { trust_sender_id: e.target.checked } })
                    }
                  />
                </p>
                <p><strong>Theme:</strong> {key.widget_theme}</p>
                <p><strong>Color:</strong> <span className="inline-block h-3 w-3 rounded-full align-middle" style={{ background: key.widget_accent }} /> {key.widget_accent}</p>
                <p><strong>Created:</strong> {new Date(key.created_at).toLocaleDateString()}</p>
              </div>

              {updatePublicChatKey.isError ? (
                <p className="mt-3 text-xs text-[var(--danger)]">
                  {(updatePublicChatKey.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                    updatePublicChatKey.error.message}
                </p>
              ) : null}

              {/* Actions */}
              <div className="mt-4 flex flex-wrap gap-3">
                <button type="button" className="panel-button-secondary" onClick={() => setExpandedSnippet(expandedSnippet === key.id ? null : key.id)}>
                  {expandedSnippet === key.id ? "Hide snippet" : "Show snippet"}
                </button>
                <button type="button" className="panel-button-secondary" onClick={() => editingKeyId === key.id ? setEditingKeyId(null) : startEditing(key)}>
                  {editingKeyId === key.id ? "Cancel" : "Edit appearance"}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => {
                    if (window.confirm(`Deactivate API key "${key.label}"?`)) {
                      void deletePublicChatKey.mutateAsync(key.id);
                    }
                  }}
                >
                  Deactivate
                </button>
                <button
                  type="button"
                  className="panel-button-secondary !border-[var(--danger)]/40 !text-[var(--danger)]"
                  onClick={() => {
                    if (window.confirm(`Permanently delete API key "${key.label}"? This cannot be undone.`)) {
                      void permanentlyDeletePublicChatKey.mutateAsync(key.id);
                    }
                  }}
                >
                  Delete
                </button>
              </div>

              {/* Snippet */}
              {expandedSnippet === key.id ? (
                <div className="mt-4 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-3">
                  <p className="text-xs font-medium text-[var(--text-secondary)]">Embed snippet</p>
                  <label className="panel-field mt-2">
                    <span className="text-[10px] text-[var(--text-secondary)]">Paste your full API key to generate a ready-to-use snippet:</span>
                    <input
                      value={snippetKeyInput[key.id] || ""}
                      onChange={(e) => setSnippetKeyInput({ ...snippetKeyInput, [key.id]: e.target.value })}
                      placeholder={`${key.key_prefix}...`}
                      className="font-mono text-xs"
                    />
                  </label>
                  <pre className="mt-2 whitespace-pre-wrap break-all text-[11px] leading-5 text-[var(--text-display)]">{buildSnippet(key.key_prefix, key, snippetKeyInput[key.id]?.trim() || undefined)}</pre>
                  <button type="button" className="panel-button-secondary mt-2 text-xs" onClick={() => copyKeySnippet(key)}>
                    Copy snippet
                  </button>
                </div>
              ) : null}

              {/* Edit appearance */}
              {editingKeyId === key.id ? (
                <div className="mt-4 space-y-3 rounded-xl border border-[var(--accent)]/30 bg-[var(--surface-muted)] p-4">
                  <p className="panel-label">Edit appearance</p>
                  <label className="panel-field">
                    <span className="panel-label">Title</span>
                    <input value={editForm.widget_title} onChange={(e) => setEditForm({ ...editForm, widget_title: e.target.value })} placeholder="Assistant" />
                  </label>
                  <div className="grid grid-cols-2 gap-3">
                    <label className="panel-field">
                      <span className="panel-label">Theme</span>
                      <select value={editForm.widget_theme} onChange={(e) => setEditForm({ ...editForm, widget_theme: e.target.value })} className={selectClass}>
                        <option value="auto">Auto</option>
                        <option value="light">Light</option>
                        <option value="dark">Dark</option>
                      </select>
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">Position</span>
                      <select value={editForm.widget_position} onChange={(e) => setEditForm({ ...editForm, widget_position: e.target.value })} className={selectClass}>
                        <option value="right">Right</option>
                        <option value="left">Left</option>
                      </select>
                    </label>
                  </div>
                  <label className="panel-field">
                    <span className="panel-label">Accent color</span>
                    <div className="mt-1 flex items-center gap-3">
                      <input type="color" value={editForm.widget_accent} onChange={(e) => setEditForm({ ...editForm, widget_accent: e.target.value })} className="h-9 w-12 cursor-pointer rounded-lg border border-[var(--border)] bg-transparent p-1" />
                      <input value={editForm.widget_accent} onChange={(e) => setEditForm({ ...editForm, widget_accent: e.target.value })} className="w-24 font-mono text-sm" maxLength={7} />
                    </div>
                  </label>
                  <div className="flex gap-3">
                    <button type="button" className="panel-button-primary" onClick={saveEditing} disabled={updatePublicChatKey.isPending}>
                      {updatePublicChatKey.isPending ? "Saving…" : "Save"}
                    </button>
                    <button type="button" className="panel-button-secondary" onClick={() => setEditingKeyId(null)}>Cancel</button>
                  </div>
                </div>
              ) : null}
            </article>
          ))}
          {!publicChatKeys?.length ? (
            <p className="text-sm text-[var(--text-secondary)]">No API keys have been created yet.</p>
          ) : null}
        </div>
      </section>
    </section>
  );
}
