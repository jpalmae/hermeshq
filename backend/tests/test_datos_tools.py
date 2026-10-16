import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_PLUGIN = _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermeshq_documentos_plugin", _PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class TestRegistration(unittest.TestCase):
    def test_only_the_generic_tools_are_registered(self):
        ctx = _Ctx()
        plugin.register(ctx)
        self.assertEqual(
            sorted(ctx.tools),
            ["hq_comparar", "hq_datos_consultar", "hq_datos_exportar", "hq_datos_extraer", "hq_skill_crear"],
        )
        manifiesto = _PLUGIN.with_name("plugin.yaml").read_text(encoding="utf-8")
        for nombre in ctx.tools:
            self.assertIn(nombre, manifiesto)


class TestExtraccionDatos(unittest.TestCase):
    def _excel(self, carpeta, nombre):
        import datetime

        import openpyxl

        libro = openpyxl.Workbook()
        hoja = libro.active
        hoja.title = "Licencias vf"
        hoja["A3"] = "Licenciamiento PBI"
        hoja["F3"] = "Última actualización:"
        hoja["G3"] = datetime.datetime(2026, 9, 30)
        hoja.append([])
        hoja.append([])
        hoja.append([None, "Sucursal", "Usuario", "Estado", "Costo USD", "Vence"])
        for i, estado in enumerate(["Asignada", "Asignada", "Disponible"]):
            hoja.append([None, "Santiago", f"user{i}", estado, 10.5 + i, datetime.datetime(2026, 11, 17 + i)])
        oculta = libro.create_sheet("oculta")
        oculta["A1"] = "x"
        oculta.sheet_state = "hidden"
        ruta = Path(carpeta) / nombre
        libro.save(ruta)
        return ruta

    def _llamar(self, carpeta, **args):
        with patch.dict(os.environ, {"HERMES_SESSION_CWD": carpeta}):
            return json.loads(plugin._extraer_datos(args))

    def test_extracts_everything_without_code_and_ignores_duplicates(self):
        with tempfile.TemporaryDirectory() as carpeta:
            descargas = Path(carpeta) / "sharepoint_downloads"
            descargas.mkdir()
            self._excel(descargas, "d528e476_Licencias.xlsx")
            shutil.copy2(descargas / "d528e476_Licencias.xlsx", descargas / "aaaa1111_Licencias copia.xlsx")
            resultado = self._llamar(carpeta, tema="licencias")
            self.assertTrue(resultado["success"])
            self.assertEqual(len(resultado["archivos"]), 1)
            self.assertEqual(len(resultado["duplicados_ignorados"]), 1)
            hoja = resultado["archivos"][0]["hojas"][0]
            self.assertEqual(hoja["hoja"], "Licencias vf")
            self.assertEqual(hoja["filas"], 3)
            columnas = {c["columna"]: c for c in hoja["columnas"]}
            self.assertEqual(columnas["Estado"]["valores"], {"Asignada": 2, "Disponible": 1})
            self.assertEqual(columnas["Costo USD"]["suma"], 34.5)
            self.assertEqual(columnas["Vence"]["min"], "2026-11-17")
            self.assertIn("Licenciamiento PBI", hoja["notas"][0])
            guardado = json.loads((Path(carpeta) / "work" / "licencias" / "datos.json").read_text(encoding="utf-8"))
            self.assertEqual(guardado["archivos"][0]["hojas"][0]["registros"][2]["Usuario"], "user2")
            self.assertEqual(len(guardado["archivos"][0]["hojas"]), 1)

    def test_rows_of_a_sheet_can_be_read_back_from_the_saved_file(self):
        with tempfile.TemporaryDirectory() as carpeta:
            self._excel(Path(carpeta), "licencias.xlsx")
            self._llamar(carpeta, archivos=["licencias"], tema="licencias")
            filas = self._llamar(carpeta, tema="licencias", hoja="vf", desde=1, limite=1)
            self.assertTrue(filas["success"])
            self.assertEqual(filas["total"], 3)
            self.assertEqual(filas["registros"][0]["Usuario"], "user1")

    def test_missing_files_are_reported_with_the_way_out(self):
        with tempfile.TemporaryDirectory() as carpeta:
            self.assertFalse(self._llamar(carpeta)["success"])
            resultado = self._llamar(carpeta, archivos=["no-existe.xlsx"])
            self.assertFalse(resultado["success"])
            self.assertIn("sharepoint_downloads", resultado["error"])

    def test_csv_with_semicolons(self):
        with tempfile.TemporaryDirectory() as carpeta:
            (Path(carpeta) / "datos.csv").write_text("Area;Monto\nTI;10\nRRHH;5\n", encoding="utf-8")
            resultado = self._llamar(carpeta, archivos=["datos.csv"], tema="csv")
            hoja = resultado["archivos"][0]["hojas"][0]
            self.assertEqual(hoja["filas"], 2)
            self.assertEqual({c["columna"]: c for c in hoja["columnas"]}["Monto"]["suma"], 15)


