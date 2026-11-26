from __future__ import annotations

import json

from hermeshq.models.task import Task
from tests.test_referencias_correo import _Historial


class TestSeguimientoConservaLaIntegracion:
    AGENTE = ["safe", "file", "hermeshq_ms365_mail", "hermeshq_sharepoint", "hermeshq_xlsx"]

    def _filtrar(self, prompt, herramientas):
        from hermeshq.services.toolsets_pedido import filtrar_para_tarea

        historial = _Historial([{"role": "user", "content": "respóndele al segundo"}])
        historial.herramientas = herramientas
        tarea = Task(prompt=prompt, metadata_json={"source": "public_chat"})
        return filtrar_para_tarea(tarea, list(self.AGENTE), historial)

    def test_el_seguimiento_de_una_accion_de_correo_conserva_el_correo(self):
        elegidos = self._filtrar("el de las 09:00", ["ms365_correo_sync"])
        assert "hermeshq_ms365_mail" in elegidos and "hermeshq_sharepoint" not in elegidos
        assert "hermeshq_xlsx" in elegidos

    def test_sin_uso_previo_el_correo_no_viaja(self):
        assert "hermeshq_ms365_mail" not in self._filtrar("el de las 09:00", [])

    def test_otra_senal_mas_lo_usado(self):
        elegidos = self._filtrar("súbelo a sharepoint", ["ms365_mail_reply"])
        assert {"hermeshq_ms365_mail", "hermeshq_sharepoint"} <= set(elegidos)


MID_COMPAT = "AAMkAGI2factura" + "Q" * 70


class _GraphCompat:
    def __init__(self):
        self.llamadas: list[tuple[str, str]] = []
        self.existe = True

    def __call__(self, metodo, ruta, token, payload=None, headers=None):
        self.llamadas.append((metodo, ruta.split("?")[0]))
        if ruta.startswith(f"/me/messages/{MID_COMPAT}") and metodo == "GET":
            if not self.existe:
                return {"error": "Correo no encontrado (404).", "_graph_code": "itemNotFound"}
            return {
                "id": MID_COMPAT,
                "subject": "Factura octubre",
                "receivedDateTime": "2026-11-10T10:00:00Z",
                "from": {"emailAddress": {"address": "facturas@proveedor.example", "name": "Proveedor Uno"}},
                "toRecipients": [{"emailAddress": {"address": "ana@hq.example"}}],
                "body": {"contentType": "text", "content": "Adjunto factura."},
            }
        if metodo == "POST" and ruta.endswith("/createReply"):
            return {"id": "borrador-1"}
        if ruta.startswith("/me/messages/borrador-1"):
            return {"body": {"content": "<blockquote>original</blockquote>"}} if metodo == "GET" else {}
        if metodo == "POST" and ruta.endswith(("/reply", "/send")):
            return {}
        return {"error": f"ruta no simulada {metodo} {ruta}"}

    def envios(self) -> int:
        return sum(1 for m, r in self.llamadas if m == "POST" and r.endswith(("/reply", "/send")))


def _plugin_correo():
    import importlib.util
    from pathlib import Path

    ruta = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin"
    spec = importlib.util.spec_from_file_location("ms365_mail_referencias", ruta / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestModoCompatibleUnSoloEnvio:
    def _entorno(self, monkeypatch, tmp_path, tarea, mensaje, anterior=""):
        payload = {
            "task_id": tarea,
            "mensaje": mensaje,
            "respuesta_anterior": anterior,
            "metadata": {"thread_user_id": "ana", "source": "public_chat"},
        }
        monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
        monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")

    def _preparar(self, monkeypatch, tmp_path, existe=True):
        p = _plugin_correo()
        graph = _GraphCompat()
        graph.existe = existe
        resuelto = {
            "success": True,
            "estado": "resuelto",
            "ejecucion": {"task_id": "cron-1", "hora_local": "09:00"},
            "correo": {
                "posicion": 2,
                "display_id": "#849",
                "asunto": "Factura octubre",
                "remitente": "Proveedor Uno",
                "message_id": MID_COMPAT,
            },
        }
        pedidos: list = []
        monkeypatch.setattr(
            p,
            "_llamar_control",
            lambda metodo, ruta, cuerpo=None, timeout=30: pedidos.append((ruta, cuerpo)) or dict(resuelto, _http=200),
        )
        monkeypatch.setattr(p, "_graph", graph)
        monkeypatch.setattr(p, "_broker_token_check", lambda _u: ("token-usuario", ""))
        monkeypatch.setattr(p, "_get_m365_token", lambda _u: ("token-usuario", ""))
        monkeypatch.setattr(p, "_modo_automatico", lambda: False)
        return p, graph, pedidos

    def test_segundo_correo_un_si_envia_una_vez_y_el_segundo_no(self, monkeypatch, tmp_path):
        p, graph, pedidos = self._preparar(monkeypatch, tmp_path)
        self._entorno(monkeypatch, tmp_path, "T1", "quiero trabajar con el segundo correo")
        ref = json.loads(p._correo_sync_tool({"accion": "referencia"}))
        assert ref["success"] is True and ref["correo"]["message_id"] == MID_COMPAT, ref
        assert pedidos == [("/agents/self/correo/referencia", {"task_id": "T1"})]
        vista = json.loads(
            p._reply_email_tool({"message_id": MID_COMPAT, "body": "Gracias, lo reviso.", "firma": "ninguna"})
        )
        assert vista["requiere_aprobacion"] is True and graph.envios() == 0
        mostrado = vista["borrador"] + "\n\n¿Lo envío?"

        self._entorno(monkeypatch, tmp_path, "T2", "sí", mostrado)
        envio = p.ejecutar_aprobacion_pendiente()
        assert envio and envio["resultado"]["success"] is True, envio
        assert graph.envios() == 1

        self._entorno(monkeypatch, tmp_path, "T3", "sí", "Listo, respondí el correo «Factura octubre».")
        assert p.ejecutar_aprobacion_pendiente() is None
        insiste = json.loads(
            p._reply_email_tool(
                {
                    "message_id": MID_COMPAT,
                    "body": "Gracias, lo reviso.",
                    "firma": "ninguna",
                    "aprobado_por_el_usuario": True,
                }
            )
        )
        assert insiste.get("success") is not True and insiste.get("ya_enviado") is True, insiste
        assert graph.envios() == 1

    def test_correo_eliminado_se_informa_y_no_prepara_nada(self, monkeypatch, tmp_path):
        p, graph, _ = self._preparar(monkeypatch, tmp_path, existe=False)
        self._entorno(monkeypatch, tmp_path, "T1", "el segundo")
        ref = json.loads(p._correo_sync_tool({"accion": "referencia"}))
        assert ref["success"] is False and ref["eliminado_o_movido"] is True, ref
        assert "message_id" not in ref["correo"] and "eliminado o movido" in ref["pregunta"]
        assert graph.envios() == 0
