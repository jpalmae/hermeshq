// pi-runner.mjs — Bridge between HermesHQ (Python) and Pi SDK (Node.js)
// Communication: JSON-RPC over stdin/stdout (newline-delimited)

import * as readline from "readline";

let session = null;
let queue = Promise.resolve();

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function enqueue(fn) {
  queue = queue.then(fn);
  return queue;
}

async function handleInit(params) {
  try {
    const { createAgentSession, SessionManager } = await import("@earendil-works/pi-coding-agent");
    const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");

    const modelRuntime = await ModelRuntime.create({
      agentDir: process.env.PI_AGENT_DIR || undefined,
    });

    const nvidiaKey = process.env.NVIDIA_API_KEY;
    const openaiKey = process.env.OPENAI_API_KEY;

    // Set API key for ALL providers defined in our models.json
    const { readFileSync, existsSync } = await import("fs");
    const { join } = await import("path");
    try {
      const agentDir = process.env.PI_AGENT_DIR || join(process.env.HOME || "/root", ".pi", "agent");
      const modelsFile = join(agentDir, "models.json");
      if (existsSync(modelsFile)) {
        const config = JSON.parse(readFileSync(modelsFile, "utf8"));
        const key = nvidiaKey || openaiKey || process.env.ANTHROPIC_API_KEY;
        if (key) {
          for (const pName of Object.keys(config.providers || {})) {
            try { await modelRuntime.setRuntimeApiKey(pName, key); } catch (e) {}
          }
        }
      }
    } catch {}

    // Also set for known providers as fallback
    if (nvidiaKey) {
      try { await modelRuntime.setRuntimeApiKey("nvidia", nvidiaKey); } catch (e) {}
    }
    if (openaiKey) {
      try { await modelRuntime.setRuntimeApiKey("openai", openaiKey); } catch (e) {}
    }

    const model = resolveModel(params.model, modelRuntime);
    if (!model) {
      send({ type: "error", error: "Could not resolve model: " + params.model });
      return;
    }

    const apiKey = nvidiaKey || openaiKey || process.env.ANTHROPIC_API_KEY;
    if (apiKey && model.provider) {
      try { await modelRuntime.setRuntimeApiKey(model.provider, apiKey); } catch (e) {}
    }

    const tools = params.tools || ["read", "bash", "edit"];

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

rl.on("line", (line) => {
  enqueue(async () => {
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
});

rl.on("close", () => {
  process.exit(0);
});
