import { FormEvent, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useBroadcast, useCommsHistory, useCommsTopology, useSendMessage } from "../api/comms";

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
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

  const edgeSummary = useMemo(() => {
    const counts = new Map<string, number>();
    (topology?.edges ?? []).forEach((edge) => {
      const key = `${edge.source}->${edge.target}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    });
    return Array.from(counts.entries());
  }, [topology?.edges]);

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
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="panel-field">
            <span className="panel-label">Content</span>
            <textarea rows={7} value={content} onChange={(event) => setContent(event.target.value)} />
          </label>
          <button type="submit" className="panel-button-primary w-full">
            Dispatch
          </button>
        </div>
      </form>

      <div className="space-y-6">
        <section className="panel-frame p-6">
          <div className="border-b border-[var(--border)] pb-4">
            <p className="panel-label">Topology</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">Message edges</h3>
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {edgeSummary.map(([edge, count]) => (
              <div key={edge} className="border border-[var(--border)] p-4">
                <p className="panel-label">{edge}</p>
                <p className="mt-2 text-2xl text-[var(--text-display)]">{count}</p>
              </div>
            ))}
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
