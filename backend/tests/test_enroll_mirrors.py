from hermeshq.cli import enroll_device as cli


def _bundle() -> dict:
    return {
        "agent": {
            "slug": "operador",
            "provider": "puget",
            "model": "qwen3.8-27b",
            "model_provider": "hermeshq-openai-compatible",
            "custom_provider": {
                "name": "HermesHQ OpenAI-compatible",
                "base_url": "https://hq.example/api/enrollment/llm/v1",
                "key_env": "OPENAI_API_KEY",
                "default_model": "qwen3.8-27b",
            },
            "base_url": "https://hq.example/api/enrollment/llm/v1",
            "system_prompt": "p",
            "soul_md": "soul",
            "api_key": None,
        },
        "guard": {
            "fail_mode": "fail-closed",
            "plugin_files": {"__init__.py": "GUARDCODE"},
            "env": {"HERMESHQ_AGENT_ID": "a1", "HERMESHQ_AGENT_TOKEN": "t", "HERMESHQ_INTERNAL_API_URL": "https://hq"},
        },
        "llm_proxy": {"enabled": True, "api_key": "tok", "base_url": "https://hq.example/api/enrollment/llm/v1"},
    }


class TestMirrorSync:
    def test_write_home_updates_all_mirrors(self, tmp_path, capsys) -> None:
        home = tmp_path / "home"
        mirror = tmp_path / "runtime" / "profiles" / "operador"
        state = {"hermes_home": str(home), "agent_slug": "operador", "mirrors": [str(mirror)]}
        cli._write_home(state, _bundle())
        for root in (home, mirror):
            assert (root / ".env").exists()
            assert (root / "config.yaml").exists()
            assert (root / "plugins" / "hermeshq_guard" / "__init__.py").read_text() == "GUARDCODE"
            env = (root / ".env").read_text()
            assert "OPENAI_API_KEY=tok" in env
            assert "HERMESHQ_GUARD_FAIL_MODE=fail-closed" in env
        out = capsys.readouterr().out
        assert "Synced bundle" in out and "Synced mirror" in out

    def test_mirror_failure_does_not_break_sync(self, tmp_path, capsys) -> None:
        home = tmp_path / "home"
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file")
        state = {
            "hermes_home": str(home),
            "agent_slug": "operador",
            "mirrors": [str(blocker / "profiles" / "operador")],
        }
        cli._write_home(state, _bundle())
        assert (home / ".env").exists()
        assert "Mirror sync failed" in capsys.readouterr().err

    def test_home_excluded_from_mirrors(self, tmp_path, capsys) -> None:
        home = tmp_path / "home"
        state = {"hermes_home": str(home), "agent_slug": "operador", "mirrors": [str(home)]}
        cli._write_home(state, _bundle())
        assert "Synced mirror" not in capsys.readouterr().out
