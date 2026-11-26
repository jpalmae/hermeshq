import json
import os
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services.agent_supervisor import history_assistant_text
from tests.test_mail_forward_reply import _Base


class _ProcesoMuerto(BaseException):
    pass


class _TareaCortada:
    status = "failed"
    error_message = "Hermes runtime exceeded the task timeout of 540s"


class TestSiTrasUnTurnoCortado(_Base):
    DATOS = {"message_id": "M1", "to": ["pedro@example.com"], "comment": "Para tu revisión"}

    def _vista(self) -> str:
        with patch.object(self.mod, "_conversacion_actual", return_value=""):
            r = self._ejecutar(self.mod._forward_email_tool, dict(self.DATOS), tarea="tarea-1")
        self.assertTrue(r["requiere_aprobacion"])
        vista = r["vista_previa"]
        return (
            f"Vista previa del reenvío:\n- Asunto: {vista['asunto']}\n- Para: {', '.join(vista['para'])}\n"
            f"- Comentario: {vista['comentario']}\n¿Lo reenvío?"
        )

    def _si(self, tarea: str, historial: list[dict], cortar_en: str | None = None):
        payload = {
            "task_id": tarea,
            "mensaje": "sí",
            "respuesta_anterior": runner._ultima_respuesta(historial),
        }
        base = self._graph()

        def graph(method, path, token, payload=None, **kwargs):
            if cortar_en == "antes" and path.endswith("/createForward"):
                raise _ProcesoMuerto
            if method == "GET" and path.startswith("/me/mailFolders/sentitems/messages"):
                self.llamadas.append((method, path, payload))
                if self.enviados_no_disponible:
                    return {"error": "Error 503: servicio no disponible"}
                return {"value": self._enviados()}
            resultado = base(method, path, token, payload, **kwargs)
            if cortar_en == "despues" and path.endswith("/send"):
                raise _ProcesoMuerto
            return resultado

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.object(self.mod, "_conversacion_actual", return_value=""),
            patch.object(self.mod, "_payload_tarea", return_value=payload),
            patch.object(self.mod, "es_aprobacion_pura", return_value=True),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
        ):
            try:
                return self.mod.ejecutar_aprobacion_pendiente()
            except _ProcesoMuerto:
                return "cortado"

    enviados_no_disponible = False

    def _envios(self) -> int:
        return sum(1 for metodo, ruta, _ in self.llamadas if metodo == "POST" and ruta.endswith("/send"))

    def _enviados(self) -> list[dict]:
        return [
            {
                "subject": "RV: Ticket 4521: acceso USB",
                "toRecipients": [{"emailAddress": {"address": "Pedro@example.com"}}],
                "sentDateTime": "2026-11-16T13:00:00Z",
            }
        ] * self._envios()

    def _historial(self, vista: str) -> list[dict]:
        return [
            {"role": "user", "content": "reenvía a Pedro el correo del ticket"},
            {"role": "assistant", "content": vista},
            {"role": "user", "content": "sí"},
            {"role": "assistant", "content": history_assistant_text(_TareaCortada(), "", 6000)},
        ]

    def test_corte_antes_de_graph_el_segundo_si_reenvia_una_vez(self):
        vista = self._vista()
        self.assertEqual(self._si("tarea-2", [{"role": "assistant", "content": vista}], cortar_en="antes"), "cortado")
        self.assertEqual(self._envios(), 0)
        envio = self._si("tarea-3", self._historial(vista))
        self.assertTrue(envio and envio["resultado"]["success"], envio)
        self.assertEqual(self._envios(), 1)

    def test_corte_despues_de_que_graph_acepto_el_segundo_si_no_duplica(self):
        vista = self._vista()
        self.assertEqual(self._si("tarea-2", [{"role": "assistant", "content": vista}], cortar_en="despues"), "cortado")
        self.assertEqual(self._envios(), 1)
        envio = self._si("tarea-3", self._historial(vista))
        self.assertEqual(self._envios(), 1, f"el segundo «sí» volvió a enviar: {envio}")
        self.assertTrue(envio["resultado"]["ya_enviado"], envio)
        self.assertIn("ya salió en el intento anterior", envio["resultado"]["error"])
        tercero = self._si("tarea-4", self._historial(vista))
        self.assertIsNone(tercero)
        self.assertEqual(self._envios(), 1)

    def test_si_no_se_puede_revisar_enviados_no_reenvia_y_lo_dice(self):
        vista = self._vista()
        self.assertEqual(self._si("tarea-2", [{"role": "assistant", "content": vista}], cortar_en="despues"), "cortado")
        self.enviados_no_disponible = True
        envio = self._si("tarea-3", self._historial(vista))
        self.assertEqual(self._envios(), 1)
        self.assertTrue(envio["resultado"]["resultado_incierto"], envio)
        self.assertIn("revisa tu carpeta Elementos enviados", envio["resultado"]["error"])

    def test_un_envio_normal_no_consulta_enviados(self):
        vista = self._vista()
        envio = self._si("tarea-2", [{"role": "assistant", "content": vista}])
        self.assertTrue(envio["resultado"]["success"], envio)
        self.assertEqual(self._envios(), 1)
        self.assertFalse([r for m, r, _ in self.llamadas if "sentitems" in r])


class TestRespuestaDirectaDelRunner:
    def _correr(self, resultado):
        envio = {"herramienta": "ms365_mail_forward", "args": {"message_id": "M1"}, "resultado": resultado}
        plugin = type(
            "P", (), {"ejecutar_aprobacion_pendiente": lambda self: envio, "es_aprobacion_pura": lambda self: True}
        )()
        with (
            patch.object(runner, "_chat_con_usuario", return_value=True),
            patch.object(runner, "_cargar_plugin_correo", return_value=plugin),
        ):
            return runner._envio_aprobado_directo({"task_id": "t3", "hermes_home": "/tmp/x", "prompt": "sí"})

    def test_ya_enviado_tras_un_corte_se_responde_sin_el_modelo(self):
        texto = "Este correo ya salió en el intento anterior, que se cortó antes de confirmarlo. No lo vuelvo a enviar."
        salida = self._correr({"success": False, "ya_enviado": True, "error": texto})
        assert salida is not None and texto in json.dumps(salida, ensure_ascii=False)
        assert "Correo reenviado" not in json.dumps(salida, ensure_ascii=False)

    def test_un_fallo_comun_sigue_con_el_modelo(self):
        assert self._correr({"success": False, "error": "Error 403"}) is None
