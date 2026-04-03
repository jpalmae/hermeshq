import { FormEvent, useEffect, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useBroadcast, useCommsHistory, useCommsTopology, useSendMessage } from "../api/comms";
import { AgentAvatar } from "../components/AgentAvatar";
import type { Agent } from "../types/api";

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

function buildAgentMap(agents: Agent[]) {
  return new Map(agents.map((agent) => [agent.id, agent]));
}

function ancestorIds(agentMap: Map<string, Agent>, agentId: string) {
  const ids: string[] = [];
  const seen = new Set<string>();
  let current = agentMap.get(agentId);
  let parentId = current?.supervisor_agent_id ?? null;
  while (parentId && !seen.has(parentId)) {
    ids.push(parentId);
    seen.add(parentId);
    current = agentMap.get(parentId);
    parentId = current?.supervisor_agent_id ?? null;
  }
  return ids;
}

function descendantIds(agentMap: Map<string, Agent>, rootId: string) {
  const childrenByParent = new Map<string | null, string[]>();
  for (const agent of agentMap.values()) {
    const key = agent.supervisor_agent_id ?? null;
    childrenByParent.set(key, [...(childrenByParent.get(key) ?? []), agent.id]);
  }
  const descendants = new Set<string>();
  const stack = [...(childrenByParent.get(rootId) ?? [])];
  while (stack.length) {
    const current = stack.pop();
    if (!current || descendants.has(current)) {
      continue;
    }
    descendants.add(current);
    stack.push(...(childrenByParent.get(current) ?? []));
  }
  return descendants;
}

function delegateOptionState(source: Agent | undefined, target: Agent, agentMap: Map<string, Agent>) {
  if (!source) {
    return { disabled: false, reason: "" };
  }
  if (source.id === target.id) {
    return { disabled: true, reason: "same agent" };
  }
  if (!source.can_send_tasks) {
    return { disabled: true, reason: "source cannot delegate" };
  }
  if (!target.can_receive_tasks) {
    return { disabled: true, reason: "target cannot receive tasks" };
  }
  if (!source.supervisor_agent_id) {
    return { disabled: false, reason: "independent" };
  }
  if (ancestorIds(agentMap, source.id).includes(target.id)) {
    return { disabled: false, reason: "escalate upward" };
  }
  if (descendantIds(agentMap, source.id).has(target.id)) {
    return { disabled: false, reason: "delegate downward" };
  }
  return { disabled: true, reason: "cross-branch blocked" };
}

function routingTone(reason: string) {
  if (reason === "escalate upward") return "text-[var(--interactive)] border-[var(--interactive)]";
  if (reason === "delegate downward" || reason === "independent") return "text-[var(--success)] border-[var(--success)]";
  return "text-[var(--accent)] border-[var(--accent)]";
}

