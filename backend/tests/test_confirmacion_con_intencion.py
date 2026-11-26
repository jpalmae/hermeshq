import pytest

import hermeshq.scripts.hermes_task_runner as runner

PROPUESTA = (
    "Esquema listo.\n\n¿Aplico con estos datos de ejemplo y adjunto el .docx + PDF aquí (sin subir a OneDrive)?\n\n"
    "⚠️ No pude adjuntar el archivo en este mensaje. Escribe «reintenta» para volver a intentarlo."
)
SOLO_ANUNCIA = (
    "El usuario confirma los supuestos. Procedo a crear el procedimiento con contenido de ejemplo genérico y lo "
    "adjunto aquí."
)


class TestConfirmacionConIntencion:
    @pytest.mark.parametrize(
        "mensaje",
        [
            "Sí, procede con esos supuestos.",
            "sí",
            "Dale, hazlo",
            "ok, adelante",
            "Sí, genérala así, con contenido de ejemplo.",
        ],
    )
    def test_confirmacion_y_solo_anuncio_sin_herramienta(self, mensaje):
        assert runner.confirmacion_con_intencion(mensaje, SOLO_ANUNCIA, PROPUESTA) == "anuncio"

    @pytest.mark.parametrize(
        ("mensaje", "respuesta", "anterior"),
        [
            ("Sí, procede con esos supuestos.", "Listo, ya lo creé.", PROPUESTA),
            ("Sí, procede con esos supuestos.", "¿Prefieres que use otros nombres? Procedo a crear.", PROPUESTA),
            ("Sí, procede con esos supuestos.", "No pude crear el documento: falta la plantilla.", PROPUESTA),
            ("Sí, procede con esos supuestos.", SOLO_ANUNCIA, "Un cron es una tarea programada. ¿Quieres un ejemplo?"),
            ("Sí, procede con esos supuestos.", SOLO_ANUNCIA, ""),
            ("Crea un procedimiento nuevo de accesos", SOLO_ANUNCIA, PROPUESTA),
            ("no", SOLO_ANUNCIA, PROPUESTA),
            ("Sí " + "x" * 120, SOLO_ANUNCIA, PROPUESTA),
        ],
    )
    def test_negativos(self, mensaje, respuesta, anterior):
        assert runner.confirmacion_con_intencion(mensaje, respuesta, anterior) is None

    def test_la_nota_de_reintento_pide_la_herramienta_y_no_menciona_la_revision(self):
        nota = runner.nota_accion_narrada("anuncio")
        assert "Llama ahora la herramienta" in nota and "sin mencionar esta revisión" in nota

    def test_el_aviso_final_es_honesto(self):
        aviso = runner.aviso_accion_no_hecha("anuncio")
        assert "lo que confirmaste" in aviso and "no cambió nada" in aviso
