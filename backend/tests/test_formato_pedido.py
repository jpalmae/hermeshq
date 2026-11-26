import unittest

import pytest

from hermeshq.services.formato_pedido import (
    DIRECTIVA_CHAT,
    DIRECTIVA_EXCEL,
    DIRECTIVA_HTML,
    DIRECTIVA_PDF,
    DIRECTIVA_WORD,
    con_formato_pedido,
    directiva_formato,
)

CASO_REAL_TEAMS = (
    "usando la skill de informes interactivos, hazme un informe interactivo con las ventas de enero a junio por "
    "región del Excel que acabas de hacer"
)


class FormatoPedidoTests(unittest.TestCase):
    def test_excel_explicito_gana_aunque_haya_gestion_o_kpis(self):
        texto = "analiza los reprocesos y dame el Excel con gráficos y KPIs para llevar la gestión"
        self.assertEqual(directiva_formato(texto), DIRECTIVA_EXCEL)

    def test_html_o_dashboard_explicito(self):
        self.assertEqual(directiva_formato("hazme un dashboard interactivo"), DIRECTIVA_HTML)
        self.assertEqual(directiva_formato("quiero el informe en html"), DIRECTIVA_HTML)

    def test_sin_formato_explicito_no_agrega_nada(self):
        self.assertIsNone(directiva_formato("revisa los reprocesos del mes"))
        self.assertEqual(con_formato_pedido("revisa los reprocesos", "Pedido"), "Pedido")

    def test_formatos_contradictorios_no_fijan_nada(self):
        self.assertIsNone(directiva_formato("dame un dashboard en excel"))

    def test_la_directiva_va_antes_del_pedido_y_gana_al_historial(self):
        prompt = con_formato_pedido(
            "dame el excel", "Conversación reciente: Tú: aquí tienes el dashboard html\n\nPedido"
        )
        self.assertTrue(prompt.startswith(DIRECTIVA_EXCEL))
        self.assertIn("No generes HTML ni dashboard", prompt)


class TestCasoRealDeTeams:
    def test_informe_interactivo_del_excel_que_acabas_de_hacer_es_html_nunca_excel(self):
        assert directiva_formato(CASO_REAL_TEAMS) == DIRECTIVA_HTML
        assert not con_formato_pedido(CASO_REAL_TEAMS, "Pedido").startswith(DIRECTIVA_EXCEL)
        assert "No generes HTML" not in con_formato_pedido(CASO_REAL_TEAMS, "Pedido")

    def test_variante_sin_tildes_ni_mayusculas(self):
        texto = "usando la skill de informes interactivos hazme un informe interactivo del excel que acabas de hacer"
        assert directiva_formato(texto.upper()) == DIRECTIVA_HTML


SALIDAS = [
    ("créame un Excel con ventas mensuales y un gráfico", DIRECTIVA_EXCEL),
    ("pásalo a Excel", DIRECTIVA_EXCEL),
    ("expórtalo como excel por favor", DIRECTIVA_EXCEL),
    ("lo quiero en Excel", DIRECTIVA_EXCEL),
    ("dame el excel con los totales", DIRECTIVA_EXCEL),
    ("hazme una planilla con los gastos", DIRECTIVA_EXCEL),
    ("ponlo en una hoja de cálculo", DIRECTIVA_EXCEL),
    ("genera un xlsx con el detalle", DIRECTIVA_EXCEL),
    ("dame un archivo de Excel con las cifras", DIRECTIVA_EXCEL),
    ("créame un Excel que tenga un gráfico de barras", DIRECTIVA_EXCEL),
    ("hazme un informe interactivo de ventas", DIRECTIVA_HTML),
    ("quiero un reporte interactivo por región", DIRECTIVA_HTML),
    ("arma un dashboard con los KPIs", DIRECTIVA_HTML),
    ("hazme un tablero de seguimiento", DIRECTIVA_HTML),
    ("necesito unos informes interactivos por niveles", DIRECTIVA_HTML),
    ("entrégalo en html", DIRECTIVA_HTML),
    ("pásalo a PDF", DIRECTIVA_PDF),
    ("conviértelo a pdf", DIRECTIVA_PDF),
    ("expórtalo a PDF", DIRECTIVA_PDF),
    ("créame un Word con el acta", DIRECTIVA_WORD),
    ("lo necesito como docx", DIRECTIVA_WORD),
]

FUENTES = [
    "resume el pdf",
    "lee el pdf que te mandé y dime los plazos",
    "abre el pdf y dime de qué trata",
    "revisa la planilla y dime si cuadra",
    "del excel que acabas de hacer, dime el total de marzo",
    "usando el excel, dime cuál región vendió más",
    "según el word, ¿quién firma?",
    "con el excel de ventas, ¿cuánto vendimos?",
    "este excel tiene errores, ¿los ves?",
    "ese pdf está desactualizado",
    "a partir del archivo excel dime el promedio",
    "el Excel que te mandé tiene los datos de junio, ¿cuál es el total?",
    "el excel de ventas que acabas de hacer, ¿cuántas filas tiene?",
    "en el excel de ayer, ¿cuál es el total?",
    "revisa ventas_2026.xlsx y dime el total",
    "desde el pdf saca las fechas",
    "mi excel no abre",
    "analiza el excel adjunto",
    "basándote en el pdf dime las conclusiones",
    "sobre la planilla, ¿qué columnas tiene?",
    "toma el dashboard que me hiciste y dime qué región lidera",
    "del informe interactivo que hiciste, ¿qué mes fue el mejor?",
]

