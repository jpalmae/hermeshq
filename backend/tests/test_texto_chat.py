import unittest

from hermeshq.services.texto_chat import (
    PREFIJO_CLAVE_TEAMS,
    quitar_lineas_media,
    respuesta_para_canal,
    tablas_para_teams,
)


class TablasParaTeams(unittest.TestCase):
    def test_la_tabla_queda_en_markdown_separada_por_lineas_en_blanco(self):
        texto = (
            "🟠 CALIENTE\n"
            "| Carpeta | Remitente | Asunto |\n"
            "|---|---|---|\n"
            "| Bandeja | BrightTALK | Watch |\n"
            "| Orden/TI | Asana | Martes |\n"
            "Fin."
        )
        self.assertEqual(
            tablas_para_teams(texto),
            "🟠 CALIENTE\n\n"
            "| Carpeta | Remitente | Asunto |\n"
            "|---|---|---|\n"
            "| Bandeja | BrightTALK | Watch |\n"
            "| Orden/TI | Asana | Martes |\n\n"
            "Fin.",
        )

    def test_no_usa_bloque_de_codigo(self):
        texto = "Resumen.\n\n| A | B |\n|---|---|\n| uno | dos |"
        self.assertNotIn("```", tablas_para_teams(texto))

    def test_tabla_ya_separada_no_duplica_lineas_en_blanco(self):
        texto = "Resumen.\n\n| A | B |\n|---|---|\n| uno | dos |\n\nFin."
        self.assertEqual(tablas_para_teams(texto), texto)

    def test_celdas_largas_se_conservan_completas(self):
        texto = "| Asunto | Hora |\n|---|---|\n| Miércoles - tareas que se deben entregar pronto | 08:29 |"
        self.assertIn("| Miércoles - tareas que se deben entregar pronto | 08:29 |", tablas_para_teams(texto))

    def test_texto_sin_tabla_solo_separa_lineas(self):
        texto = "Sin tabla | solo una barra\nNada más"
        self.assertEqual(tablas_para_teams(texto), "Sin tabla | solo una barra\n\nNada más")


class LineasSeparadasParaTeams(unittest.TestCase):
    def test_lineas_de_texto_quedan_separadas(self):
        texto = "📥 Informe horario\nVentana: última hora.\n\nResumen."
        self.assertEqual(tablas_para_teams(texto), "📥 Informe horario\n\nVentana: última hora.\n\nResumen.")

    def test_items_de_lista_seguidos_no_se_separan(self):
        texto = "Pendientes:\n- uno\n- dos\n1. tres\n2. cuatro"
        self.assertEqual(tablas_para_teams(texto), "Pendientes:\n\n- uno\n- dos\n1. tres\n2. cuatro")

    def test_bloque_de_codigo_no_se_toca(self):
        texto = "```\nlinea a\nlinea b\n```"
        self.assertEqual(tablas_para_teams(texto), texto)

    def test_titulo_y_tabla_y_texto(self):
        texto = "🔴 CRÍTICO\n| # | Asunto |\n|---|---|\n| #408 | OC |\nResumen."
        self.assertEqual(
            tablas_para_teams(texto),
            "🔴 CRÍTICO\n\n| # | Asunto |\n|---|---|\n| #408 | OC |\n\nResumen.",
        )

    def test_vacio_y_nulo(self):
        self.assertEqual(tablas_para_teams(None), "")
        self.assertEqual(tablas_para_teams(""), "")

    def test_fila_con_menos_celdas_no_rompe(self):
        texto = "| A | B | C |\n|---|---|---|\n| x |"
        self.assertEqual(tablas_para_teams(texto), "| A | B | C |\n|---|---|---|\n| x |  |  |")


class LineasMediaFueraDeTeams(unittest.TestCase):
    def test_las_lineas_media_se_quitan(self):
        texto = "Informe listo.\n\nMEDIA: uploads/a.html\nMEDIA: uploads/b.html"
        self.assertEqual(quitar_lineas_media(texto), "Informe listo.")

    def test_el_texto_sin_media_no_cambia(self):
        self.assertEqual(quitar_lineas_media("Hola\nmundo"), "Hola\nmundo")

    def test_nulo(self):
        self.assertEqual(quitar_lineas_media(None), "")


class RespuestaPorCanal(unittest.TestCase):
    TABLA = "Resumen\n| A | B |\n|---|---|\n| uno | dos |"

    def test_canal_teams_separa_la_tabla_del_texto(self):
        self.assertEqual(
            respuesta_para_canal(self.TABLA, es_teams=True), "Resumen\n\n| A | B |\n|---|---|\n| uno | dos |"
        )

    def test_widget_web_no_cambia_nada(self):
        self.assertEqual(respuesta_para_canal(self.TABLA, es_teams=False), self.TABLA)

    def test_nulo_no_rompe(self):
        self.assertEqual(respuesta_para_canal(None, es_teams=True), "")

    def test_prefijo_de_claves_del_bridge(self):
        self.assertTrue("bridge-teams-AGN-Usuario".startswith(PREFIJO_CLAVE_TEAMS))
        self.assertFalse("widget-web-cliente".startswith(PREFIJO_CLAVE_TEAMS))