class TestConsultaDatos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        destino = Path(self.tmp.name) / "work" / "hse"
        destino.mkdir(parents=True)
        filas = [
            {
                "Sucursal": "Arica",
                "Fecha": "2026-01-10",
                "Tipo": "Cuasi Incidente",
                "Costo": 100,
                "Desc": "Caída de carga",
            },
            {"Sucursal": "Arica", "Fecha": "2026-03-02", "Tipo": "STP", "Costo": 250, "Desc": "Corte en mano"},
            {"Sucursal": "Arica", "Fecha": "2025-07-09", "Tipo": "CTP", "Costo": 900, "Desc": "Golpe"},
            {"Sucursal": "Angamos", "Fecha": "2026-05-05", "Tipo": "Cuasi Incidente", "Costo": None, "Desc": "Derrame"},
            {"Sucursal": "Angamos", "Fecha": "2026-06-06", "Tipo": "STP", "Costo": 50, "Desc": ""},
        ]
        datos = {
            "archivos": [
                {
                    "archivo": "t.xlsx",
                    "hojas": [
                        {
                            "nombre": "Inc. Seguridad",
                            "filas": len(filas),
                            "columnas": ["Sucursal", "Fecha", "Tipo", "Costo", "Desc"],
                            "registros": filas,
                        }
                    ],
                }
            ]
        }
        (destino / "datos.json").write_text(json.dumps(datos), encoding="utf-8")
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _consultar(self, **args):
        return json.loads(plugin._consultar_datos({"tema": "hse", "hoja": "seguridad", **args}))

    def test_filters_list_the_matching_rows(self):
        out = (
            self._consultar(
                filtros=[{"columna": "sucursal", "op": "="}, {"columna": "Fecha", "op": "anio", "valor": 2026}],
                columnas=["Fecha", "Tipo"],
            )
            if False
            else self._consultar(
                filtros=[
                    {"columna": "sucursal", "op": "=", "valor": "arica"},
                    {"columna": "Fecha", "op": "anio", "valor": 2026},
                ],
                columnas=["Fecha", "Tipo"],
                ordenar="-Fecha",
            )
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["coinciden"], 2)
        self.assertEqual(
            out["filas"], [{"Fecha": "2026-03-02", "Tipo": "STP"}, {"Fecha": "2026-01-10", "Tipo": "Cuasi Incidente"}]
        )

    def test_grouping_counts_and_sums(self):
        out = self._consultar(
            agrupar_por=["Sucursal", "Tipo"], sumar="Costo", filtros=[{"columna": "Fecha", "op": "anio", "valor": 2026}]
        )
        grupos = {(g["Sucursal"], g["Tipo"]): (g["conteo"], g["suma"]) for g in out["grupos"]}
        self.assertEqual(grupos[("Arica", "STP")], (1, 250.0))
        self.assertEqual(grupos[("Angamos", "Cuasi Incidente")], (1, 0.0))
        self.assertEqual(out["total_grupos"], 4)

    def test_operators_empty_between_in_and_contains(self):
        self.assertEqual(self._consultar(filtros=[{"columna": "Desc", "op": "vacio"}])["coinciden"], 1)
        self.assertEqual(
            self._consultar(filtros=[{"columna": "Costo", "op": "entre", "valor": [90, 300]}])["coinciden"], 2
        )
        self.assertEqual(
            self._consultar(filtros=[{"columna": "Tipo", "op": "en", "valor": ["stp", "ctp"]}])["coinciden"], 3
        )
        self.assertEqual(
            self._consultar(filtros=[{"columna": "Desc", "op": "contiene", "valor": "caida"}])["coinciden"], 1
        )
        self.assertEqual(self._consultar(filtros=[{"columna": "Costo", "op": ">=", "valor": 250}])["coinciden"], 2)

    def test_total_without_filters_and_paging(self):
        out = self._consultar(limite=2, desde=1)
        self.assertEqual((out["coinciden"], len(out["filas"]), out["desde"]), (5, 2, 1))
        self.assertEqual(self._consultar(sumar="Costo")["suma"], 1300.0)

    def test_several_queries_run_in_one_call_and_inherit_the_sheet(self):
        out = json.loads(
            plugin._consultar_datos(
                {
                    "tema": "hse",
                    "hoja": "seguridad",
                    "consultas": [
                        {"agrupar_por": ["Sucursal"]},
                        {"filtros": [{"columna": "Tipo", "op": "=", "valor": "CTP"}], "columnas": ["Desc"]},
                        {"filtros": [{"columna": "nada", "op": "="}]},
                    ],
                }
            )
        )
        self.assertTrue(out["success"])
        grupos, ctp, malo = out["resultados"]
        self.assertEqual({g["Sucursal"]: g["conteo"] for g in grupos["grupos"]}, {"Arica": 3, "Angamos": 2})
        self.assertEqual(ctp["filas"], [{"Desc": "Golpe"}])
        self.assertFalse(malo["success"])

    def test_errors_list_the_available_options(self):
        malo = self._consultar(filtros=[{"columna": "inexistente", "op": "="}])
        self.assertFalse(malo["success"])
        self.assertIn("Columnas:", malo["error"])
        self.assertIn("Hojas:", self._consultar(hoja="zzz")["error"])
        self.assertIn("Usa:", self._consultar(filtros=[{"columna": "Tipo", "op": "parecido", "valor": "x"}])["error"])
        self.assertIn("hq_datos_extraer", json.loads(plugin._consultar_datos({"tema": "otro", "hoja": "x"}))["error"])
