import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
PEDIDO = (
    "NECESITO QUE GUARDES ESTE ARCHIVO EN LAS SIGUIENTES RUTAS 1.- C:\\Users\\usuario1\\OneDrive - Empresa\\"
    "Escritorio\\Ana\\Proyectos\\Demo\\Pruebas 2.- C:\\Users\\usuario1\\OneDrive - Empresa\\"
    "TI - Sistemas\\02 - Proyectos\\00 - Requerimientos\\"
)
SUBIDA_1 = {
    "success": True,
    "name": "Pruebas.xlsx",
    "folder": "Escritorio/Ana/Proyectos/Demo/Pruebas",
}
SUBIDA_2 = {"success": True, "name": "Pruebas.xlsx", "folder": "01 - Recursos"}
RUTA_F7B = "TI - Sistemas\\02 - Proyectos\\00 - Requerimientos\\02 - Area\\03 - Proyecto Demo\\01 - Recursos"
PEDIDO_F7B = f"crea un archivo word en esta ruta {RUTA_F7B} que se llame Prueba SO5"
SUBIDA_F7B = {"success": True, "name": "Prueba SO5.docx", "folder": "01 - Recursos"}
RESPUESTA_F7B = (
    "Listo. Creé el archivo Word y lo guardé en la carpeta de SharePoint.\n\n- **Archivo:** `Prueba SO5.docx`\n"
    "- **Ubicación:** `TI - Sistemas/02 - Proyectos/00 - Requerimientos/02 - Area/03 - Proyecto Demo/01 - Recursos`"
)
RESPUESTA_REAL = (
    "Listo. El archivo **Pruebas.xlsx** quedó guardado en ambas rutas:\n\n**1. OneDrive personal**\n"
    "`Escritorio/Ana/Proyectos/Demo/Pruebas`\n\n**2. SharePoint — TI**\n"
    "`02 - Area / 03 - Proyecto Demo / 01 - Recursos`\n\n"
    "Ya la subí al destino correcto dentro de esa biblioteca."
)


def _mensajes(*subidas):
    mensajes = [{"role": "user", "content": PEDIDO}]
    for i, subida in enumerate(subidas):
        mensajes += [
            {"role": "assistant", "tool_calls": [{"id": f"s{i}", "function": {"name": "sharepoint_upload_file"}}]},
            {"role": "tool", "tool_call_id": f"s{i}", "name": "sharepoint_upload_file", "content": json.dumps(subida)},
        ]
    return mensajes


