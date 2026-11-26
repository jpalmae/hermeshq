import json
import unittest

import hermeshq.scripts.hermes_task_runner as runner

RUTA = "/app/workspaces/agent-e9df4600/output/prueba_asistentebot-3.xlsx"
TEXTO = f"Archivo creado y guardado correctamente.\n- Sitio SharePoint: SitioTI\n\nMEDIA:{RUTA}"
PEDIDO = "crea un archivo excel llamado prueba_asistentebot y guárdalo en esta ruta de sharepoint TI - Sistemas"


def mensajes(exito=True, ruta=RUTA):
    return [
        {"role": "user", "content": PEDIDO},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "sharepoint_upload_file",
                        "arguments": json.dumps({"file_path": ruta, "folder": "x"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": json.dumps({"success": exito, "name": "prueba_asistentebot.xlsx"}),
        },
    ]


class TestAdjuntoTrasSubida(unittest.TestCase):
    def test_caso_real_la_subida_exitosa_sin_pedir_el_archivo_quita_el_adjunto(self):
        salida = runner.adjunto_quitado_tras_subida(TEXTO, mensajes(), 0, PEDIDO)
        self.assertNotIn("MEDIA", salida)
        self.assertIn("Archivo creado y guardado correctamente.", salida)
        self.assertIn("SitioTI", salida)

    def test_si_el_usuario_pidio_el_archivo_en_el_chat_se_conserva(self):
        for pedido in (
            PEDIDO + ". Adjúntalo aquí",
            "guárdalo en SharePoint y envíamelo",
            "súbelo y mándamelo por el chat",
        ):
            self.assertEqual(runner.adjunto_quitado_tras_subida(TEXTO, mensajes(), 0, pedido), TEXTO, pedido)

    def test_si_la_subida_fallo_el_adjunto_se_conserva(self):
        self.assertEqual(runner.adjunto_quitado_tras_subida(TEXTO, mensajes(exito=False), 0, PEDIDO), TEXTO)

    def test_solo_se_quita_el_adjunto_del_archivo_subido(self):
        texto = TEXTO + "\nMEDIA:/app/workspaces/agent-e9df4600/output/otro-informe.pdf"
        salida = runner.adjunto_quitado_tras_subida(texto, mensajes(), 0, PEDIDO)
        self.assertNotIn("prueba_asistentebot-3.xlsx", salida)
        self.assertIn("MEDIA:/app/workspaces/agent-e9df4600/output/otro-informe.pdf", salida)

    def test_sin_subida_en_el_turno_no_cambia_nada(self):
        solo_generar = [m for m in mensajes()[:1]]
        self.assertEqual(runner.adjunto_quitado_tras_subida(TEXTO, solo_generar, 0, PEDIDO), TEXTO)

    def test_una_subida_de_un_turno_anterior_no_cuenta(self):
        self.assertEqual(runner.adjunto_quitado_tras_subida(TEXTO, mensajes(), 3, PEDIDO), TEXTO)
