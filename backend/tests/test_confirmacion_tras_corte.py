import hermeshq.scripts.hermes_task_runner as runner

VISTA = (
    "Vista previa del reenvío:\n\n- **Asunto:** Batería L16 C4\n- **Para:** usuario1@contoso.cl (tu propio "
    "buzón)\n- **Comentario:** Prueba de reenvío HermesHQ.\n\n¿Lo envío?"
)
CORTADO = "(Turno anterior sin completar: Hermes runtime exceeded the task timeout of 600s. No se entregó una respuesta final.)"
RESPUESTA_REAL = (
    "Reenvié el correo a tu propio buzón.\n\n- **Asunto:** Batería L16 C4\n- **De:** Laura Ruiz (Externo)\n- "
    "**Para:** tu propio buzón\n- **Comentario:** Prueba de reenvío HermesHQ.\n\nLo recibiste en la carpeta de enviados "
    "de tu Microsoft 365."
)


class TestUltimaRespuesta:
    def test_caso_real_el_turno_cortado_no_tapa_la_vista_previa(self):
        historial = [
            {"role": "user", "content": "Reenvía a mí mismo el último correo"},
            {"role": "assistant", "content": VISTA},
            {"role": "user", "content": "sí"},
            {"role": "assistant", "content": CORTADO},
        ]
        assert runner._ultima_respuesta(historial) == VISTA

    def test_sin_corte_es_la_ultima(self):
        assert (
            runner._ultima_respuesta([{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}])
            == "b"
        )


class TestCorreccion:
    def test_caso_real_no_quedan_detalles_de_un_envio_que_no_ocurrio(self):
        final = runner.respuesta_verificada(
            RESPUESTA_REAL, [{"tipo": "correo", "frase": "Reenvié el correo a tu propio buzón."}]
        )
        assert "No se envió ningún correo" in final
        assert "carpeta de enviados" not in final
        assert "Batería L16 C4" not in final

    def test_con_contenido_largo_de_otra_cosa_se_conserva(self):
        resumen = "Resumen de tu bandeja:\n" + "\n".join(
            f"- Correo {i}: asunto de prueba número {i} con detalle" for i in range(20)
        )
        texto = resumen + "\n\nTe envié el resumen por correo."
        final = runner.respuesta_verificada(texto, [{"tipo": "correo", "frase": "Te envié el resumen por correo."}])
        assert "Correo 19" in final and "No se envió ningún correo" in final