class TestCarpetaPedida(unittest.TestCase):
    def test_caso_real_la_segunda_ruta_no_tuvo_subida(self):
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_1, SUBIDA_2), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO, resultados), ["00 - Requerimientos"])

    def test_caso_real_el_verificador_lo_marca(self):
        pendientes = runner.verificar_afirmaciones(RESPUESTA_REAL, _mensajes(SUBIDA_1, SUBIDA_2), 0, mensaje=PEDIDO)
        self.assertIn("subida_otra_carpeta", [p["tipo"] for p in pendientes])

    def test_si_reconoce_la_ruta_que_no_pudo_no_se_marca(self):
        honesta = (
            "Quedó guardado en Pruebas 121126. No pude guardarlo en 00 - Requerimientos: es una biblioteca de un sitio "
            "de SharePoint y todavía no puedo subir ahí."
        )
        pendientes = runner.verificar_afirmaciones(honesta, _mensajes(SUBIDA_1), 0, mensaje=PEDIDO)
        self.assertNotIn("subida_otra_carpeta", [p["tipo"] for p in pendientes])

    def test_las_dos_rutas_cumplidas(self):
        ok = {
            "success": True,
            "name": "Pruebas.xlsx",
            "folder": "TI - Sistemas/02 - Proyectos/00 - Requerimientos",
        }
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_1, ok), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO, resultados), [])
        dentro = dict(ok, folder=ok["folder"] + "/Pruebas")
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_1, dentro), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO, resultados), [], "una subcarpeta de la pedida vale")

    def test_ruta_seguida_de_texto_cumplida(self):
        pedido = (
            "Hace un informe interactivo. GUardalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana"
            "\\Proyectos y el archivo que se llame Estructura-Demo.html"
        )
        ok = {"success": True, "name": "Estructura-Demo.html", "folder": "Escritorio/Ana/Proyectos"}
        self.assertEqual(
            runner.carpetas_pedidas_sin_subida(pedido, runner.resultados_de_herramientas(_mensajes(ok), 0)), []
        )
        otra = {"success": True, "name": "Estructura-Demo.html", "folder": "Escritorio"}
        self.assertEqual(
            runner.carpetas_pedidas_sin_subida(pedido, runner.resultados_de_herramientas(_mensajes(otra), 0)),
            ["Proyectos"],
        )

    def test_un_prefijo_de_otra_carpeta_no_la_cumple(self):
        casi = {"success": True, "name": "Pruebas.xlsx", "folder": "Area/00"}
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_1, casi), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO, resultados), ["00 - Requerimientos"])

    def test_sin_carpeta_en_el_resultado_no_se_juzga(self):
        resultados = runner.resultados_de_herramientas(_mensajes({"success": True, "url": "https://od/x"}), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO, resultados), [])

    def test_caso_real_ruta_suelta_misma_ultima_carpeta_en_la_raiz(self):
        pendientes = runner.verificar_afirmaciones(RESPUESTA_F7B, _mensajes(SUBIDA_F7B), 0, mensaje=PEDIDO_F7B)
        self.assertIn("subida_otra_carpeta", [p["tipo"] for p in pendientes])
        self.assertEqual(
            runner.carpetas_pedidas_sin_subida(PEDIDO_F7B, runner.resultados_de_herramientas(_mensajes(SUBIDA_F7B), 0)),
            ["01 - Recursos"],
        )

    def test_ruta_suelta_cumplida_con_texto_delante_y_detras(self):
        ok = dict(SUBIDA_F7B, folder=RUTA_F7B.replace("\\", "/"))
        resultados = runner.resultados_de_herramientas(_mensajes(ok), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(PEDIDO_F7B, resultados), [])

    def test_barras_invertidas_que_no_son_una_carpeta_no_aplican(self):
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_2), 0)
        self.assertEqual(
            runner.carpetas_pedidas_sin_subida("usa la expresión \\d+\\s y súbelo a mi OneDrive", resultados), []
        )

    def test_si_reconoce_que_no_pudo_en_la_ruta_suelta_no_se_marca(self):
        honesta = (
            "Lo dejé en tu OneDrive, en la carpeta 01 - Recursos de la raíz. No pude guardarlo en 01 - Recursos de "
            "TI: es una biblioteca de un sitio de SharePoint."
        )
        pendientes = runner.verificar_afirmaciones(honesta, _mensajes(SUBIDA_F7B), 0, mensaje=PEDIDO_F7B)
        self.assertNotIn("subida_otra_carpeta", [p["tipo"] for p in pendientes])

    def test_replay_ruta_que_termina_en_el_archivo(self):
        base = "C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Proyectos\\Proyecto Demo\\"
        ok = {"success": True, "name": "Informe_V6.html", "folder": "Escritorio/Ana/Proyectos/Proyecto Demo"}
        resultados = runner.resultados_de_herramientas(_mensajes(ok), 0)
        for pedido in (
            f'Modifica "{base}Informe_V5.html" y crea una nueva versión',
            f'Quiero modificar el informe: "{base}Informe_V4.html" realiza los cambios: tabla Subcategoría / Gestión / OBS',
            f'"{base}Modelo_Datos_Demo.html" usa ese archivo y creemos una nueva version en la misma ruta',
        ):
            with self.subTest(pedido=pedido[-60:]):
                self.assertEqual(runner.carpetas_pedidas_sin_subida(pedido, resultados), [])

    def test_caso_real_ee972529_quedo_fuera_de_escritorio(self):
        pedido = (
            "Crea un informe interactivo y guárdalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana"
            "\\Proyectos\\Proyecto Demo\\PROYECTO DEMO\\Etapa 2 - Estructura. Este informe debe presentar la nueva "
            "estructura de carpetas"
        )
        fuera = {
            "success": True,
            "name": "Estructura-Demo.html",
            "folder": "Ana/Proyectos/Proyecto Demo/PROYECTO DEMO/Etapa 2 - Estructura",
        }
        resultados = runner.resultados_de_herramientas(_mensajes(fuera), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(pedido, resultados), ["Etapa 2 - Estructura"])
        dentro = dict(fuera, folder="Escritorio/" + fuera["folder"])
        resultados = runner.resultados_de_herramientas(_mensajes(dentro), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida(pedido, resultados), [])

    def test_sin_rutas_de_windows_no_aplica(self):
        resultados = runner.resultados_de_herramientas(_mensajes(SUBIDA_2), 0)
        self.assertEqual(runner.carpetas_pedidas_sin_subida("súbelo a mi OneDrive", resultados), [])


