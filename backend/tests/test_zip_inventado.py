from hermeshq.scripts import hermes_task_runner as runner

ZIP_DESCRITO = (
    "Confirmo el resultado real de la herramienta:\n\n- **2 skills** que interactúan con la generación de informes:\n"
    "  1. `informe-por-rol` — genera el informe con navegación por rol.\n"
    "  2. `informes-base` — crea el esqueleto del informe HTML.\n"
    "- **ZIP** con las dos SKILL.md, el resumen y la tabla comparativa.\n\n"
    "Para compartirlo externamente te sugiero enviar el ZIP y el PDF."
)
ZIP_INVENTADO = (
    "El ZIP con la skill completa quedó listo. Incluye el archivo `SKILL.md` y el script del generador.\n\n"
    "MEDIA: uploads/76938b8e-9d7a-4a9d-90b3-45c15e09507f.zip"
)
RECHAZO_ZIP = (
    "No pude generar el ZIP porque no tengo una herramienta para comprimir carpetas en un archivo .zip. "
    "Sí pude verificar qué archivos existen en tu workspace."
)


def _verificar(respuesta, mensaje, mensajes=None, cwd="/no/existe"):
    return runner.verificar_afirmaciones(respuesta, mensajes or [], 0, cwd=cwd, mensaje=mensaje)


class TestEntregaSinArchivo:
    def test_caso_real_envíame_el_zip_descrito_sin_adjuntar_ni_ejecutar_nada(self):
        pendientes = _verificar(ZIP_DESCRITO, "envíame el ZIP")
        assert [p["tipo"] for p in pendientes] == ["entrega_sin_archivo"]
        final = runner.respuesta_verificada(ZIP_DESCRITO, pendientes)
        assert final.startswith("⚠️") and "No te adjunté ningún archivo" in final
        assert "SKILL.md" not in final, "el relato de un archivo que no existe no debe quedar en el historial"

    def test_el_reclamo_por_el_descargable_tambien_cuenta(self):
        assert _verificar(ZIP_DESCRITO, "no tengo ningún descargable")

    def test_la_nota_de_reintento_dice_como_crear_el_zip(self):
        nota = runner.nota_verificacion(_verificar(ZIP_DESCRITO, "envíame el ZIP"))
        assert "execute_code" in nota and "zipfile" in nota and "MEDIA:" in nota

    def test_control_negativo_con_el_adjunto_en_la_respuesta(self, tmp_path):
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "skills.zip").write_bytes(b"PK")
        respuesta = "Aquí está el ZIP.\n\nMEDIA: outputs/skills.zip"
        assert _verificar(respuesta, "envíame el ZIP", cwd=str(tmp_path)) == []

    def test_control_negativo_si_reconoce_que_no_pudo_o_pregunta(self):
        assert _verificar("No pude encontrar ese archivo en tu espacio de trabajo.", "envíame el ZIP") == []
        assert _verificar("¿Cuál de los dos archivos quieres que te envíe?", "envíame el archivo") == []

    def test_control_negativo_si_lo_pide_por_otro_canal(self):
        respuesta = "Listo, el informe queda en tu carpeta de OneDrive."
        assert not any(
            p["tipo"] == "entrega_sin_archivo" for p in _verificar(respuesta, "envíame el informe por correo")
        )
        assert not any(
            p["tipo"] == "entrega_sin_archivo"
            for p in _verificar(respuesta, "compárteme un enlace del Excel de ventas")
        )

    def test_control_negativo_el_aviso_de_segundo_plano(self):
        aviso = "Este informe es largo: lo preparo en segundo plano y te aviso por aquí apenas esté listo."
        assert _verificar(aviso, "envíame el informe en pdf") == []

    def test_control_negativo_si_ejecuto_herramientas(self):
        assert not runner.entrega_de_archivo_sin_adjunto("envíame el ZIP", ZIP_DESCRITO, [("execute_code", "ok", "")])


class TestAdjuntoInventadoSinHerramientas:
    def test_caso_real_sin_herramientas_no_queda_el_relato(self):
        pendientes = _verificar(ZIP_INVENTADO, "Puedes enviarme en un archivo .zip la skill de informes?")
        assert [(p["tipo"], p["sin_herramientas"]) for p in pendientes] == [("adjunto", True)]
        final = runner.respuesta_verificada(ZIP_INVENTADO, pendientes)
        assert "No pude adjuntar «76938b8e-9d7a-4a9d-90b3-45c15e09507f.zip»" in final
        assert "SKILL.md" not in final

    def test_control_negativo_con_herramientas_se_conserva_el_resto(self):
        pendientes = [{"tipo": "adjunto", "frase": "outputs/x.zip", "sin_herramientas": False}]
        texto = "Revisé las dos skills y estos son sus pasos principales, con el detalle de cada uno.\n\n" * 3
        final = runner.respuesta_verificada(texto + "MEDIA: outputs/x.zip", pendientes)
        assert "Revisé las dos skills" in final


class TestRechazoDeComprimir:
    def test_caso_real_no_tengo_herramienta_para_comprimir_con_execute_code_disponible(self):
        disponibles = runner.herramientas_negadas_disponibles(
            "Puedes enviarme en un archivo .zip la skill de informes?", RECHAZO_ZIP, {"execute_code", "read_file"}
        )
        assert disponibles == ["execute_code"]

    def test_control_negativo_sin_execute_code_no_se_sugiere(self):
        assert runner.herramientas_negadas_disponibles("envíame un zip", RECHAZO_ZIP, {"read_file"}) == []


class TestAdjuntoFueraDelEspacio:
    def _rutas(self, tmp_path):
        espacio = tmp_path / "agente"
        (espacio / "outputs").mkdir(parents=True)
        afuera = tmp_path / "compartida"
        afuera.mkdir()
        (afuera / "skills.zip").write_bytes(b"PK")
        return espacio, afuera / "skills.zip"

    def test_prueba_en_vivo_zip_creado_fuera_del_workspace(self, tmp_path):
        espacio, zip_afuera = self._rutas(tmp_path)
        respuesta = f"Listo, aquí está el ZIP.\n\nMEDIA: {zip_afuera}"
        pendientes = _verificar(respuesta, "envíame el ZIP", cwd=str(espacio))
        assert [p["tipo"] for p in pendientes] == ["adjunto_fuera"]
        nota = runner.nota_verificacion(pendientes)
        assert "outputs/" in nota and "shutil.copy" in nota
        final = runner.respuesta_verificada(respuesta, pendientes)
        assert "quedó fuera de mi espacio de trabajo" in final and "MEDIA:" not in final

    def test_control_negativo_dentro_del_workspace(self, tmp_path):
        espacio, _ = self._rutas(tmp_path)
        (espacio / "outputs" / "skills.zip").write_bytes(b"PK")
        respuesta = f"Aquí está.\n\nMEDIA: {espacio / 'outputs' / 'skills.zip'}"
        assert _verificar(respuesta, "envíame el ZIP", cwd=str(espacio)) == []

    def test_control_negativo_rutas_internas_de_skills(self, tmp_path):
        espacio, _ = self._rutas(tmp_path)
        interna = tmp_path / "otro" / ".hermes" / "skills" / "x" / "SKILL.md"
        interna.parent.mkdir(parents=True)
        interna.write_text("x", encoding="utf-8")
        assert not runner._fuera_del_espacio(str(interna), str(espacio))
