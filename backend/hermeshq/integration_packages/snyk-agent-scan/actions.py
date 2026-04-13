from __future__ import annotations

import asyncio
import json
import os
import subprocess
import venv
from pathlib import Path

PACKAGE_SPEC = "snyk-agent-scan==0.4.15"


async def run_action(action_slug: str, *, agent, config: dict, resolve_secret, workspaces_root: Path, package_root: Path | None = None):
    if action_slug != "scan_skills":
        return False, f"Unknown action: {action_slug}", None

    secret_ref = str(config.get("api_key_ref") or "").strip()
    if not secret_ref:
        return False, "Snyk token secret is not configured.", None

    token = resolve_secret(secret_ref)
    if asyncio.iscoroutine(token):
        token = await token
    if not token:
        return False, "Configured Snyk token secret could not be resolved.", None

    skills_root = Path(agent.workspace_path) / ".hermes" / "skills"
    if not skills_root.exists():
        return True, "No installed skills were found for this agent.", {"issue_count": 0, "path_count": 0, "paths": []}

    skill_files = sorted(skills_root.rglob("SKILL.md"))
    if not skill_files:
        return True, "No installed skills were found for this agent.", {"issue_count": 0, "path_count": 0, "paths": []}

    try:
        executable, version = _ensure_runner(workspaces_root)
    except Exception as exc:
        return False, f"Could not bootstrap Snyk Agent Scan: {exc}", None

    storage_file = workspaces_root / "_managed_tools" / "snyk-agent-scan" / "scanner-state.json"
    command = [
        str(executable),
        "--json",
        "--skills",
        "--storage-file",
        str(storage_file),
        str(skills_root),
    ]
    env = {
        **os.environ,
        "SNYK_TOKEN": token,
        "PYTHONUNBUFFERED": "1",
    }
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "Snyk Agent Scan timed out while scanning installed skills.", None

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    parsed = _parse_json_output(stdout)
    summary = _build_summary(parsed)
    summary["version"] = version
    summary["command"] = command
    summary["target"] = str(skills_root)

    if completed.returncode != 0 and parsed is None:
        return False, "Snyk Agent Scan failed to complete.", {**summary, "exit_code": completed.returncode, "stderr": stderr[:4000], "stdout": stdout[:4000]}

    if parsed is not None:
        summary["raw"] = parsed
    elif stdout:
        summary["raw_output"] = stdout[:8000]
    if stderr:
        summary["stderr"] = stderr[:4000]
    summary["exit_code"] = completed.returncode

    issue_count = int(summary.get("issue_count") or 0)
    if issue_count:
        return True, f"Snyk skill scan completed with {issue_count} findings across {summary['path_count']} paths.", summary
    return True, f"Snyk skill scan completed with no findings across {summary['path_count']} paths.", summary


def _ensure_runner(workspaces_root: Path) -> tuple[Path, str]:
    tool_root = workspaces_root / "_managed_tools" / "snyk-agent-scan"
    tool_root.mkdir(parents=True, exist_ok=True)
    python_path = tool_root / "bin" / "python"
    executable = tool_root / "bin" / "snyk-agent-scan"
    if not executable.exists():
        builder = venv.EnvBuilder(with_pip=True, clear=False)
        builder.create(tool_root)
        subprocess.run([str(python_path), "-m", "pip", "install", "--upgrade", "pip"], check=True, capture_output=True, text=True, timeout=180)
        subprocess.run([str(python_path), "-m", "pip", "install", PACKAGE_SPEC], check=True, capture_output=True, text=True, timeout=300)
    probe = subprocess.run([str(executable), "help"], check=True, capture_output=True, text=True, timeout=30)
    version = PACKAGE_SPEC
    if "Snyk Agent Scan v" in probe.stdout:
        version = probe.stdout.split("Snyk Agent Scan v", 1)[1].splitlines()[0].strip()
    return executable, version


def _parse_json_output(stdout: str) -> dict | None:
    if not stdout:
        return None
    try:
        loaded = json.loads(stdout)
        return loaded if isinstance(loaded, dict) else {"result": loaded}
    except Exception:
        return None


def _build_summary(payload: dict | None) -> dict:
    if not payload:
        return {"issue_count": 0, "path_count": 0, "paths": []}

    paths: list[dict] = []
    issue_count = 0
    for path, result in payload.items():
        issues = result.get("issues") if isinstance(result, dict) else []
        issue_total = len(issues) if isinstance(issues, list) else 0
        issue_count += issue_total
        paths.append({"path": str(path), "issue_count": issue_total})
    return {
        "issue_count": issue_count,
        "path_count": len(paths),
        "paths": paths,
    }