export function CommsPage() {
  const { data: agents } = useAgents();
  const { data: history } = useCommsHistory();
  const { data: topology } = useCommsTopology();
  const sendMessage = useSendMessage();
  const broadcast = useBroadcast();

  const [fromAgentId, setFromAgentId] = useState("");
  const [toAgentId, setToAgentId] = useState("");
  const [messageType, setMessageType] = useState("direct");
  const [teamTag, setTeamTag] = useState("");
  const [content, setContent] = useState("");

  const agentMap = useMemo(() => buildAgentMap(agents ?? []), [agents]);
  const sourceAgent = fromAgentId ? agentMap.get(fromAgentId) : undefined;
  const delegateOptions = useMemo(
    () =>
      (agents ?? []).map((agent) => ({
        agent,
        state: delegateOptionState(sourceAgent, agent, agentMap),
      })),
    [agents, sourceAgent, agentMap],
  );
  const selectedDelegateState = useMemo(
    () => delegateOptions.find((item) => item.agent.id === toAgentId)?.state,
    [delegateOptions, toAgentId],
  );
  const routingPreview = useMemo(() => {
    const upward = delegateOptions.filter((item) => item.state.reason === "escalate upward");
    const downward = delegateOptions.filter((item) => item.state.reason === "delegate downward");
    const unrestricted = delegateOptions.filter((item) => item.state.reason === "independent" && item.agent.id !== sourceAgent?.id);
    const blocked = delegateOptions.filter((item) => item.state.reason === "cross-branch blocked");
    return { upward, downward, unrestricted, blocked };
  }, [delegateOptions, sourceAgent?.id]);

  const edgeSummary = useMemo(() => {
    const summary = new Map<
      string,
      {
        source: string;
        target: string;
        total: number;
        delegate: number;
        direct: number;
        broadcast: number;
        lastSeenIndex: number;
      }
    >();
    (topology?.edges ?? []).forEach((edge, index) => {
      const key = `${edge.source}->${edge.target}`;
      const current = summary.get(key) ?? {
        source: edge.source,
        target: edge.target,
        total: 0,
        delegate: 0,
        direct: 0,
        broadcast: 0,
        lastSeenIndex: index,
      };
      current.total += 1;
      current.lastSeenIndex = index;
      if (edge.type === "delegate") current.delegate += 1;
      else if (edge.type === "broadcast") current.broadcast += 1;
      else current.direct += 1;
      summary.set(key, current);
    });
    return Array.from(summary.values()).sort((left, right) => right.lastSeenIndex - left.lastSeenIndex);
  }, [topology?.edges]);

  useEffect(() => {
    if (messageType !== "delegate" || !toAgentId) {
      return;
    }
    const selected = delegateOptions.find((item) => item.agent.id === toAgentId);
    if (selected?.state.disabled) {
      setToAgentId("");
    }
  }, [delegateOptions, messageType, toAgentId]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (messageType === "broadcast") {
      await broadcast.mutateAsync({
        from_agent_id: fromAgentId,
        team_tag: teamTag,
        content,
      });
    } else {
      await sendMessage.mutateAsync({
        from_agent_id: fromAgentId,
        to_agent_id: toAgentId,
        message_type: messageType,
        content,
      });
    }
    setContent("");
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.7fr_1.3fr]">
      <form className="panel-frame p-6" onSubmit={onSubmit}>
        <div className="space-y-3">
          <p className="panel-label">Comms router</p>
          <h2 className="text-3xl text-[var(--text-display)]">Inter-agent exchange</h2>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            <code>Delegate</code> dispatches a one-off task to another agent. It does not create a recurring
            schedule even if the message says “cada 15 minutos”. For recurring execution use{" "}
            <code>Settings &gt; Timed tasks</code>.
          </p>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            Hierarchy rules now apply only to <code>Delegate</code>: independent agents can delegate freely, subordinates
            can escalate to supervisors or delegate within their own branch, and cross-branch lateral delegation is blocked.
            <code>Direct</code> messages remain unrestricted inside the user's access scope.
          </p>
        </div>
        <div className="mt-8 space-y-5">
          <label className="panel-field">
            <span className="panel-label">From</span>
            <select value={fromAgentId} onChange={(event) => setFromAgentId(event.target.value)}>
                <option value="">Select source</option>
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">Mode</span>
            <select value={messageType} onChange={(event) => setMessageType(event.target.value)}>
              <option value="direct">Direct</option>
              <option value="delegate">Delegate</option>
              <option value="broadcast">Broadcast</option>
            </select>
          </label>
          {messageType === "broadcast" ? (
            <label className="panel-field">
              <span className="panel-label">Team tag</span>
              <input value={teamTag} onChange={(event) => setTeamTag(event.target.value)} />
            </label>
          ) : (
            <label className="panel-field">
              <span className="panel-label">To</span>
              <select value={toAgentId} onChange={(event) => setToAgentId(event.target.value)}>
                <option value="">Select target</option>
                {(messageType === "delegate" ? delegateOptions : (agents ?? []).map((agent) => ({ agent, state: null }))).map((item) => (
                  <option
                    key={item.agent.id}
                    value={item.agent.id}
                    disabled={Boolean(item.state?.disabled)}
                  >
                    {messageType === "delegate"
                      ? `${agentLabel(item.agent)}${item.state?.reason ? ` — ${item.state.reason}` : ""}`
                      : agentLabel(item.agent)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {messageType === "delegate" ? (
            <p className="text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">
              {sourceAgent
                ? selectedDelegateState && !selectedDelegateState.disabled
                  ? `Allowed: ${selectedDelegateState.reason}`
                  : sourceAgent.supervisor_agent_id
                    ? "Allowed targets: supervisors above or agents below in the same branch."
                    : "Independent agent: delegation is unrestricted."
                : "Select a source agent to evaluate hierarchy rules."}
            </p>
          ) : null}
          <label className="panel-field">
            <span className="panel-label">Content</span>
            <textarea rows={7} value={content} onChange={(event) => setContent(event.target.value)} />
          </label>
          <button
            type="submit"
            className="panel-button-primary w-full"
            disabled={messageType !== "broadcast" && (!fromAgentId || !toAgentId || (messageType === "delegate" && Boolean(selectedDelegateState?.disabled)))}
          >
            Dispatch
          </button>
        </div>
      </form>

      <div className="space-y-6">
        {messageType === "delegate" && sourceAgent ? (
          <section className="panel-frame p-6">
            <div className="border-b border-[var(--border)] pb-4">
              <p className="panel-label">Hierarchy routing</p>
              <h3 className="mt-2 text-2xl text-[var(--text-display)]">Allowed delegate map</h3>
              <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                Visual scope for <span className="text-[var(--text-display)]">{agentLabel(sourceAgent)}</span>. This map reflects the same
                hierarchy rules enforced by the backend.
              </p>
            </div>

            <div className="mt-5 grid gap-4 xl:grid-cols-[0.8fr_0.9fr_1.3fr]">
              <div className="space-y-3">
                <p className="panel-label">Escalate upward</p>
                {routingPreview.upward.length ? (
                  routingPreview.upward.map(({ agent, state }) => (
                    <div key={agent.id} className="border border-[var(--interactive)]/40 p-3">
                      <div className="flex items-center gap-3">
                        <AgentAvatar agent={agent} sizeClass="h-10 w-10" className="shrink-0" />
                        <div className="min-w-0">
                          <p className="truncate text-sm text-[var(--text-display)]">{agentLabel(agent)}</p>
                          <p className="panel-label mt-1">{state.reason}</p>
                        </div>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="panel-inline-status">[NONE] no supervisors above this agent</p>
                )}
              </div>

              <div className="border border-[var(--border-visible)] p-4">
                <p className="panel-label">Source</p>
                <div className="mt-3 flex items-center gap-3">
                  <AgentAvatar agent={sourceAgent} sizeClass="h-12 w-12" className="shrink-0" />
                  <div className="min-w-0">
                    <p className="truncate text-lg text-[var(--text-display)]">{agentLabel(sourceAgent)}</p>
                    <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      {sourceAgent.supervisor_agent_id ? "subordinate agent" : "independent agent"}
                    </p>
                  </div>
                </div>
                <div className="mt-4 border-t border-[var(--border)] pt-3">
                  <p className="text-sm leading-6 text-[var(--text-secondary)]">
                    {sourceAgent.supervisor_agent_id
                      ? "May escalate to any supervisor above and delegate downward inside its own branch."
                      : "Independent agents can delegate freely because they do not report to a supervisor."}
                  </p>
                </div>
              </div>

              <div className="space-y-3">
                <p className="panel-label">
                  {sourceAgent.supervisor_agent_id ? "Delegate downward" : "Reachable targets"}
                </p>
                {(sourceAgent.supervisor_agent_id ? routingPreview.downward : routingPreview.unrestricted).length ? (
                  <div className="grid gap-3 md:grid-cols-2">
                    {(sourceAgent.supervisor_agent_id ? routingPreview.downward : routingPreview.unrestricted).map(({ agent, state }) => (
                      <div key={agent.id} className="border border-[var(--success)]/40 p-3">
                        <div className="flex items-center gap-3">
                          <AgentAvatar agent={agent} sizeClass="h-10 w-10" className="shrink-0" />
                          <div className="min-w-0">
                            <p className="truncate text-sm text-[var(--text-display)]">{agentLabel(agent)}</p>
                            <p className="panel-label mt-1">{state.reason}</p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="panel-inline-status">[NONE] no descendants available for delegation</p>
                )}
              </div>
            </div>

            {sourceAgent.supervisor_agent_id ? (
              <div className="mt-5 border-t border-[var(--border)] pt-4">
                <div className="flex items-end justify-between gap-4">
                  <div>
                    <p className="panel-label">Blocked routes</p>
                    <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                      Cross-branch siblings and unrelated branches are visible here so operators know upfront where delegation is not allowed.
                    </p>
                  </div>
                  <p className="panel-label">{routingPreview.blocked.length} blocked</p>
                </div>
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  {routingPreview.blocked.map(({ agent, state }) => (
                    <div key={agent.id} className="border border-[var(--accent)]/40 p-3">
                      <div className="flex items-center gap-3">
                        <AgentAvatar agent={agent} sizeClass="h-10 w-10" className="shrink-0" />
                        <div className="min-w-0">
                          <p className="truncate text-sm text-[var(--text-display)]">{agentLabel(agent)}</p>
                          <p className={`panel-label mt-1 ${routingTone(state.reason).split(" ")[0]}`}>{state.reason}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                  {!routingPreview.blocked.length ? <p className="panel-inline-status">[CLEAR] no blocked cross-branch routes</p> : null}
                </div>
              </div>
            ) : null}
          </section>
        ) : null}

        <section className="panel-frame p-6">
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">Topology</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">Message edges</h3>
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {edgeSummary.map((edge) => {
              const sourceLabel = topology?.nodes.find((node) => node.id === edge.source)?.label ?? edge.source;
              const targetLabel = topology?.nodes.find((node) => node.id === edge.target)?.label ?? edge.target;
              return (
                <div key={`${edge.source}->${edge.target}`} className="border border-[var(--border)] p-4">
                  <p className="panel-label">
                    {sourceLabel} -&gt; {targetLabel}
                  </p>
                  <p className="mt-2 text-2xl text-[var(--text-display)]">{edge.total}</p>
                  <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                    {edge.delegate} delegate / {edge.direct} direct / {edge.broadcast} broadcast
                  </p>
                </div>
              );
            })}
          </div>
        </section>

        <section className="panel-frame p-6">
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">History</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">Recent messages</h3>
          </div>
          <div className="mt-2">
            {(history ?? []).map((message) => (
              <article key={message.id} className="border-b border-[var(--border)] py-4">
                <div className="flex items-center justify-between gap-4">
                  <p className="panel-label">{message.message_type}</p>
                  <p className="panel-label">{new Date(message.created_at).toLocaleString()}</p>
                </div>
                <p className="mt-2 text-sm text-[var(--text-primary)]">{message.content}</p>
              </article>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