class TestDestinoNoSoportado(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("sharepoint_destino", PLUGIN)
        self.sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sp)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "uploads").mkdir()
        (Path(self.tmp.name) / "uploads" / "f6.xlsx").write_bytes(b"x")
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.subidas = []

    def _subir(self, args, respuesta=None):
        def fake_put(url, data, token=None, content_range=None):
            self.subidas.append(url)
            return respuesta or {"id": "1", "name": "f6.xlsx", "webUrl": "https://od/x"}

        with (
            patch.object(self.sp, "_task_user_id", return_value="u1"),
            patch.object(self.sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.sp, "_put_binary", side_effect=fake_put),
        ):
            return json.loads(self.sp._upload_file_tool({"file_path": "uploads/f6.xlsx", **args}))

    def test_caso_real_item_id_se_rechaza_en_vez_de_subir_a_la_raiz(self):
        salida = self._subir({"folder": "01 - Recursos", "item_id": "01CM9RSSTCGTRXTZ8JQMLRJDRFIVGJLWEF"})
        self.assertFalse(salida["success"])
        self.assertIn("no acepta item_id", salida["error"])
        self.assertEqual(self.subidas, [], "no crea «01 - Recursos» en la raíz del OneDrive")

    def test_caso_real_id_en_otro_parametro_se_rechaza(self):
        salida = self._subir(
            {
                "folder": "01 - Recursos",
                "name": "Prueba SO5.docx",
                "aprobacion_id": "01CM9RSSTCGTRXTZ8JQMLRJDRFIVGJLWEF",
            }
        )
        self.assertFalse(salida["success"])
        self.assertIn("no acepta item_id", salida["error"])
        self.assertEqual(self.subidas, [])
        salida = self._subir({"folder": "01CM9RSSTCGTRXTZ8JQMLRJDRFIVGJLWEF"})
        self.assertFalse(salida["success"])
        self.assertEqual(self.subidas, [])

    def test_parametros_normales_suben(self):
        salida = self._subir({"folder": "Escritorio/Ana/Pruebas 121126", "name": "Prueba SO5.docx"})
        self.assertTrue(salida["success"], salida)
        self.assertEqual(len(self.subidas), 1)

    def test_caso_real_biblioteca_sincronizada_explica_que_no_se_puede(self):
        salida = self._subir(
            {"folder": "C:\\Users\\d\\OneDrive - Empresa\\TI - Sistemas\\02 - Proyectos\\00 - Requerimientos"},
            respuesta={"error": '{"error":{"code":"itemNotFound","message":"Item not found"}}'},
        )
        self.assertFalse(salida["success"])
        self.assertIn("biblioteca de un sitio de SharePoint", salida["error"])
        self.assertIn("No lo subas a otra carpeta", salida["error"])


