import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    pytest.importorskip("openpyxl")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_xlsx_editar_prueba", PLUGIN / "__init__.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _libro(ruta: Path, combinadas=False, formulas=False, grafico=False):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Cálculo"
    ws.append(["USUARIO", "NECESIDAD", "PUNTAJE"])
    for u, n, p in [("Ana", "Presentaciones", 4), ("Beto", "Reportes", 3), ("Carla", "Correo", 5)]:
        ws.append([u, n, p])
    for col in "ABC":
        c = ws[f"{col}1"]
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill(start_color="0F3460", end_color="0F3460", fill_type="solid")
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[col].width = 18
    borde = Border(left=Side(style="thin"), right=Side(style="thin"), top=Side(style="thin"), bottom=Side(style="thin"))
    for fila in range(2, 5):
        for col in "ABC":
            ws[f"{col}{fila}"].border = borde
            ws[f"{col}{fila}"].fill = PatternFill(
                start_color="EEF2F8" if fila % 2 else "FFFFFF",
                end_color="EEF2F8" if fila % 2 else "FFFFFF",
                fill_type="solid",
            )
    ws["C2"].number_format = "0.0"
    if formulas:
        ws["C5"] = "=SUM(C2:C4)"
    if combinadas:
        ws.merge_cells("A7:C7")
        ws["A7"] = "Nota final"
    if grafico:
        g = BarChart()
        g.add_data(Reference(ws, min_col=3, min_row=1, max_row=4), titles_from_data=True)
        ws.add_chart(g, "F2")
    wb.create_sheet("Guía").append(["Criterio", "Qué significa"])
    wb.save(ruta)


def _editar(plugin, tmp_path, cambios, **kw):
    (tmp_path / "outputs").mkdir(exist_ok=True)
    _libro(tmp_path / "outputs" / "matriz.xlsx", **kw)
    return json.loads(plugin._handle_edit_xlsx({"path": "matriz.xlsx", "cambios": cambios}))


def _abrir(tmp_path, nombre="matriz_v2.xlsx"):
    from openpyxl import load_workbook

    return load_workbook(tmp_path / "outputs" / nombre)


class TestAgregarColumnas:
    def test_agrega_columnas_al_final_con_el_formato_de_la_vecina(self, plugin, tmp_path):
        r = _editar(
            plugin,
            tmp_path,
            [
                {"agregar_columna": {"hoja": "Cálculo", "encabezado": "SUCURSAL"}},
                {"agregar_columna": {"hoja": "Cálculo", "encabezado": "ÁREA"}},
            ],
        )
        assert r["success"] is True, r
        ws = _abrir(tmp_path)["Cálculo"]
        assert [c.value for c in ws[1]] == ["USUARIO", "NECESIDAD", "PUNTAJE", "SUCURSAL", "ÁREA"]
        assert ws["D1"].font.bold and ws["D1"].fill.start_color.rgb == ws["C1"].fill.start_color.rgb
        assert ws["D2"].border.left.style == "thin" and ws["E4"].fill.start_color.rgb == ws["C4"].fill.start_color.rgb
        assert ws.column_dimensions["D"].width >= 18

    def test_con_valores_por_fila(self, plugin, tmp_path):
        r = _editar(
            plugin,
            tmp_path,
            [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "SUCURSAL", "valores": ["SUC1", "SUC2", "SUC11"]}}],
        )
        assert r["success"] is True
        ws = _abrir(tmp_path)["Cálculo"]
        assert [ws[f"D{i}"].value for i in (2, 3, 4)] == ["SUC1", "SUC2", "SUC11"]

    def test_el_original_y_las_demas_celdas_no_cambian(self, plugin, tmp_path):
        antes = None
        _editar(plugin, tmp_path, [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "X"}}])
        orig = (tmp_path / "outputs" / "matriz.xlsx").read_bytes()
        from openpyxl import load_workbook

        a, b = load_workbook(tmp_path / "outputs" / "matriz.xlsx"), _abrir(tmp_path)
        for fila in range(1, 5):
            for col in "ABC":
                assert a["Cálculo"][f"{col}{fila}"].value == b["Cálculo"][f"{col}{fila}"].value
        assert a["Cálculo"]["C2"].number_format == b["Cálculo"]["C2"].number_format == "0.0"
        assert b.sheetnames == ["Cálculo", "Guía"] and antes is None and orig


