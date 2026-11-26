import json

import pytest

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO_EXCEL = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: Excel (.xlsx).]\nHazme un Excel con las ventas de enero a marzo de 3 sucursales "
    "(Norte 120, 135, 150; Centro 200, 190, 210; Sur 80, 95, 110) y un gráfico de evolución por sucursal."
)
FILAS_INVENTADAS = [
    ["Norte 1", 120, 135, 150, 405],
    ["Norte 2", 135, 150, 165, 450],
    ["Norte 3", 150, 165, 180, 495],
    ["Centro 1", 200, 190, 210, 600],
    ["Sur 1", 80, 95, 110, 285],
]
FILAS_BIEN = [["Norte", 120, 135, 150, 405], ["Centro", 200, 190, 210, 600], ["Sur", 80, 95, 110, 285]]


def _turno_excel(filas, mensaje=PEDIDO_EXCEL, otra_hoja=None):
    hojas = [{"name": "Ventas", "headers": ["Sucursal", "Enero", "Febrero", "Marzo", "Total"], "rows": filas}]
    if otra_hoja:
        hojas.append(otra_hoja)
    return [
        {"role": "user", "content": mensaje},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "hq_xlsx_generate", "arguments": json.dumps({"sheets": hojas})}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True, "path": "outputs/v.xlsx"})},
    ]


class TestExcelConFilasInventadas:
    def test_caso_real_norte_1_2_3(self):
        inventadas = runner.filas_inventadas_en_excel(_turno_excel(FILAS_INVENTADAS), 0, PEDIDO_EXCEL)
        assert inventadas == ["Norte 1", "Norte 2", "Norte 3", "Centro 1", "Sur 1"]
        pendientes = runner.verificar_afirmaciones(
            "✅ Listo.\nMEDIA: outputs/v.xlsx", _turno_excel(FILAS_INVENTADAS), 0, mensaje=PEDIDO_EXCEL
        )
        assert "excel_inventado" in [p["tipo"] for p in pendientes]

    def test_aviso_final_no_adjunta_el_excel_equivocado(self):
        pendientes = [{"tipo": "excel_inventado", "frase": "Norte 1, Norte 2"}]
        final = runner.respuesta_verificada("✅ Listo.\nMEDIA: outputs/v.xlsx", pendientes)
        assert "MEDIA" not in final and "Norte 1, Norte 2" in final and "no te lo adjunto" in final

    @pytest.mark.parametrize(
        ("filas", "otra_hoja"),
        [
            (FILAS_BIEN, None),
            (
                [["Sucursal Norte", 120, 135, 150], ["Sucursal Centro", 200, 190, 210], ["Sucursal Sur", 80, 95, 110]],
                None,
            ),
            (FILAS_BIEN + [["Total", 400, 420, 470, 1290], ["Promedio", 133, 140, 157, 430]], None),
            (
                FILAS_BIEN,
                {
                    "name": "Gráfico",
                    "headers": ["Mes", "Norte", "Centro", "Sur"],
                    "rows": [["Enero", 120, 200, 80], ["Febrero", 135, 190, 95], ["Marzo", 150, 210, 110]],
                },
            ),
            (FILAS_BIEN, {"name": "Q", "headers": ["Periodo", "Ventas"], "rows": [["Q1 2026", 1290]]}),
        ],
    )
    def test_filas_legitimas_no_se_marcan(self, filas, otra_hoja):
        assert runner.filas_inventadas_en_excel(_turno_excel(filas, otra_hoja=otra_hoja), 0, PEDIDO_EXCEL) == []

    def test_sin_datos_en_el_mensaje_no_aplica(self):
        mensaje = "créame un Excel con ventas de ejemplo por sucursal"
        assert runner.filas_inventadas_en_excel(_turno_excel(FILAS_INVENTADAS, mensaje), 0, mensaje) == []


REPORTE_COPIADO = (
    "📬 INFORME DE CORREOS — 23:40\n\n## 🔵 FRÍO (3)\n| ID | Edad | Asunto | Remitente | Relación | Acción requerida | "
    "Plazo | Respuesta |\n|---|---|---|---|---|---|---|---|\n| #389 | 2 h 25 min | Batería firma 12-11 | Laura | Para "
    "mí | — | Sin plazo | No |"
)


class TestInformeDeCorreoCopiado:
    def test_caso_real_sin_ms365_correo_sync_es_una_copia(self):
        mensajes = [{"role": "user", "content": "Hazme de nuevo el informe unificado de correos"}]
        pendientes = runner.verificar_afirmaciones(REPORTE_COPIADO, mensajes, 0, mensaje=mensajes[0]["content"])
        assert [p["tipo"] for p in pendientes] == ["informe_copiado"]
        final = runner.respuesta_verificada(REPORTE_COPIADO, pendientes)
        assert "#389" not in final and "No revisé tu bandeja" in final

    def test_con_el_commit_del_turno_no_se_toca(self):
        mensajes = [
            {"role": "user", "content": "informe de correos"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "ms365_correo_sync"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True, "tablas_md": {}})},
        ]
        assert runner.verificar_afirmaciones(REPORTE_COPIADO, mensajes, 0, mensaje="informe de correos") == []

    def test_un_cron_no_se_revisa_por_esta_regla(self):
        mensajes = [{"role": "user", "content": "Ejecuta la skill correo-unificado-bruno"}]
        assert runner.verificar_afirmaciones(REPORTE_COPIADO, mensajes, 0, programada=True) == []

    def test_otras_tablas_no_cuentan(self):
        tabla = "| Sucursal | Enero |\n|---|---|\n| Norte | 120 |"
        assert runner.verificar_afirmaciones(tabla, [{"role": "user", "content": "x"}], 0) == []


class _Agente:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.llamadas.append(user_message)
        texto, llamadas = self.respuestas.pop(0)
        mensajes = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        for i, (nombre, argumentos) in enumerate(llamadas):
            mensajes += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"c{i}", "function": {"name": nombre, "arguments": json.dumps(argumentos)}}],
                },
                {"role": "tool", "content": '{"success": true}', "tool_call_id": f"c{i}"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 1 + len(llamadas)}


def test_en_el_runner_el_reintento_rehace_el_excel_con_las_filas_del_usuario(monkeypatch, tmp_path):
    import test_codigos_inventados as base

    agente = _Agente(
        [
            ("✅ Listo.", [("hq_xlsx_generate", {"sheets": [{"name": "V", "rows": FILAS_INVENTADAS}]})]),
            (
                "✅ Listo, con tus 3 sucursales.",
                [("hq_xlsx_generate", {"sheets": [{"name": "V", "rows": FILAS_BIEN}]})],
            ),
        ]
    )
    monkeypatch.setattr(base, "_Agente", lambda respuestas: agente)
    res, _ = base._correr(monkeypatch, tmp_path, [], PEDIDO_EXCEL)
    assert len(agente.llamadas) == 2 and "Norte 1" in agente.llamadas[1]
    assert res["verificacion"]["resultado"] == "corregida"
    assert res["final_response"] == "✅ Listo, con tus 3 sucursales."