MIXTOS = [
    ("a partir del excel hazme un informe interactivo", DIRECTIVA_HTML),
    ("con el excel que te mandé arma un dashboard", DIRECTIVA_HTML),
    ("lee el pdf y créame un Word con el resumen", DIRECTIVA_WORD),
    ("del dashboard que me hiciste, pásalo a Excel", DIRECTIVA_EXCEL),
    ("toma el excel y pásalo a PDF", DIRECTIVA_PDF),
    ("convierte el excel a pdf", DIRECTIVA_PDF),
    ("resume el pdf en un Excel", DIRECTIVA_EXCEL),
    ("usando el word que te mandé hazme una planilla", DIRECTIVA_EXCEL),
    ("revisa reporte.pdf y hazme un excel con los montos", DIRECTIVA_EXCEL),
]

CONTRADICTORIOS = [
    "dame un dashboard en excel",
    "dame un excel y un pdf",
    "del excel que hiciste créame un Word y un PDF",
    "hazme un informe interactivo y también un excel",
]


@pytest.mark.parametrize(("texto", "esperada"), SALIDAS)
def test_salida_pedida_se_reconoce(texto, esperada):
    assert directiva_formato(texto) == esperada


@pytest.mark.parametrize("texto", FUENTES)
def test_mencion_de_fuente_no_es_formato_pedido(texto):
    planilla = any(p in texto.lower() for p in ("excel", "xlsx", "planilla"))
    assert directiva_formato(texto) == (DIRECTIVA_CHAT if planilla else None)


PEDIDO_BRUNO = (
    "Analiza el archivo ReprocesosMtto.xlsx, que contiene dos hojas:\nIW38: órdenes de trabajo (OT) de mantenimiento."
    "\neSkuad: solicitudes de atención por fallas de equipos.\nMetodología requerida:\nNormalización previa: "
    "normaliza los identificadores. Documenta las equivalencias aplicadas.\nEntregables:\nTabla de reprocesos "
    "detectados: equipo, OT base, OT posterior.\nResumen estadístico: total de reprocesos.\nRestricciones: no "
    "infieras reprocesos sin evidencia en los datos."
)


def test_caso_real_analisis_de_excel_sin_formato_va_en_el_chat():
    assert directiva_formato(PEDIDO_BRUNO) == DIRECTIVA_CHAT
    assert "no generes archivos" in DIRECTIVA_CHAT


@pytest.mark.parametrize(
    "texto",
    [
        "corrige el excel que te mandé",
        "actualiza la planilla con los datos de octubre",
        "agrega una columna de totales al excel",
        "con el excel hazme un informe",
        "con el excel arma una presentación",
        "analiza el excel y envíame el resultado por correo",
        "analiza el excel y súbelo a onedrive",
        "del excel hazme un gráfico de ventas",
        "analiza el excel y genera un archivo nuevo con los duplicados",
    ],
)
def test_pedidos_que_esperan_un_archivo_no_se_fuerzan_al_chat(texto):
    assert directiva_formato(texto) != DIRECTIVA_CHAT


def test_sin_planilla_no_hay_directiva_de_chat():
    assert directiva_formato("revisa los reprocesos del mes") is None


@pytest.mark.parametrize(("texto", "esperada"), MIXTOS)
def test_fuente_y_salida_distintas_manda_la_salida(texto, esperada):
    assert directiva_formato(texto) == esperada


@pytest.mark.parametrize("texto", CONTRADICTORIOS)
def test_dos_salidas_distintas_no_fuerzan_ninguna(texto):
    assert directiva_formato(texto) is None


if __name__ == "__main__":
    unittest.main()


class TestProgramarNoEsPedirElArchivo:
    def test_programar_una_tarea_que_genera_un_excel_no_fuerza_la_entrega_del_excel(self):
        from hermeshq.services.formato_pedido import directiva_formato

        assert (
            directiva_formato(
                "Programa una tarea para hoy a las 10:20 y otra igual a las 10:26 que genere un Excel llamado CRON-N1"
            )
            is None
        )
        assert directiva_formato("Quiero programar una tarea diaria que arme un informe en PDF") is None
        assert directiva_formato("Agenda una tarea para mañana que genere un Excel") is None

    def test_pedir_el_excel_sin_programar_sigue_forzando_excel(self):
        from hermeshq.services.formato_pedido import DIRECTIVA_EXCEL, directiva_formato

        assert directiva_formato("Hazme un Excel con las ventas") == DIRECTIVA_EXCEL
        assert directiva_formato("Programa una reunión mañana y mándame un Excel con la agenda") == DIRECTIVA_EXCEL
