import pytest

from hermeshq.services import kql_correo as k


class TestKqlValido:
    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("from:ana@ejemplo.invalid", 'from:"ana@ejemplo.invalid"'),
            ('subject:"informe mensual"', 'subject:"informe mensual"'),
            ("received>=2026-01-05", "received>=2026-01-05"),
            ("hasattachments:true", "hasattachments:true"),
            (
                'from:"Juan Pérez" subject:"plan" received>=2026-02-01 hasattachments:true',
                'from:"Juan Pérez" subject:"plan" received>=2026-02-01 hasattachments:true',
            ),
            ("presupuesto", "presupuesto"),
            ("from:ana presupuesto", 'from:"ana" presupuesto'),
        ],
    )
    def test_forma_canonica(self, entrada, esperado):
        assert k.validar(entrada) == esperado


class TestKqlRechazado:
    @pytest.mark.parametrize(
        "entrada,motivo",
        [
            ("", "consulta_vacia"),
            ("   ", "consulta_vacia"),
            ("from:", "valor_no_permitido"),
            ('from:""', "valor_no_permitido"),
            ("subject:a(b)", "termino_no_permitido"),
            ("from:x OR subject:y", "operador_no_permitido"),
            ("presupuesto AND plan", "operador_no_permitido"),
            ("NOT plan", "operador_no_permitido"),
            ("body:secreto", "termino_no_permitido"),
            ("received>=2026-02-30", "fecha_no_valida"),
            ("received>=ayer", "fecha_no_valida"),
            ("hasattachments:false", "termino_no_permitido"),
            ("from:*", "valor_no_permitido"),
            ('subject:"a\' OR 1=1"', "valor_no_permitido"),
            ("x" * 401, "consulta_demasiado_larga"),
            ("a1 a2 a3 a4 a5 a6 a7 a8 a9 b1 b2 b3 b4", "demasiados_terminos"),
            ("from:a;DROP", "termino_no_permitido"),
            ("{json}", "termino_no_permitido"),
        ],
    )
    def test_rechazo_explicito(self, entrada, motivo):
        with pytest.raises(k.KqlNoPermitido) as e:
            k.validar(entrada)
        assert e.value.motivo == motivo
