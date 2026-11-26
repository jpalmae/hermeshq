from hermeshq.services import env_sanitize


class TestCredencialesDeBaseDeDatos:
    def test_variables_de_postgres_no_llegan_a_los_procesos_de_agente(self, monkeypatch):
        for clave in (
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
            "POSTGRES_USER",
            "PGPASSWORD",
            "PGPASSFILE",
            "PGUSER",
            "PGHOST",
            "PGSERVICEFILE",
            "SQLALCHEMY_DATABASE_URI",
        ):
            monkeypatch.setenv(clave, "valor-de-prueba")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("TZ", "America/Santiago")
        entorno = env_sanitize.build_safe_env()
        assert not any(k.startswith(("POSTGRES_", "PG", "SQLALCHEMY_")) or k == "DATABASE_URL" for k in entorno)
        assert entorno["PATH"] == "/usr/bin" and entorno["TZ"] == "America/Santiago"

    def test_control_negativo_sin_las_claves_nuevas_se_filtrarian(self, monkeypatch):
        monkeypatch.setattr(
            env_sanitize,
            "SENSITIVE_ENV_KEYS",
            tuple(k for k in env_sanitize.SENSITIVE_ENV_KEYS if not k.startswith(("POSTGRES_", "PG", "SQLALCHEMY_"))),
        )
        monkeypatch.setenv("PGPASSWORD", "valor-de-prueba")
        assert "PGPASSWORD" in env_sanitize.build_safe_env()