class TestFilasCeldasYReemplazos:
    def test_agregar_filas_copia_el_formato_de_la_ultima(self, plugin, tmp_path):
        r = _editar(plugin, tmp_path, [{"agregar_filas": {"hoja": "Cálculo", "filas": [["Dani", "Chat", 2]]}}])
        assert r["success"] is True, r
        ws = _abrir(tmp_path)["Cálculo"]
        assert [c.value for c in ws[5]] == ["Dani", "Chat", 2] and ws["A5"].border.left.style == "thin"

    def test_celda_y_reemplazar(self, plugin, tmp_path):
        r = _editar(
            plugin,
            tmp_path,
            [
                {"celda": {"hoja": "Cálculo", "ref": "B3", "valor": "Informes"}},
                {"reemplazar": {"buscar": "Correo", "por": "Mail"}},
            ],
        )
        assert r["success"] is True, r
        ws = _abrir(tmp_path)["Cálculo"]
        assert ws["B3"].value == "Informes" and ws["B4"].value == "Mail"

    def test_formato_op_del_editor_de_word_tambien_vale(self, plugin, tmp_path):
        r = _editar(plugin, tmp_path, [{"op": "agregar_columna", "hoja": "Cálculo", "encabezado": "OBS"}])
        assert r["success"] is True, r

    def test_eliminar_columna(self, plugin, tmp_path):
        r = _editar(plugin, tmp_path, [{"eliminar_columna": {"hoja": "Cálculo", "encabezado": "NECESIDAD"}}])
        assert r["success"] is True
        assert [c.value for c in _abrir(tmp_path)["Cálculo"][1][:2]] == ["USUARIO", "PUNTAJE"]

    def test_operaciones_como_alias_y_json(self, plugin, tmp_path):
        (tmp_path / "outputs").mkdir()
        _libro(tmp_path / "outputs" / "matriz.xlsx")
        texto = json.dumps([{"agregar_columna": {"hoja": "Cálculo", "encabezado": "Z"}}])
        assert json.loads(plugin._handle_edit_xlsx({"path": "matriz.xlsx", "operaciones": texto}))["success"] is True


class TestRechazos:
    @pytest.mark.parametrize(
        "cambio",
        [
            {"agregar_columna": {"hoja": "Nada", "encabezado": "X"}},
            {"agregar_columna": {"hoja": "Cálculo"}},
            {"agregar_columna": {"hoja": "Cálculo", "encabezado": "X", "despues_de": "NO EXISTE"}},
            {"celda": {"hoja": "Cálculo", "ref": "B3"}},
            {"reemplazar": {"buscar": "inexistente", "por": "x"}},
            {"inventada": {}},
            {"agregar_filas": {"hoja": "Cálculo", "filas": []}},
        ],
    )
    def test_no_escribe_nada(self, plugin, tmp_path, cambio):
        r = _editar(plugin, tmp_path, [cambio])
        assert r["success"] is False and r["original_intacto"] is True
        assert not (tmp_path / "outputs" / "matriz_v2.xlsx").exists()

    def test_un_libro_con_grafico_no_se_edita_porque_se_perderia(self, plugin, tmp_path):
        r = _editar(plugin, tmp_path, [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "X"}}], grafico=True)
        assert r["success"] is False and "charts" in r["error"]

    def test_insertar_en_el_medio_con_celdas_combinadas_se_rechaza(self, plugin, tmp_path):
        r = _editar(
            plugin,
            tmp_path,
            [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "X", "despues_de": "USUARIO"}}],
            combinadas=True,
        )
        assert r["success"] is False and "combinadas" in r["error"]

    def test_insertar_en_el_medio_con_formulas_se_rechaza(self, plugin, tmp_path):
        r = _editar(
            plugin,
            tmp_path,
            [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "X", "despues_de": "USUARIO"}}],
            formulas=True,
        )
        assert r["success"] is False and "fórmulas" in r["error"]

    def test_insertar_en_el_medio_sin_combinadas_ni_formulas_funciona(self, plugin, tmp_path):
        r = _editar(
            plugin, tmp_path, [{"agregar_columna": {"hoja": "Cálculo", "encabezado": "X", "despues_de": "USUARIO"}}]
        )
        assert r["success"] is True, r
        assert [c.value for c in _abrir(tmp_path)["Cálculo"][1]][:3] == ["USUARIO", "X", "NECESIDAD"]

    def test_archivo_inexistente_y_no_xlsx(self, plugin, tmp_path):
        assert json.loads(plugin._handle_edit_xlsx({"path": "no.xlsx", "cambios": [{"celda": {}}]}))["success"] is False
        (tmp_path / "x.csv").write_text("a,b")
        assert json.loads(plugin._handle_edit_xlsx({"path": "x.csv", "cambios": [{}]}))["success"] is False

    def test_sin_cambios_el_error_nombra_el_parametro(self, plugin, tmp_path):
        (tmp_path / "outputs").mkdir()
        _libro(tmp_path / "outputs" / "matriz.xlsx")
        r = json.loads(plugin._handle_edit_xlsx({"path": "matriz.xlsx"}))
        assert r["success"] is False and "«cambios»" in r["error"]
