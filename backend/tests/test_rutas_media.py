import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import hermeshq.scripts.hermes_task_runner as runner

RUTA_INVENTADA = "/app/workspaces/agent-d65d10ee-1b83-4500-b09a-15064ecdb1b7/output/mascota-del-proyecto.pdf"


class TestRutasMedia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        (Path(self.cwd) / "output").mkdir()
        (Path(self.cwd) / "output" / "mascota-del-proyecto.pdf").write_bytes(b"%PDF-1.7")

    def mensajes(self, ruta_herramienta):
        return [
            {"role": "user", "content": "arma un PDF"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "hq_pdf_generate"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True, "path": ruta_herramienta})},
        ]

    def test_caso_real_la_ruta_se_corrige_al_archivo_generado_en_el_turno(self):
        texto = f"Listo. PDF de una página.\n\nMEDIA: {RUTA_INVENTADA}"
        mensajes = self.mensajes(os.path.join(self.cwd, "output", "mascota-del-proyecto.pdf"))
        corregido = runner.rutas_media_corregidas(texto, self.cwd, mensajes)
        self.assertIn("MEDIA: output/mascota-del-proyecto.pdf", corregido)
        self.assertEqual(
            [p for p in runner.verificar_afirmaciones(corregido, mensajes, 0, cwd=self.cwd) if p["tipo"] == "adjunto"],
            [],
        )

    def test_un_archivo_viejo_con_el_mismo_nombre_no_se_adjunta(self):
        texto = f"Listo.\n\nMEDIA: {RUTA_INVENTADA}"
        mensajes = self.mensajes(os.path.join(self.cwd, "output", "otro.pdf"))
        self.assertEqual(runner.rutas_media_corregidas(texto, self.cwd, mensajes), texto)

    def test_dos_archivos_con_el_mismo_nombre_no_se_adivina(self):
        (Path(self.cwd) / "uploads").mkdir()
        (Path(self.cwd) / "uploads" / "mascota-del-proyecto.pdf").write_bytes(b"%PDF-1.7")
        texto = "MEDIA: /tmp/mascota-del-proyecto.pdf"
        mensajes = self.mensajes("output/mascota-del-proyecto.pdf")
        self.assertEqual(runner.rutas_media_corregidas(texto, self.cwd, mensajes), texto)

    def test_una_ruta_que_existe_no_se_toca(self):
        texto = "MEDIA: output/mascota-del-proyecto.pdf"
        self.assertEqual(runner.rutas_media_corregidas(texto, self.cwd, self.mensajes("x")), texto)


def _escrito(herramienta, argumentos, resultado):
    return [
        {"role": "user", "content": "hazme la página"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "w1", "function": {"name": herramienta, "arguments": json.dumps(argumentos)}}],
        },
        {"role": "tool", "tool_call_id": "w1", "content": json.dumps(resultado)},
    ]


