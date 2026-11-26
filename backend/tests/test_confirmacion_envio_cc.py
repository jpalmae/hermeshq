import hermeshq.scripts.hermes_task_runner as runner


def test_caso_real_para_y_cc_por_separado():
    texto = runner._texto_envio(
        {
            "preview": {
                "para": ["equipo-datos@contoso.cl"],
                "cc": ["persona.externa@example.com"],
                "cco": [],
                "asunto": "Prueba ID 02-03-04",
            }
        }
    )
    assert texto.splitlines() == [
        "✅ Correo enviado (confirmado por el servidor).",
        "**Para:** equipo-datos@contoso.cl",
        "**CC:** persona.externa@example.com",
        "**Asunto:** Prueba ID 02-03-04",
    ]


def test_respuesta_con_copia_oculta():
    texto = runner._texto_envio({"preview": {"responde_a": "ana@x.cl", "cco": "jefe@x.cl", "asunto": "Re: Informe"}})
    assert "**Para:** ana@x.cl" in texto and "**CCO:** jefe@x.cl" in texto and "**CC:**" not in texto
