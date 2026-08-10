// pi-runner.mjs — Bridge between HermesHQ (Python) and Pi SDK (Node.js)
// Communication: JSON-RPC over stdin/stdout (newline-delimited)

import * as readline from "readline";

let session = null;

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

async function handleInit(params) {
  send({ type: "debug", message: "init started" });
  try {
    const { createAgentSession, SessionManager } = await import("@earendil-works/pi-coding-agent");
    send({ type: "debug", message: "imports done" });
    const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");
    const { InMemoryCredentialStore } = await import("@earendil-works/pi-ai");

    const credStore = new InMemoryCredentialStore();
    const modelRuntime = await ModelRuntime.create({ credentials: credStore });
    send({ type: "debug", message: "ModelRuntime created" });

    const nvidiaKey = process.env.NVIDIA_API_KEY;
    const openaiKey = process.env.OPENAI_API_KEY;
    if (nvidiaKey) {
      try { await modelRuntime.setRuntimeApiKey("nvidia", nvidiaKey); } catch (e) {}
    }
    if (openaiKey) {
      try { await modelRuntime.setRuntimeApiKey("openai", openaiKey); } catch (e) {}
    }
    send({ type: "debug", message: "keys set" });

    const model = resolveModel(params.model, modelRuntime);
    send({ type: "debug", message: "model resolved: " + (model ? model.provider + "/" + model.id : "NONE") });
    if (!model) {
      send({ type: "error", error: "Could not resolve model: " + params.model + ". Available: " + modelRuntime.getProviders().filter(p => p.models?.length).map(p => p.id).join(", ") });
      return;
    }

    send({ type: "debug", message: "Using model: " + model.provider + "/" + model.id });

    const tools = params.tools || ["read", "bash", "edit"];

    send({ type: "debug", message: "Creating session..." });
    const result = await createAgentSession({
      model,
      thinkingLevel: params.thinking_level || "medium",
      tools,
      sessionManager: SessionManager.inMemory(),
      cwd: process.cwd(),
    });

    session = result.session;

    session.subscribe((event) => {
      if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
        send({ type: "text_delta", delta: event.assistantMessageEvent.delta });
      }
      if (event.type === "tool_execution_start") {
        send({ type: "tool_call", tool: event.toolName, input: event.args });
      }
      if (event.type === "agent_settled") {
        const messages = session.messages;
        const response = extractResponse(messages);
        const toolCalls = extractToolCalls(messages);
        send({ type: "done", response, messages, tool_calls: toolCalls, tokens: 0, turns: messages.length, attachments: [] });
      }
    });

    send({ type: "ready" });
  } catch (err) {
    send({ type: "error", error: "Init failed: " + err.message + "\n" + err.stack });
  }
}

async function handlePrompt(params) {
  if (!session) {
    send({ type: "error", error: "Session not initialized" });
    return;
  }
  try {
    await session.prompt(params.text);
  } catch (err) {
    send({ type: "error", error: err.message });
  }
}

async function handleAbort() {
  if (session) {
    try {
      await session.abort();
    } catch {}
  }
}

function resolveModel(modelSpec, modelRuntime) {
  if (!modelSpec) return undefined;

  // Try direct provider/id lookup
  if (modelSpec.includes("/")) {
    const [provider, ...rest] = modelSpec.split("/");
    const id = rest.join("/");
    const m = modelRuntime.getModel(provider, id);
    if (m) return m;
  }

  // Search all providers for the model ID
  for (const p of modelRuntime.getProviders()) {
    const m = modelRuntime.getModel(p.id, modelSpec);
    if (m) return m;
    // Also try without provider prefix
    const shortId = modelSpec.split("/").pop();
    const m2 = modelRuntime.getModel(p.id, shortId);
    if (m2) return m2;
  }

  return undefined;
}

function extractResponse(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant" && msg.content) {
      if (typeof msg.content === "string") return msg.content;
      const textParts = msg.content.filter((c) => c.type === "text").map((c) => c.text);
      if (textParts.length) return textParts.join("\n");
    }
  }
  return "";
}

function extractToolCalls(messages) {
  const calls = [];
  for (const msg of messages) {
    if (msg.role === "assistant" && Array.isArray(msg.content)) {
      for (const part of msg.content) {
        if (part.type === "tool_use") {
          calls.push({ tool: part.name, input: part.input });
        }
      }
    }
  }
  return calls;
}

rl.on("line", async (line) => {
  try {
    const msg = JSON.parse(line);
    switch (msg.method) {
      case "init":
        await handleInit(msg.params || {});
        break;
      case "prompt":
        await handlePrompt(msg.params || {});
        break;
      case "abort":
        await handleAbort();
        break;
    }
  } catch (err) {
    send({ type: "error", error: `Runner error: ${err.message}` });
  }
});

rl.on("close", () => {
  process.exit(0);
});
