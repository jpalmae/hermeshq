from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermeshq.services import archivos_conversacion as ac

PEDIDO_8B9BD4DD = "Agrega una lámina de cierre a la presentación gobernanza-datos_3.pptx con los próximos pasos."


def _workspace(tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "gobernanza-datos_3.pptx").write_bytes(b"x")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "Informe Q3.docx").write_bytes(b"x")
    (tmp_path / "otra").mkdir()
    (tmp_path / "otra" / "secreto.pptx").write_bytes(b"x")
    return tmp_path


class TestNombresCitados:
    def test_nombre_suelto_en_el_pedido(self):
        assert ac.nombres_citados(PEDIDO_8B9BD4DD) == ["gobernanza-datos_3.pptx"]

    def test_nombre_entre_comillas_con_espacios(self):
        assert ac.nombres_citados("Resume «Informe Q3.docx» por favor") == ["informe q3.docx"]

    def test_solo_cuenta_el_pedido_del_usuario_no_el_historial(self):
        prompt = "Conversación reciente:\nvieja.pptx\n\nPedido del usuario:\nedita nueva.pptx"
        assert ac.nombres_citados(prompt) == ["nueva.pptx"]

    def test_sin_archivos_nombrados(self):
        assert ac.nombres_citados("hazme un resumen del trimestre") == []

    def test_una_ruta_se_reduce_al_nombre(self):
        assert ac.nombres_citados("abre outputs/gobernanza-datos_3.pptx") == []
        assert ac.nombres_citados("abre `outputs/gobernanza-datos_3.pptx`") == ["gobernanza-datos_3.pptx"]


class TestArchivosNombrados:
    def test_encuentra_el_archivo_de_outputs_sin_distinguir_mayusculas(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, "edita GOBERNANZA-datos_3.PPTX") == ["outputs/gobernanza-datos_3.pptx"]

    def test_encuentra_el_de_uploads_con_espacios(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, "resume «Informe Q3.docx»") == ["uploads/Informe Q3.docx"]

    def test_no_mira_carpetas_fuera_de_las_de_archivos(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, "edita secreto.pptx") == []

    def test_nombre_sin_extension_entre_comillas_o_con_separadores(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / "outputs" / "gobernanza-datos_5.pptx").write_bytes(b"x")
        pedido = "En la presentación «gobernanza-datos_5» (Proyecto de Gobernanza de Datos), agrega un cuarto riesgo."
        assert ac.archivos_nombrados(ws, pedido) == ["outputs/gobernanza-datos_5.pptx"]
        assert ac.archivos_nombrados(ws, "abre gobernanza-datos_5 y cambia el título") == [
            "outputs/gobernanza-datos_5.pptx"
        ]

    def test_nombre_sin_extension_no_coincide_parcialmente(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / "outputs" / "gobernanza-datos_5.pptx").write_bytes(b"x")
        assert ac.archivos_nombrados(ws, "habla de la gobernanza de datos en general") == []
        assert ac.archivos_nombrados(ws, "usa «gobernanza-datos» como base") == []

    def test_inexistente_o_sin_workspace(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, "edita fantasma.pptx") == []
        assert ac.archivos_nombrados(None, PEDIDO_8B9BD4DD) == []

    def test_linea_vacia_y_con_rutas(self):
        assert ac.linea_nombrados([]) is None
        linea = ac.linea_nombrados(["outputs/a.pptx"])
        assert linea.startswith(ac.ENCABEZADO_NOMBRADOS) and "outputs/a.pptx" in linea and "SharePoint" in linea


@pytest.mark.asyncio
class TestConArchivosGenerados:
    async def test_el_pedido_8b9bd4dd_recibe_la_nota_con_la_ruta(self, tmp_path):
        ws = _workspace(tmp_path)
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(workspace_path=str(ws))))
        salida = await ac.con_archivos_generados(db, "ag", "u", "s", None, PEDIDO_8B9BD4DD)
        assert salida.startswith(ac.ENCABEZADO_NOMBRADOS)
        assert "outputs/gobernanza-datos_3.pptx" in salida
        assert salida.endswith(PEDIDO_8B9BD4DD)

    async def test_control_negativo_sin_archivo_nombrado_el_pedido_no_cambia(self, tmp_path):
        ws = _workspace(tmp_path)
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(workspace_path=str(ws))))
        pedido = "Hazme un resumen de las ventas de octubre"
        assert await ac.con_archivos_generados(db, "ag", "u", "s", None, pedido) == pedido

    async def test_no_repite_la_nota_si_ya_viene(self, tmp_path):
        ws = _workspace(tmp_path)
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(workspace_path=str(ws))))
        una = await ac.con_archivos_generados(db, "ag", "u", "s", None, PEDIDO_8B9BD4DD)
        assert await ac.con_archivos_generados(db, "ag", "u", "s", None, una) == una

    async def test_un_fallo_al_resolver_no_rompe_el_pedido(self):
        db = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("bd")))
        assert await ac.con_archivos_generados(db, "ag", "u", "s", None, PEDIDO_8B9BD4DD) == PEDIDO_8B9BD4DD


