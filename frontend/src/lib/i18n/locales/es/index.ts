import account from "./account";
import agent from "./agent";
import agentBuilder from "./agentBuilder";
import agents from "./agents";
import audit from "./audit";
import common from "./common";
import comms from "./comms";
import dashboard from "./dashboard";
import email from "./email";
import login from "./login";
import mfa from "./mfa";
import nav from "./nav";
import nodes from "./nodes";
import providers from "./providers";
import schedules from "./schedules";
import cloud from "./cloud";
import settings from "./settings";
import shell from "./shell";
import tasks from "./tasks";
import users from "./users";
import v2 from "./v2";

const es: Record<string, string> = {
  ...account,
  ...agent,
  ...agentBuilder,
  ...agents,
  ...audit,
  ...common,
  ...comms,
  ...dashboard,
  ...email,
  ...login,
  ...mfa,
  ...nav,
  ...nodes,
  ...providers,
  ...schedules,
  ...cloud,
  ...settings,
  ...shell,
  ...tasks,
  ...users,
  ...v2,
};

export default es;
