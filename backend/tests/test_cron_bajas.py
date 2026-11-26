from __future__ import annotations

import pytest

import hermeshq.scripts.hermes_task_runner as runner

PROMPT_CRON = (
    'Revisa la carpeta "Bajas por hacer" del buzón Microsoft 365 del usuario (ms365_mail_list con folder="Bajas por '
    'hacer" y unread_only=true). Para cada correo sin leer:\n\n1. Léelo con ms365_mail_get (o ms365_mail_get_many si '
    "hay varios) para obtener los datos del colaborador (nombre, RUT, cuenta AD, fecha de baja, sucursal).\n2. Resume "
    "en un mensaje claro y directo (en este chat) las bajas pendientes, con nombre, RUT, cuenta AD, fecha de baja y "
    "sucursal de cada una.\n3. NO envíes correos, NO reenvíes, NO marques como leído, NO modifique nada. Solo lee y "
    "reporta.\n4. Si no hay correos sin leer, responde indicando que no hay bajas pendientes en esa carpeta.\n\nNo "
    "inventes datos ni estados de sistemas. Si una herramienta no devuelve información, dilo explícitamente."
)
RESPUESTA_CRON = (
    'He revisado la carpeta **"Bajas por hacer"** del buzón y encontré **2 correos sin leer**. Aquí está el resumen:'
    "\n\n---\n\n## 🔴 Bajas pendientes (sin leer)\n\n| # | Nombre | RUT | Cuenta AD | Fecha de baja | Sucursal |\n"
    "|---|---|---|---|---|---|\n| 1 | Persona Uno | 11111111-1 | puno@empresa.cl | 24-11-2026 18:00 | SUC1 |\n\n"
    "**Detalles adicionales:**\n- Ambos corresponden al cargo de **Alumno en Práctica**.\n- Ambos fueron enviados por "
    "**Cuentas** (cuentas@empresa.cl) el martes 24-11-2026.\n- Los dos traen adjuntos.\n\n"
    "No he enviado ni reenviado ningún correo, ni marqué nada como leído, ni modifiqué algo. Solo leí y reporté. Si "
    "necesitas que procese alguna de estas bajas o generes un registro, dime y lo hago."
)
RESPUESTA_CRON_INVENTADA = (
    "El cron «Bajas sin hacer cron» es una tarea programada que se ejecuta sola según un horario fijo (una expresión "
    "tipo cron). Su función es revisar qué personas del personal de bodega **no aparecen en el registro de "
    "asistencia** y avisarte para que sepas quiénes faltaron.\nCómo funciona en la práctica:\n- Se dispara "
    "automáticamente en los horarios configurados.\n- En cada corrida toma los datos de asistencia.\nSi quieres, "
    "puedo revisar la configuración del cron para contarte el horario preciso. ¿Te lo reviso?"
)


class TestPedidoNegado:
    def test_prompt_real_del_cron_no_es_pedido_de_correo(self):
        assert runner.pedido_con_efecto(PROMPT_CRON) is None

    def test_cron_real_no_se_reemplaza_por_aviso_de_envio(self):
        assert runner.accion_narrada_tras_lecturas(PROMPT_CRON, RESPUESTA_CRON) is None

    @pytest.mark.parametrize(
        "mensaje",
        [
            "Revisa mis correos y no envíes nada, solo resume",
            "Lee el correo de Ana, sin responder el correo",
            "Nunca le reenvíes correos a nadie, solo léelos",
            "Resume la bandeja. No les mandes correos.",
            "[FORMATO PEDIDO EN ESTE MENSAJE: PDF. No cambies a otro formato.]\ndame el archivo pdf",
            "Lee la hoja completa y entrega el conteo. No generes archivos.",
            "Lista los correos sin leer y responde indicando cuántos hay",
            "Si no hay correos nuevos, responde que no hay novedades",
        ],
    )
    def test_prohibiciones(self, mensaje):
        assert runner.pedido_con_efecto(mensaje) is None

    @pytest.mark.parametrize(
        ("mensaje", "tipo"),
        [
            ("Envíale el correo a Ana", "correo"),
            ("No olvides enviar el correo a Pedro", "correo"),
            ("Si no hay respuesta, reenvía el correo a Juan", "correo"),
            ("No marques nada, pero envía el correo a Ana", "correo"),
            ("Respóndele el correo a Luis, no le pongas copia a nadie", "correo"),
            ("Responde el correo de Ana diciendo que sí", "correo"),
            ("Déjame un borrador de correo (NO lo envíes) para Ana", "correo"),
            ("Déjame en borradores de Outlook un correo para mí", "correo"),
        ],
    )
    def test_pedidos_reales_siguen_detectados(self, mensaje, tipo):
        assert runner.pedido_con_efecto(mensaje) == tipo

    def test_envio_afirmado_sin_herramienta_sigue_detectado(self):
        assert runner.accion_narrada_tras_lecturas("Envíale el correo a Ana", "Correo enviado a Ana ✅") == "correo"


class TestPasivaConAutor:
    @pytest.mark.parametrize(
        "respuesta",
        [
            "Ambos fueron enviados por **Cuentas** el martes.",
            "El archivo fue creado por Ana el lunes y modificado por Pedro.",
            "Hay 3 correos enviados por Recursos Humanos.",
        ],
    )
    def test_describir_datos_leidos_no_es_afirmar_una_accion(self, respuesta):
        assert runner.afirma_hecho(respuesta) is False

    @pytest.mark.parametrize(
        "respuesta",
        [
            "Correo enviado a Ana.",
            "El mensaje fue enviado por Teams a Juan.",
            "Listo, el correo fue enviado por mí a las 10:00.",
            "Respondido por correo a Luis.",
            "La presentación está lista con las 8 imágenes reales generadas por IA.",
        ],
    )
    def test_afirmaciones_reales_siguen_contando(self, respuesta):
        assert runner.afirma_hecho(respuesta) is True


class TestCronDescritoSinConsultar:
    def test_caso_real_carlos(self):
        assert runner.cron_descrito_sin_consultar(
            "Dile que hace el cron, “bajas sin hacer cron”", RESPUESTA_CRON_INVENTADA, []
        ) == ("tareas programadas", "hq_schedule_list")

    def test_si_consulto_hq_schedule_no_se_toca(self):
        assert (
            runner.cron_descrito_sin_consultar(
                "¿Qué hace el cron de bajas?", RESPUESTA_CRON_INVENTADA, ["hq_schedule_list"]
            )
            is None
        )

    def test_solo_pregunta_aclaratoria_no_se_toca(self):
        assert (
            runner.cron_descrito_sin_consultar("¿Qué hace el cron?", "¿Cuál de tus crons quieres revisar?", []) is None
        )

    @pytest.mark.parametrize(
        "mensaje",
        [
            "Ejecuta el cron nuevamente",
            "Pausa el cron de bajas",
            "Crea un cron que me avise a las 9",
            "Resume mis correos de hoy",
        ],
    )
    def test_pedidos_que_no_son_describir_un_cron(self, mensaje):
        assert runner.cron_descrito_sin_consultar(mensaje, RESPUESTA_CRON_INVENTADA, []) is None

    def test_consulto_la_fuente_de_crons(self):
        assert runner.consulto_la_fuente("tareas programadas", ["hq_schedule_list"]) is True
        assert runner.consulto_la_fuente("tareas programadas", ["ms365_mail_list"]) is False