class TestArchivoQueElUsuarioUbicaEnLaNube:
    def test_si_dice_que_esta_en_su_onedrive_no_se_antepone_la_nota_del_workspace(self, tmp_path):
        ws = _workspace(tmp_path)
        pedido = (
            "Comparte con ana@x.cl el archivo gobernanza-datos_3.pptx que está en mi OneDrive en la carpeta «Pruebas»."
        )
        assert ac.archivos_nombrados(ws, pedido) == []

    def test_de_sharepoint_tampoco(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, "Descarga gobernanza-datos_3.pptx de SharePoint y resúmelo") == []

    def test_control_negativo_subir_al_onedrive_si_antepone_la_nota(self, tmp_path):
        ws = _workspace(tmp_path)
        pedido = "Sube gobernanza-datos_3.pptx a mi OneDrive en la carpeta «Pruebas»."
        assert ac.archivos_nombrados(ws, pedido) == ["outputs/gobernanza-datos_3.pptx"]

    def test_control_negativo_sin_ubicacion_en_la_nube(self, tmp_path):
        ws = _workspace(tmp_path)
        assert ac.archivos_nombrados(ws, PEDIDO_8B9BD4DD) == ["outputs/gobernanza-datos_3.pptx"]


@pytest.mark.asyncio
class TestReinicioNoOcultaElArchivoNombrado:
    PEDIDO = (
        "Edita gobernanza-datos_3.pptx de tu espacio de trabajo: agrega al final una sección con el título «Anexo A» y "
        "debajo un párrafo que diga «Contenido de ejemplo del anexo.». Guarda una versión nueva (no toques el "
        "original) y adjúntala aquí, sin subirla a OneDrive."
        "\n[Búsqueda en SharePoint por «Edita gobernanza-datos_3.pptx»: no hay archivos con ese nombre.]"
    )

    def _db(self, ws):
        return SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(workspace_path=str(ws))))

    async def _con(self, db, prompt):
        from unittest.mock import patch

        with (
            patch.object(ac, "archivos_generados", AsyncMock(return_value=[])),
            patch.object(ac, "sesion_sin_historial", AsyncMock(return_value=False)),
            patch.object(ac, "sesion_recien_reiniciada", AsyncMock(return_value=True)),
        ):
            return await ac.con_archivos_generados(db, "ag", "u", "s", None, prompt, aviso_conversacion_nueva=True)

    async def test_tras_un_reset_un_archivo_con_nombre_recibe_la_nota_y_no_el_aviso(self, tmp_path):
        ws = _workspace(tmp_path)
        salida = await self._con(self._db(ws), self.PEDIDO)
        assert salida.startswith(ac.ENCABEZADO_NOMBRADOS)
        assert ac.AVISO_CONVERSACION_REINICIADA not in salida

    async def test_control_negativo_sin_archivo_con_nombre_el_aviso_sigue(self, tmp_path):
        ws = _workspace(tmp_path)
        salida = await self._con(self._db(ws), "Agrega un punto a ese documento, no toques el original.")
        assert salida.startswith(ac.AVISO_CONVERSACION_REINICIADA)


class TestNombreSinExtensionTolerante:
    def _ws(self, tmp_path, nombre):
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / nombre).write_bytes(b"x")
        return tmp_path

    def test_espacio_en_el_pedido_y_guion_en_el_archivo(self, tmp_path):
        ws = self._ws(tmp_path, "Informe-W1.docx")
        assert ac.archivos_nombrados(ws, "Con mi Word «Informe W1» hazme una presentación") == [
            "output/Informe-W1.docx"
        ]

    def test_guion_bajo_y_mayusculas_y_acentos(self, tmp_path):
        ws = self._ws(tmp_path, "Gestión_de_Riesgos.pptx")
        assert ac.archivos_nombrados(ws, "Abre «gestion de riesgos» y cámbialo") == ["output/Gestión_de_Riesgos.pptx"]

    def test_control_negativo_otro_nombre_no_coincide(self, tmp_path):
        ws = self._ws(tmp_path, "Informe-W2.docx")
        assert ac.archivos_nombrados(ws, "Con mi Word «Informe W1» hazme una presentación") == []
