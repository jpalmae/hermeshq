// pi-runner.mjs — Bridge between HermesHQ (Python) and Pi SDK (Node.js)
// Communication: JSON-RPC over stdin/stdout (newline-delimited)

import * as readline from "readline";
import { existsSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { EnvHttpProxyAgent, install, setGlobalDispatcher } from "undici";

if (process.env.HTTP_PROXY || process.env.HTTPS_PROXY) {
  setGlobalDispatcher(new EnvHttpProxyAgent());
  install();
}

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
    const {
      createAgentSession,
      DefaultResourceLoader,
      ModelRuntime,
      SessionManager,
      SettingsManager,
    } = await import("@earendil-works/pi-coding-agent");

    const agentDir = process.env.PI_CODING_AGENT_DIR || process.env.PI_AGENT_DIR || join(process.env.HOME || "/root", ".pi", "agent");
    const runtimeStateDir = join(process.env.HOME || "/tmp", ".pi-runtime");
    mkdirSync(runtimeStateDir, { recursive: true, mode: 0o700 });
    const modelRuntime = await ModelRuntime.create({
      authPath: join(runtimeStateDir, "auth.json"),
      modelsPath: join(agentDir, "models.json"),
      modelsStorePath: join(runtimeStateDir, "models-store.json"),
    });

    const settingsPath = join(agentDir, "settings.json");
    const managedSettings = existsSync(settingsPath)
      ? JSON.parse(readFileSync(settingsPath, "utf8"))
      : {};
    const settingsManager = SettingsManager.inMemory(managedSettings, { projectTrusted: false });
    const resourceLoader = new DefaultResourceLoader({
      cwd: process.cwd(),
      agentDir,
      settingsManager,
      appendSystemPrompt: params.system_prompt ? [params.system_prompt] : undefined,
      skillsOverride: (current) => ({
        skills: [...current.skills, ...discoverPiSkills()],
        diagnostics: current.diagnostics,
      }),
    });
    await resourceLoader.reload();
    const extensionsResult = resourceLoader.getExtensions();
    if (extensionsResult.errors.length > 0) {
      throw new Error(`Managed Pi extension failed to load: ${extensionsResult.errors[0].error}`);
    }
    if (!extensionsResult.extensions.some((extension) => extension.path.endsWith("hermeshq-security.ts"))) {
      throw new Error("Managed HermesHQ security extension is missing");
    }

    const nvidiaKey = process.env.NVIDIA_API_KEY;
    const openaiKey = process.env.OPENAI_API_KEY;

    // Set API key for ALL providers defined in our models.json
    try {
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
      agentDir,
      modelRuntime,
      settingsManager,
      resourceLoader,
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
        const error = extractAssistantError(messages);
        if (error) {
          send({ type: "error", error });
          return;
        }
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

  // First: try our custom providers from models.json
  try {
    const agentDir = process.env.PI_CODING_AGENT_DIR || process.env.PI_AGENT_DIR || (process.env.HOME || "/root") + "/.pi/agent";
    const config = JSON.parse(readFileSync(join(agentDir, "models.json"), "utf8"));
    for (const providerName of Object.keys(config.providers || {})) {
      // Try exact ID match
      const m = modelRuntime.getModel(providerName, modelSpec);
      if (m) return m;
      // Try short ID
      const shortId = modelSpec.includes("/") ? modelSpec.split("/").pop() : modelSpec;
      const m2 = modelRuntime.getModel(providerName, shortId);
      if (m2) return m2;
      // Try full ID as-is (for NIM: deepseek-ai/deepseek-v4-flash-0731)
      const providerConfig = config.providers[providerName];
      if (providerConfig?.models) {
        for (const modelDef of providerConfig.models) {
          if (modelDef.id === modelSpec || modelDef.id === shortId) {
            // Model is defined in our config but Pi SDK doesn't index it —
            // construct a model object from our config
            return {
              id: modelDef.id,
              name: modelDef.name || modelDef.id,
              provider: providerName,
              providerId: providerName,
              reasoning: modelDef.reasoning || false,
              input: modelDef.input || ["text"],
              cost: modelDef.cost || { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
              contextWindow: modelDef.contextWindow || 128000,
              maxTokens: modelDef.maxTokens || 4096,
              api: providerConfig.api || "openai-completions",
              baseUrl: providerConfig.baseUrl,
            };
          }
        }
      }
    }
  } catch {}

  // Second: direct provider/id lookup on built-in providers
  if (modelSpec.includes("/")) {
    const [provider, ...rest] = modelSpec.split("/");
    const id = rest.join("/");
    const m = modelRuntime.getModel(provider, id);
    if (m) return m;
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

function extractAssistantError(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant" && (msg.stopReason === "error" || msg.errorMessage)) {
      return msg.errorMessage || "Pi model execution failed";
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

function discoverPiSkills() {
  const skillsDir = join(process.cwd(), ".pi", "skills");
  const skills = [];
  if (!existsSync(skillsDir)) return skills;
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const skillMd = join(skillsDir, entry.name, "SKILL.md");
    if (!existsSync(skillMd)) continue;
    skills.push({
      name: entry.name,
      description: extractSkillDescription(readFileSync(skillMd, "utf8"), entry.name),
      filePath: skillMd,
      baseDir: join(skillsDir, entry.name),
      source: "custom",
    });
  }
  return skills;
}

function extractSkillDescription(content, fallbackName) {
  const fm = content.match(/^---\n([\s\S]*?)\n---/);
  if (fm) {
    const desc = fm[1].match(/^description:\s*(.+)$/m);
    if (desc) return desc[1].trim();
  }
  const heading = content.match(/^#\s+(.+)$/m);
  return heading ? heading[1].trim() : fallbackName;
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
  queue.finally(() => process.exit(0));
});
