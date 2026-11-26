import json

import pytest

from hermeshq.services.clasificacion_herramientas import (
    ERROR_EXTERNO,
    ERROR_INTERNO,
    OK,
    RECHAZO_GUARDIA,
    SIN_RESULTADO,
    VISTA_PREVIA,
    clasificar,
)

CASOS = [
    ("ms365_mail_send", '{"success": true, "message": "Correo enviado correctamente.", "recipients": 2}', OK),
    (
        "ms365_mail_send",
        '{"success": false, "requiere_aprobacion": true, "vista_previa": {"accion": "enviar correo", "para": ["a@b.cl"]}}',
        VISTA_PREVIA,
    ),
    (
        "ms365_mail_send",
        '{"success": false, "error": "No puedo completar un envío de correo en modo automático (tarea programada o en segundo plano): no hay un usuario que apruebe la vista previa."}',
        RECHAZO_GUARDIA,
    ),
    (
        "hq_pptx_generate",
        '{"success": false, "error": "diapositiva 4: el modelo \'contenido-panel-cian\' no se usa en el tipo \'propuesta-tecnica\'. Permitidos: []", "detalle": null}',
        RECHAZO_GUARDIA,
    ),
    (
        "hq_pptx_generate",
        '{"success": false, "error": "ripts/componentes.py\\", line 500, in proceso\\n    title_size = fit_size([it.get(1)])\\n ^^^^^^"}',
        ERROR_INTERNO,
    ),
    (
        "sharepoint_upload_file",
        '{"success": false, "error": "No existe el archivo \'outputs/x.xlsx\'."}',
        RECHAZO_GUARDIA,
    ),
    (
        "sharepoint_download_file",
        "{\"success\": false, \"error\": \"{'code': 'itemNotFound', 'message': 'The resource could not be found.'}\"}",
        ERROR_EXTERNO,
    ),
    ("ms365_mail_folders", '{"success": false, "error": "Error HTTP 502"}', ERROR_EXTERNO),
    (
        "ms365_correo_sync",
        '{"success": false, "error": "ms365_correo_sync(plan): The read operation timed out"}  [Tool loop warning: repeated_exact_failure_warning; count=3]',
        ERROR_EXTERNO,
    ),
    (
        "sharepoint_search",
        "Tool 'sharepoint_search' does not exist. Available tools: delegate_task, execute_code",
        ERROR_INTERNO,
    ),
    (
        "hq_leer_documento",
        "{\"error\": \"Tool execution failed: AttributeError: 'str' object has no attribute 'get'\"}",
        ERROR_INTERNO,
    ),
    (
        "execute_code",
        '{"status": "error", "output": "\\n--- stderr ---\\nTraceback (most recent call last):\\n"}',
        ERROR_INTERNO,
    ),
    ("execute_code", '{"status": "success", "output": "listo"}', OK),
    ("read_file", "contenido plano del archivo", OK),
    ("read_file", "", SIN_RESULTADO),
    ("read_file", None, SIN_RESULTADO),
    (
        "sharepoint_share_file",
        '{"success": false, "error": "No se pudo obtener token M365: This agent is not assigned to this user."}',
        ERROR_EXTERNO,
    ),
]


@pytest.mark.parametrize("nombre,resultado,esperado", CASOS)
def test_clasificacion_de_resultados_reales(nombre, resultado, esperado):
    assert clasificar(nombre, resultado) == esperado


def test_un_rechazo_intencional_no_cuenta_como_falla():
    from hermeshq.services.clasificacion_herramientas import FALLAS

    assert RECHAZO_GUARDIA not in FALLAS and VISTA_PREVIA not in FALLAS and OK not in FALLAS
    assert {ERROR_EXTERNO, ERROR_INTERNO, SIN_RESULTADO} == set(FALLAS)


def test_el_resultado_puede_venir_como_diccionario():
    assert clasificar("x", {"success": True}) == OK
    assert clasificar("x", json.loads('{"success": false, "error": "falta el campo \'fuente\'"}')) == RECHAZO_GUARDIA


def test_el_runner_carga_el_clasificador_y_clasifica():
    import importlib.util
    from pathlib import Path

    ruta = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
    spec = importlib.util.spec_from_file_location("runner_clasificacion_prueba", ruta)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert (
        runner._clasificar_resultado("ms365_mail_folders", '{"success": false, "error": "Error HTTP 502"}')
        == ERROR_EXTERNO
    )
    assert runner._clasificar_resultado("read_file", "texto") == OK


from conftest import requires_database  # noqa: E402


@requires_database
class TestRegistroEnLaBase:
    async def test_activacion_y_fin_de_herramienta_quedan_en_activity_logs(self, db_engine):
        from sqlalchemy import text
        from test_execution_lease import _seed_task
        from test_execution_lease_e2e import _maker, _orphan

        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        store, lease = await _orphan(maker, task_id, "host-prueba", tool_events=0, ttl=60)
        await store.registrar_activacion_herramienta(lease, agent_id, "sharepoint_search")
        await store.registrar_fin_herramienta(lease, agent_id, "sharepoint_search", "ok", 120)
        await store.registrar_fin_herramienta(lease, agent_id, "ms365_mail_send", "error_externo", 5)
        async with maker() as session:
            filas = (
                await session.execute(
                    text(
                        "select event_type, message, details->>'clase', details->>'attempt_id', severity "
                        "from activity_logs where task_id = :t order by created_at"
                    ),
                    {"t": task_id},
                )
            ).all()
        assert [(f[0], f[1], f[2]) for f in filas] == [
            ("task.tool_activated", "sharepoint_search", None),
            ("task.tool_finished", "sharepoint_search", "ok"),
            ("task.tool_finished", "ms365_mail_send", "error_externo"),
        ]
        assert {f[3] for f in filas} == {lease.attempt_id}
        assert [f[4] for f in filas] == ["info", "info", "warning"]