class TestCarpetaInventadaEnLaRaiz(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("sharepoint_raiz", PLUGIN)
        self.sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sp)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "outputs").mkdir()
        (Path(self.tmp.name) / "outputs" / "a.docx").write_bytes(b"PK docx")
        self.subidas, self.consultas = [], []

    def _subir(self, mensaje, folder, raiz=None, anterior=""):
        def fake_graph(method, path, token, payload=None):
            self.consultas.append((method, path))
            if isinstance(raiz, Exception):
                raise raiz
            return raiz if raiz is not None else {"error": '{"error":{"code":"itemNotFound"}}'}

        def fake_put(url, data, token=None, content_range=None):
            self.subidas.append(url)
            return {"id": "1", "name": "a.docx", "webUrl": "https://od/x"}

        payload = {"metadata": {"mensaje_usuario": mensaje}, "pedido_anterior": anterior}
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload), "HERMES_SESSION_CWD": self.tmp.name}),
            patch.object(self.sp, "_task_user_id", return_value="u1"),
            patch.object(self.sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.sp, "_graph", side_effect=fake_graph),
            patch.object(self.sp, "_put_binary", side_effect=fake_put),
        ):
            return json.loads(self.sp._upload_file_tool({"file_path": "outputs/a.docx", "folder": folder}))

    def test_caso_real_ee972529_falta_escritorio(self):
        pedido = (
            "guárdalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Proyectos\\Proyecto Demo"
            "\\PROYECTO DEMO\\Etapa 2 - Estructura"
        )
        salida = self._subir(pedido, "Ana/Proyectos/Proyecto Demo/PROYECTO DEMO/Etapa 2 - Estructura")
        self.assertFalse(salida["success"])
        self.assertIn("no existe en la raíz", salida["error"])
        self.assertEqual(self.subidas, [])

    def test_caso_real_f7b89dd2_ultima_carpeta_en_la_raiz(self):
        salida = self._subir(PEDIDO_F7B, "01 - Recursos")
        self.assertFalse(salida["success"])
        self.assertEqual(self.subidas, [])

    def test_caso_real_9061c5b6_ultima_carpeta_en_la_raiz(self):
        salida = self._subir(PEDIDO, "01 - Recursos")
        self.assertFalse(salida["success"])
        self.assertEqual(self.subidas, [])

    def test_la_ruta_del_usuario_sube_aunque_no_exista(self):
        salida = self._subir(PEDIDO_F7B, RUTA_F7B.replace("\\", "/"))
        self.assertTrue(salida["success"], salida)
        salida = self._subir(PEDIDO, "Escritorio/Ana/Proyectos/Demo/Pruebas")
        self.assertTrue(salida["success"], salida)

    def test_carpeta_nombrada_o_pedida_crear(self):
        self.assertTrue(self._subir("sube el acta a la carpeta Entregas", "Entregas")["success"])
        self.assertTrue(self._subir("crea una carpeta Informes 2026 y guárdalo ahí", "Informes 2026")["success"])
        self.assertTrue(
            self._subir("sí, créala", "Proyecto X", anterior="guárdalo en la carpeta Proyecto X")["success"]
        )

    def test_carpeta_existente_sube(self):
        salida = self._subir(PEDIDO_F7B, "Documentos/Varios", raiz={"id": "d1", "name": "Documentos", "folder": {}})
        self.assertTrue(salida["success"], salida)

    def test_acceso_directo_a_biblioteca_explica_sin_subir(self):
        salida = self._subir(
            PEDIDO_F7B,
            RUTA_F7B.replace("\\", "/"),
            raiz={"id": "s1", "name": "TI - Sistemas", "remoteItem": {"id": "r"}},
        )
        self.assertFalse(salida["success"])
        self.assertIn("biblioteca de un sitio de SharePoint", salida["error"])
        self.assertEqual(self.subidas, [])

    def test_sin_red_o_sin_pedido_no_se_juzga(self):
        self.assertTrue(self._subir(PEDIDO, "01 - Recursos", raiz=OSError("sin red"))["success"])
        self.assertTrue(self._subir("", "01 - Recursos")["success"])
