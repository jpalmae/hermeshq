export type AgentStatus = "running" | "stopped" | "starting" | "error" | "paused";
export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "user";
  is_active: boolean;
  theme_preference: "default" | "dark" | "light" | "system";
  avatar_url: string | null;
  has_avatar: boolean;
}

export interface ManagedUser extends User {
  assigned_agent_ids: string[];
}

export interface Node {
  id: string;
  name: string;
  hostname: string;
  node_type: string;
  status: string;
  ssh_user: string | null;
  ssh_port: number;
  max_agents: number;
  system_info: Record<string, unknown>;
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string;
}

export interface Agent {
  id: string;
  node_id: string;
  name: string;
  friendly_name: string | null;
  slug: string;
  avatar_url: string | null;
  has_avatar: boolean;
  description: string | null;
  status: AgentStatus;
  run_mode: string;
  model: string;
  provider: string;
  api_key_ref: string | null;
  base_url: string | null;
  system_prompt: string | null;
  workspace_path: string;
  enabled_toolsets: string[];
  disabled_toolsets: string[];
  skills: string[];
  team_tags: string[];
  supervisor_agent_id: string | null;
  total_tasks: number;
  total_tokens_used: number;
  last_activity: string | null;
  created_at: string;
  updated_at: string;
  node?: Node | null;
}

export interface AppSettings {
  id: string;
  app_name: string | null;
  app_short_name: string | null;
  theme_mode: "dark" | "light" | "system";
  default_provider: string | null;
  default_model: string | null;
  default_api_key_ref: string | null;
  default_base_url: string | null;
  logo_url: string | null;
  favicon_url: string | null;
  has_logo: boolean;
  has_favicon: boolean;
  created_at: string;
  updated_at: string;
}

export interface Task {
  id: string;
  agent_id: string;
  title: string | null;
  prompt: string;
  status: TaskStatus;
  priority: number;
  response: string | null;
  error_message: string | null;
  messages_json: Array<{ role: string; content: string }>;
  tool_calls: Array<{ name: string; status: string; payload: Record<string, unknown> }>;
  tokens_used: number;
  iterations: number;
  queued_at: string;
  started_at: string | null;
  completed_at: string | null;
  metadata: Record<string, unknown>;
}

export interface ScheduledTask {
  id: string;
  agent_id: string;
  name: string;
  cron_expression: string;
  prompt: string;
  enabled: boolean;
  last_run: string | null;
  next_run: string | null;
  created_at: string;
  updated_at: string;
}

export interface DashboardOverview {
  stats: {
    total_agents: number;
    active_agents: number;
    total_tasks: number;
    queued_tasks: number;
  };
  activity: Array<{
    id: string;
    event_type: string;
    message: string | null;
    severity: string;
    created_at: string;
  }>;
}

export interface RealtimeEvent {
  type: string;
  agent_id?: string;
  task_id?: string;
  status?: string;
  message?: string;
  response?: string;
  step?: number;
}

export interface SkillCatalogItem {
  name: string;
  description: string;
  identifier: string;
  source: string;
  trust_level?: string;
  repo?: string | null;
  path?: string | null;
  tags?: string[];
  extra?: Record<string, unknown>;
}

export interface SkillCatalogResponse {
  skills: SkillCatalogItem[];
  count: number;
  query: string;
}

export interface InstalledSkill {
  name: string;
  description: string;
  path?: string;
  managed: boolean;
  identifier?: string;
  source?: string;
}

export interface AgentSkillState {
  agent_id: string;
  assigned: string[];
  installed: InstalledSkill[];
  count: number;
}

export interface MessagingChannel {
  id: string;
  agent_id: string;
  platform: string;
  enabled: boolean;
  mode: string;
  secret_ref: string | null;
  allowed_user_ids: string[];
  home_chat_id: string | null;
  home_chat_name: string | null;
  require_mention: boolean;
  free_response_chat_ids: string[];
  unauthorized_dm_behavior: string;
  status: string;
  last_error: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface MessagingChannelRuntime {
  status: string;
  pid: number | null;
  log_path: string | null;
}
