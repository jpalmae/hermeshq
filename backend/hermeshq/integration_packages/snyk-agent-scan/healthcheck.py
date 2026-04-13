from __future__ import annotations

import asyncio
import subprocess
import venv
from pathlib import Path

PACKAGE_SPEC = "snyk-agent-scan==0.4.15"


async def test_connection(config: dict, resolve_secret):
    secret_ref = str(config.get("api_key_ref") or "").strip()
    if not secret_ref:
        return False, "Snyk token secret is not configured.", None

    token = resolve_secret(secret_ref)
    if asyncio.iscoroutine(token):
        token = await token
    if not token:
        return False, "Configured Snyk token secret could not be resolved.", None

    try:
        executable, version = _ensure_runner(Path(config.get("__workspaces_root") or "/tmp"))
    except Exception as exc:
        return False, f"Could not bootstrap Snyk Agent Scan: {exc}", None

    return True, f"Snyk Agent Scan is ready ({version}).", {"executable": str(executable), "version": version}


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
