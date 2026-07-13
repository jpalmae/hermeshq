import { apiClient } from "./client";

export interface AgentDraft {
  name?: string;
  friendly_name?: string;
  slug?: string;
  description?: string;
  runtime_profile: string;
  system_prompt?: string;
  enabled_toolsets?: string[];
  integration_configs?: Record<string, Record<string, unknown>>;
}

export interface RequiredConnector {
  slug: string;
  name: string;
  installed: boolean;
  required_fields: string[];
  admin_instructions: string;
}

export interface AgentBuilderTurn {
  assistant_text: string;
  draft: AgentDraft;
  required_connectors: RequiredConnector[];
  ready_to_create: boolean;
}

export interface BuilderSession {
  session_id: string;
  tool_mode: string;
}

export interface FinalizeResult {
  agent_id: string;
  agent_name: string;
  required_connectors: RequiredConnector[];
}

export async function createBuilderSession(): Promise<BuilderSession> {
  const { data } = await apiClient.post("/agent-builder/sessions");
  return data;
}

export async function sendBuilderMessage(
  sessionId: string,
  text: string,
  onDelta: (chunk: string) => void,
): Promise<AgentBuilderTurn> {
  const response = await fetch(`${apiClient.defaults.baseURL}/agent-builder/sessions/${sessionId}/message`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ text }),
    credentials: "include",
  });

  if (!response.ok) {
    throw new Error(`Builder request failed: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("No response body");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let finalTurn: AgentBuilderTurn | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const jsonStr = line.slice(6).trim();
      if (!jsonStr) continue;

      try {
        const event = JSON.parse(jsonStr);
        if (event.type === "delta") {
          onDelta(event.text);
        } else if (event.type === "turn") {
          finalTurn = event.turn;
        } else if (event.type === "error") {
          throw new Error(event.message || "Builder error");
        }
      } catch {
        // partial JSON, skip
      }
    }
  }

  if (!finalTurn) {
    throw new Error("No turn received from builder");
  }

  return finalTurn;
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem("hermeshq.token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function finalizeAgent(sessionId: string): Promise<FinalizeResult> {
  const { data } = await apiClient.post(`/agent-builder/sessions/${sessionId}/finalize`);
  return data;
}
