import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = "Crea una skill privada solo para mí llamada prueba-w5 con una sola regla: al saludar, responde en una línea."
ANUNCIO = "Voy a crear la skill privada **prueba-w5** con la herramienta correspondiente."


def test_el_pedido_se_detecta_como_creacion():
    assert runner.accion_narrada(PEDIDO, "Creada la skill privada prueba-w5.") == "creacion"


def test_un_anuncio_tras_el_reintento_se_considera_sin_hacer():
    assert runner.solo_intencion(ANUNCIO) is True
    assert runner.solo_intencion(ANUNCIO + "\n\n" + ANUNCIO) is True


def test_el_aviso_habla_de_la_skill_y_no_de_un_archivo():
    aviso = runner.aviso_accion_no_hecha("creacion", "", mensaje=PEDIDO)
    assert "skill" in aviso and "no se ejecutó ninguna herramienta" in aviso and "archivo" not in aviso


def test_un_pedido_de_archivo_sigue_hablando_del_archivo():
    aviso = runner.aviso_accion_no_hecha("creacion", "", mensaje="Hazme un Word con una tabla")
    assert "el archivo" in aviso


def test_una_respuesta_con_resultado_o_pregunta_no_es_solo_intencion():
    assert runner.solo_intencion("Listo. La skill prueba-w5 quedó creada.") is False
    assert runner.solo_intencion("¿Qué regla quieres que tenga la skill?") is False
