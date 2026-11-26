import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = "Elimina la skill prueba-pii, ya terminé la prueba. Sin preguntarme nada."
FALSA = (
    "La skill `prueba-pii` ya no existe, así que no hay nada que eliminar. Lo verifiqué con la herramienta de gestión "
    "de skills y no está en la lista."
)


def test_el_pedido_se_detecta_como_accion_con_efecto():
    assert runner.accion_narrada(PEDIDO, "Listo. La skill `prueba-pii` ha sido eliminada.") == "edicion"


def test_decir_que_no_existe_sin_llamar_herramientas_se_trata_como_no_hecho():
    assert runner.sin_efecto_declarado(FALSA, 0) is True


def test_el_aviso_honesto_no_deja_la_afirmacion_falsa():
    aviso = runner.aviso_accion_no_hecha("edicion", FALSA, mensaje=PEDIDO)
    assert "No pude completar" in aviso and "no se ejecutó ninguna herramienta" in aviso
    assert "verifiqué" not in aviso


def test_con_consultas_hechas_no_existe_es_un_resultado_real():
    assert runner.sin_efecto_declarado(FALSA, 2) is False


def test_los_casos_anteriores_siguen_igual():
    assert runner.sin_efecto_declarado("Listo. Eliminé la skill.", 0) is True
    assert runner.sin_efecto_declarado("Voy a eliminar la skill con la herramienta.", 0) is True
    assert runner.sin_efecto_declarado("¿Cuál de las dos skills quieres eliminar?", 0) is False
    assert runner.sin_efecto_declarado("No pude eliminarla: la herramienta no está disponible.", 0) is False


NEGATIVA_DE_CAPACIDAD = (
    "No generé ningún archivo: no pude aplicar los cambios. La herramienta `hq_pptx_editar` no está disponible en mi "
    "lista de herramientas, y no existe otra que edite un .pptx existente. Para poder hacerlo necesito una de estas "
    "opciones: 1. Que uses tú la edición."
)


def test_una_negativa_de_capacidad_honesta_no_se_reemplaza():
    assert runner.sin_efecto_declarado(NEGATIVA_DE_CAPACIDAD, 0) is False
    assert runner.sin_efecto_declarado("La herramienta de envío no existe en mi lista; no puedo hacerlo.", 0) is False
    assert runner.sin_efecto_declarado("No encuentro esa opción en mis herramientas disponibles.", 0) is False


def test_afirmar_el_estado_o_la_verificacion_sin_consultar_si_se_avisa():
    assert runner.sin_efecto_declarado("Ya no existe, así que no hay nada que eliminar.", 0) is True
    assert runner.sin_efecto_declarado("Lo comprobé y no hay nada que cambiar.", 0) is True
    assert runner.sin_efecto_declarado("Verifiqué con la herramienta de skills y no está en la lista.", 0) is True
    assert runner.sin_efecto_declarado("Ya no existe, así que no hay nada que eliminar.", 3) is False
