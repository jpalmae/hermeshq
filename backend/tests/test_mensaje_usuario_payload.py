import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_mensaje_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

_SP = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)


_ARCHIVOS_PREVIOS = (
    "[Archivos de turnos anteriores: uploads/0f3c9a2b_informe-trimestral-final.pdf, " + "y" * 120 + "]\n"
)


def _prompt_con_referencia_larga(texto: str) -> str:
    referencia = "x" * 400
    return _ARCHIVOS_PREVIOS + (
        f"[Respuesta corta a tu último mensaje: «{referencia}». Interpreta «{texto}» como respuesta a eso, no a temas "
        f"anteriores.]\n{texto}"
    )


class TestMensajeDelUsuarioEnLaCarga:
    def test_usa_el_original_del_chat_publico_aunque_la_referencia_lo_empuje_fuera_del_recorte(self):
        prompt = "[Hora actual: 10:00]\n" + _prompt_con_referencia_larga("sí, compártelo")
        assert len(prompt) > 600 and "compártelo" not in prompt[:600].split("]")[-1]
        payload = {"prompt": prompt, "metadata": {"mensaje_usuario": "sí, compártelo"}}
        assert runner._mensaje_del_usuario(payload) == "sí, compártelo"

    def test_sin_original_usa_el_prompt(self):
        assert runner._mensaje_del_usuario({"prompt": "envía el informe", "metadata": {}}) == "envía el informe"
        assert runner._mensaje_del_usuario({"prompt": "y" * 900}) == "y" * 600

    def test_el_candado_reconoce_el_si_con_la_carga_que_arma_el_runner(self):
        spec = importlib.util.spec_from_file_location("sp_mensaje_test", _SP)
        sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sp)
        prompt = "[Hora actual: 10:00]\n" + _prompt_con_referencia_larga("sí, compártelo")
        for mensaje, esperado in (
            (runner._mensaje_del_usuario({"prompt": prompt, "metadata": {"mensaje_usuario": "sí, compártelo"}}), True),
            (prompt[:600], False),
        ):
            carga = json.dumps({"task_id": "t", "metadata": {}, "mensaje": mensaje})
            with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}):
                assert sp._usuario_confirma() is esperado, mensaje[-40:]
