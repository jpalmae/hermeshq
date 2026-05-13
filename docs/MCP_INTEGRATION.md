# MCP Integration Guide — HermesHQ

HermesHQ implements the [Model Context Protocol (MCP)](https://spec.modelcontextprotocol.io/) specification version `2024-11-05`, allowing external AI clients to interact with your agents.

## Quick Start

### 1. Create an MCP Access Token

Navigate to **Settings → External Access** and create a new token with the desired scopes:
- `agents:list` — List and view agents
- `agents:invoke` — Send tasks to agents
- `tasks:read` — Read task status and results

Optionally restrict the token to specific agents.

### 2. Connect Your Client

#### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hermeshq": {
      "url": "https://your-instance.com/mcp",
      "headers": {
        "Authorization": "Bearer hq_mcp_YOUR_TOKEN_HERE"
      }
    }
  }
}
```

#### VS Code / Cursor

1. Install the MCP extension
2. Configure the server URL: `https://your-instance.com/mcp`
3. Set the Bearer token in authentication settings

#### Claude Code (CLI)

```bash
export MCP_TOKEN=hq_mcp_YOUR_TOKEN_HERE
claude --mcp-server https://your-instance.com/mcp --mcp-header "Authorization: Bearer $MCP_TOKEN"
```

#### cURL (testing)

```bash
# Initialize
curl -X POST https://your-instance.com/mcp \
  -H "Authorization: Bearer hq_mcp_YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# List tools
curl -X POST https://your-instance.com/mcp \
  -H "Authorization: Bearer hq_mcp_YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# Invoke an agent
curl -X POST https://your-instance.com/mcp \
  -H "Authorization: Bearer hq_mcp_YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0","id":3,"method":"tools/call",
    "params":{
      "name":"invoke_agent",
      "arguments":{"agent_id":"AGENT_UUID","prompt":"Hello!","wait_seconds":30}
    }
  }'
```

## Available Methods

### Protocol Methods

| Method | Description |
|--------|-------------|
| `initialize` | Handshake — returns server capabilities |
| `logging/setLevel` | Set log verbosity (`debug`, `info`, `warning`, `error`) |

### Tools

| Tool | Scope Required | Description |
|------|---------------|-------------|
| `list_agents` | `agents:list` | List agents (paginated) |
| `invoke_agent` | `agents:invoke` | Send a task to an agent |
| `get_agent_task` | `tasks:read` | Get task status and result |
| `agent__{slug}__{tool}` | `agents:invoke` | Per-agent MCP tools (dynamic) |

### Resources

| URI Pattern | Description |
|-------------|-------------|
| `agent://{agent_id}/config` | Agent configuration |
| `agent://{agent_id}/tasks/recent` | Last 20 tasks |
| `task://{task_id}` | Specific task details |
| `agent://{agent_id}/activity` | Last 50 activity events |

### Resource Templates

| Template | Description |
|----------|-------------|
| `task://{task_id}` | Any task by ID |
| `agent://{agent_id}/activity` | Agent activity log |

### Prompts

| Prompt | Arguments | Description |
|--------|-----------|-------------|
| `summarize_agent` | `agent_id` | Summarize agent activity and status |
| `debug_task_failure` | `task_id` | Debug a failed task |
| `invoke_with_context` | `agent_id`, `prompt`, `context?` | Invoke agent with additional context |
| `chat_{agent_slug}` | `message` | Per-agent chat prompt |

## Transport

### HTTP POST (JSON-RPC 2.0)
- **URL**: `POST /mcp`
- **Auth**: Bearer token in `Authorization` header
- **Content-Type**: `application/json`

### SSE (Server-Sent Events)
- **URL**: `GET /mcp` with `Accept: text/event-stream`
- **Auth**: Bearer token in `Authorization` header
- **Events**: `endpoint`, `message`, heartbeat every 15s

## Synchronous Invocation

By default, `invoke_agent` returns immediately with the task ID. For synchronous responses:

```json
{
  "name": "invoke_agent",
  "arguments": {
    "agent_id": "AGENT_UUID",
    "prompt": "Analyze this data",
    "wait_seconds": 60
  }
}
```

- `wait_seconds=0`: Fire-and-forget (returns task_id immediately)
- `wait_seconds=1-120`: Wait up to N seconds for the result

## Per-Agent MCP Tools

Agents with `mcp_servers` configured expose their own tools through the MCP protocol. These tools appear as `agent__{slug}__{tool_name}` in the tools list.

Example: An agent named "Data Analyst" with slug `data-analyst` that has an MCP server with a `query_database` tool would expose:
```
agent__data-analyst__query_database
```

## Rate Limiting

- **Limit**: 60 requests per minute per token
- **Response**: HTTP 429 with `Retry-After` header when exceeded

## Monitoring

### Health Check
```bash
curl https://your-instance.com/mcp/health
```

### Usage Analytics (admin only)
```bash
curl https://your-instance.com/mcp/analytics \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"
```

### Token Rotation
```bash
curl -X POST https://your-instance.com/api/mcp-access/access-tokens/{TOKEN_ID}/rotate \
  -H "Authorization: Bearer ADMIN_JWT_TOKEN"
```

## Error Codes

| Code | Meaning |
|------|---------|
| `-32700` | Parse error (invalid JSON) |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32000` | Internal server error |
| HTTP 401 | Invalid or expired token |
| HTTP 429 | Rate limit exceeded |
