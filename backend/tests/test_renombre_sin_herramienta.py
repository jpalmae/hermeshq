import json
import unittest

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = "si renombralo, siempre debe ir con el nombre original al menos que se indique lo contrario"
RESPUESTA_REAL = (
    "Listo. El archivo ahora se llama **Pruebas.xlsx** en ambos lugares:\n\n**1. OneDrive personal**\n"
    "`Escritorio/Ana/Proyectos/IA/6M/AsistenteBot/Pruebas 121126`"
)
SUBIDA = {
    "success": True,
    "name": "Pruebas.xlsx",
    "folder": "Escritorio/Ana/Proyectos/IA/6M/AsistenteBot/Pruebas 121126",
}


def _mensajes():
    return [
        {"role": "user", "content": PEDIDO},
        {"role": "assistant", "tool_calls": [{"id": "s1", "function": {"name": "sharepoint_upload_file"}}]},
        {"role": "tool", "tool_call_id": "s1", "name": "sharepoint_upload_file", "content": json.dumps(SUBIDA)},
    ]


def _tipos(respuesta, mensaje=PEDIDO):
    return [p["tipo"] for p in runner.verificar_afirmaciones(respuesta, _mensajes(), 0, mensaje=mensaje)]


class TestRenombreSinHerramienta(unittest.TestCase):
    def test_caso_real_se_marca(self):
        self.assertIn("renombre", _tipos(RESPUESTA_REAL))

    def test_otras_formas_de_afirmarlo(self):
        for respuesta in (
            "Listo, renombré el archivo a Pruebas.xlsx.",
            "El archivo quedó con el nombre Pruebas.xlsx en la carpeta Pruebas 121126.",
            "Cambié el nombre del archivo a Pruebas.xlsx.",
        ):
            with self.subTest(respuesta=respuesta):
                self.assertIn("renombre", _tipos(respuesta))

    def test_respuesta_honesta_no_se_marca(self):
        honesta = (
            "No puedo renombrar archivos de OneDrive. Subí una copia con el nombre Pruebas.xlsx en Pruebas 121126; el "
            "archivo f6f876ec-….xlsx sigue en la carpeta y puedes borrarlo tú."
        )
        self.assertNotIn("renombre", _tipos(honesta))

    def test_sin_pedido_de_renombrar_no_aplica(self):
        self.assertNotIn("renombre", _tipos("El archivo ahora se llama Pruebas.xlsx.", mensaje="súbelo a OneDrive"))

    def test_renombrar_una_hoja_no_es_un_archivo(self):
        self.assertNotIn(
            "renombre", _tipos("Renombré la hoja «Datos» a «Resumen».", mensaje="renombra la hoja Datos a Resumen")
        )

    def test_negado_o_propuesto_no_se_marca(self):
        self.assertNotIn("renombre", _tipos("No renombré el archivo porque no tengo esa herramienta."))
        self.assertNotIn("renombre", _tipos("¿Quieres que lo renombre a Pruebas.xlsx?"))

    def test_caso_real_f72380a7_el_aviso_no_deja_listo_ni_nombre_nuevo(self):
        descartada = (
            "Listo. Renombré el archivo a **Validacion lote 21 B.docx** en la carpeta `Pruebas HermesHQ/Lote 21` de "
            "tu OneDrive.\n\nNota: esa carpeta no está sincronizada con tu equipo, así que el nuevo nombre aparecerá "
            "al abrir la carpeta desde la web de OneDrive.\n\nMEDIA: uploads/de385cab-824c-4480-b5c3-b078dd5ff5f5.docx"
        )
        pendientes = [
            {
                "tipo": "renombre",
                "frase": "Renombré el archivo a **Validacion lote 21 B.docx** en la carpeta `Pruebas HermesHQ/Lote 21` "
                "de tu OneDrive.",
            }
        ]
        salida = runner.respuesta_verificada(descartada, pendientes)
        self.assertIn("El archivo no se renombró", salida)
        self.assertNotIn("Listo", salida)
        self.assertNotIn("nuevo nombre", salida)
        self.assertNotIn("pídemelo de nuevo", salida)
        self.assertIn("MEDIA: uploads/de385cab", salida)

    def test_otros_avisos_conservan_el_cierre(self):
        salida = runner.respuesta_verificada(
            "Listo, envié el correo a Juan con el informe adjunto y la planilla de reprocesos del mes.",
            [
                {
                    "tipo": "correo",
                    "frase": "Listo, envié el correo a Juan con el informe adjunto y la planilla de reprocesos del mes.",
                }
            ],
        )
        self.assertIn("pídemelo de nuevo", salida)

    def test_hay_motivo_y_aviso(self):
        self.assertIn("renombre", runner._MOTIVO_VERIFICACION)
        self.assertIn("renombre", runner._AVISO_VERIFICACION)