class TestRaizInventada(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        for carpeta in ("outputs", "work"):
            (Path(self.cwd) / carpeta).mkdir()

    def _corregir(self, ruta, mensajes):
        texto = f"Te generé la página HTML interactiva.\n\nMEDIA: {ruta}"
        corregido = runner.rutas_media_corregidas(texto, self.cwd, mensajes)
        pendientes = [
            p for p in runner.verificar_afirmaciones(corregido, mensajes, 0, cwd=self.cwd) if "adjunto" in p["tipo"]
        ]
        return corregido, pendientes

    def test_operador2_app_outputs(self):
        (Path(self.cwd) / "outputs" / "sede-serv.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito(
            "write_file", {"content": "<html>…", "path": "outputs/sede-serv.html"}, {"bytes_written": 7374}
        )
        corregido, pendientes = self._corregir("/app/outputs/sede-serv.html", mensajes)
        self.assertIn("MEDIA: outputs/sede-serv.html", corregido)
        self.assertEqual(pendientes, [])

    def test_ana_work_con_write_file(self):
        (Path(self.cwd) / "work" / "Estructura_Demo_GRUPO.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito(
            "write_file",
            {"content": "<html>…", "path": "work/Estructura_Demo_GRUPO.html"},
            {"bytes_written": 42262},
        )
        corregido, pendientes = self._corregir("/home/user/work/Estructura_Demo_GRUPO.html", mensajes)
        self.assertIn("MEDIA: work/Estructura_Demo_GRUPO.html", corregido)
        self.assertEqual(pendientes, [])

    def test_ana_raiz_con_execute_code(self):
        (Path(self.cwd) / "Estructura_Apollo_Accesos.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito(
            "execute_code",
            {"code": "p = 'Estructura_Apollo_Accesos.html'\nprint(os.path.getsize(p))"},
            {"output": "size: 60871"},
        )
        corregido, pendientes = self._corregir("/home/ubuntu/workspace/Estructura_Apollo_Accesos.html", mensajes)
        self.assertIn("MEDIA: Estructura_Apollo_Accesos.html", corregido)
        self.assertEqual(pendientes, [])

    def test_con_dos_del_mismo_nombre_gana_el_escrito_en_el_turno(self):
        viejo = Path(self.cwd) / "outputs" / "Estructura_Apollo_Accesos.html"
        viejo.write_text("<html>informe 14:41</html>", encoding="utf-8")
        os.utime(viejo, (time.time() - 1000, time.time() - 1000))
        (Path(self.cwd) / "Estructura_Apollo_Accesos.html").write_text("<html>app 14:58</html>", encoding="utf-8")
        mensajes = _escrito("execute_code", {"code": "p = 'Estructura_Apollo_Accesos.html'"}, {"output": "ok"})
        corregido, _ = self._corregir("/home/ubuntu/workspace/Estructura_Apollo_Accesos.html", mensajes)
        self.assertIn("MEDIA: Estructura_Apollo_Accesos.html", corregido)

    def test_nombre_con_tildes_en_los_argumentos(self):
        (Path(self.cwd) / "outputs" / "Gestión_Áreas.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito("write_file", {"content": "x", "path": "outputs/Gestión_Áreas.html"}, {"bytes_written": 1})
        corregido, _ = self._corregir("/home/user/outputs/Gestión_Áreas.html", mensajes)
        self.assertIn("MEDIA: outputs/Gestión_Áreas.html", corregido)

    def test_archivo_que_no_se_toco_en_el_turno_no_se_adjunta(self):
        (Path(self.cwd) / "outputs" / "viejo.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito("write_file", {"content": "x", "path": "outputs/nuevo.html"}, {"bytes_written": 1})
        corregido, pendientes = self._corregir("/app/outputs/viejo.html", mensajes)
        self.assertIn("MEDIA: /app/outputs/viejo.html", corregido)
        self.assertEqual([p["tipo"] for p in pendientes], ["adjunto"])

    def test_no_sale_del_espacio_de_trabajo(self):
        afuera = Path(self.cwd).parent / "secreto.html"
        mensajes = _escrito("write_file", {"content": "x", "path": "../secreto.html"}, {"bytes_written": 1})
        corregido, _ = self._corregir("/app/workspaces/../secreto.html", mensajes)
        self.assertNotIn(str(afuera), corregido)
        self.assertIn("MEDIA: /app/workspaces/../secreto.html", corregido)

    def test_ruta_del_pc_conserva_el_aviso_para_subirlo(self):
        (Path(self.cwd) / "work" / "x.html").write_text("<html></html>", encoding="utf-8")
        mensajes = _escrito("write_file", {"content": "x", "path": "work/x.html"}, {"bytes_written": 1})
        ruta = "C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Filial Sur\\x.html"
        corregido, pendientes = self._corregir(ruta, mensajes)
        self.assertIn(f"MEDIA: {ruta}", corregido)
        self.assertEqual([p["tipo"] for p in pendientes], ["adjunto_pc"])


if __name__ == "__main__":
    unittest.main()


class TestNotaDeVerificacion(unittest.TestCase):
    def test_con_un_adjunto_pendiente_no_sugiere_subir(self):
        nota = runner.nota_verificacion([{"tipo": "adjunto", "frase": "output/x.pdf"}])
        self.assertNotIn("sharepoint_upload_file", nota)

    def test_con_una_subida_pendiente_si_la_nombra(self):
        nota = runner.nota_verificacion([{"tipo": "subida", "frase": "subido a OneDrive"}])
        self.assertIn("sharepoint_upload_file", nota)
