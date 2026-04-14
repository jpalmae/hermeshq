export type AgentStatus = "running" | "stopped" | "starting" | "error" | "paused";
export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "user";
  is_active: boolean;
  theme_preference: "default" | "dark" | "light" | "system" | "enterprise";
  locale_preference: "default" | "en" | "es";
  avatar_url: string | null;
  has_avatar: boolean;
}

export interface ManagedUser extends User {
  assigned_agent_ids: string[];
}

export interface Secret {
  id: string;
  name: string;
  provider: string | null;
  created_at: string;
  updated_at: string;
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
  runtime_profile: string;
  hermes_version: string | null;
  model: string;
  provider: string;
  api_key_ref: string | null;
  base_url: string | null;
  system_prompt: string | null;
  workspace_path: string;
  enabled_toolsets: string[];
  disabled_toolsets: string[];
  skills: string[];
  integration_configs: Record<string, Record<string, unknown>>;
  team_tags: string[];
  is_system_agent: boolean;
  system_scope: string | null;
  can_receive_tasks: boolean;
  can_send_tasks: boolean;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
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
  theme_mode: "dark" | "light" | "system" | "enterprise";
  default_locale: "en" | "es";
  default_provider: string | null;
  default_model: string | null;
  default_api_key_ref: string | null;
  default_base_url: string | null;
  default_hermes_version: string | null;
  default_tui_skin: string | null;
  tui_skin_filename: string | null;
  logo_url: string | null;
  favicon_url: string | null;
  has_tui_skin: boolean;
  has_logo: boolean;
  has_favicon: boolean;
  created_at: string;
  updated_at: string;
}

export interface HermesVersion {
  version: string;
  release_tag: string | null;
  description: string | null;
  source: "bundled" | "managed" | string;
  installed: boolean;
  install_status: string;
  installed_path: string | null;
  detected_version: string | null;
  is_default: boolean;
  is_effective_default: boolean;
  in_use_by_agents: number;
}

export interface ProviderDefinition {
  slug: string;
  name: string;
  runtime_provider: string;
  auth_type: "api_key" | "oauth_external" | string;
  base_url: string | null;
  default_model: string | null;
  description: string | null;
  docs_url: string | null;
  secret_placeholder: string | null;
  supports_secret_ref: boolean;
  supports_custom_base_url: boolean;
  enabled: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface RuntimeProfileDefinition {
  slug: string;
  name: string;
  description: string;
  typical_roles: string[];
  tooling_summary: string;
  container_intent: string;
  defaults: {
    enabled_toolsets: string[];
    disabled_toolsets: string[];
    max_iterations: number;
    auto_approve_cmds: boolean;
    command_allowlist: string[];
  };
}

export interface RuntimeToolsetDefinition {
  slug: string;
  name: string;
  description: string;
}

export interface RuntimePlatformPluginDefinition {
  slug: string;
  name: string;
  toolset: string;
  description: string;
  standard_compatible: boolean;
}

export interface RuntimeProfileCapabilityDefinition {
  slug: string;
  name: string;
  description: string;
  tooling_summary: string;
  container_intent: string;
  terminal_allowed: boolean;
  phase1_full_access: boolean;
  builtin_toolsets: RuntimeToolsetDefinition[];
}

export interface RuntimeCapabilityOverview {
  profiles: RuntimeProfileCapabilityDefinition[];
  platform_plugins: RuntimePlatformPluginDefinition[];
}

export interface ManagedIntegrationDefinition {
  slug: string;
  name: string;
  description: string;
  version: string;
  source_type: string;
  installed: boolean;
  standard_compatible: boolean;
  supported_profiles: string[];
  required_fields: string[];
  fields: Array<{
    name: string;
    label: string;
    kind: string;
    placeholder: string | null;
    secret_provider: string | null;
  }>;
  defaults: Record<string, string>;
  secret_provider: string | null;
  plugin_slug: string | null;
  plugin_name: string | null;
  plugin_description: string | null;
  skill_identifier: string | null;
  test_action: string | null;
  env_map: Record<string, string>;
  tools: string[];
  actions: Array<{
    slug: string;
    label: string;
    description: string | null;
  }>;
}

export interface Task {
  id: string;
  agent_id: string;
  title: string | null;
  prompt: string;
  status: TaskStatus;
  board_column: string;
  board_order: number;
  board_manual: boolean;
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

export interface TaskBoardUpdate {
  board_column: string;
  board_order?: number | null;
}

export interface TerminalSession {
  id: string;
  agent_id: string;
  node_id: string | null;
  mode: string;
  cwd: string | null;
  command_json: string[];
  status: string;
  started_at: string;
  ended_at: string | null;
  exit_code: number | null;
  input_transcript: string;
  output_transcript: string;
  transcript_text: string;
  created_at: string;
  updated_at: string;
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
