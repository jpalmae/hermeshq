import unittest

import hermeshq.scripts.hermes_task_runner as runner


def _asst(args, name="sharepoint_search"):
    return {"role": "assistant", "tool_calls": [{"type": "function", "function": {"name": name, "arguments": args}}]}


def _tool():
    return {"role": "tool", "content": '{"success": true, "count": 0}'}


def _turnos(argumentos):
    mensajes = [{"role": "user", "content": "Dime qué dice la matriz de priorización"}]
    for a in argumentos:
        mensajes += [_asst(a), _tool()]
    return mensajes


REAL = [
    '{"queries":[{"drive":"me","query":"a"},{"drive":"me","query":"e"},{"drive":"me","query":"i"}]}',
    '{"queries":[{"drive":"me","query":"o"},{"drive":"me","query":"u"},{"drive":"me","query":"a"}]}',
    '{"queries":[{"drive":"me","query":"e"},{"drive":"me","query":"i"},{"drive":"me","query":"o"}]}',
]


class TestCicloDeLlamadas(unittest.TestCase):
    def test_el_caso_real_se_corta_a_las_12_llamadas_y_no_a_las_69(self):
        self.assertEqual(runner._repeticiones_identicas(_turnos(REAL * 4)), 1, "el cortacircuito anterior no lo veía")
        self.assertTrue(runner._ciclo_de_llamadas(_turnos(REAL * 4)))

    def test_con_menos_de_12_llamadas_no_se_corta(self):
        self.assertFalse(runner._ciclo_de_llamadas(_turnos(REAL * 3 + REAL[:2])))

    def test_un_trabajo_con_llamadas_distintas_no_es_un_ciclo(self):
        distintas = [f'{{"query": "termino {n}"}}' for n in range(12)]
        self.assertFalse(runner._ciclo_de_llamadas(_turnos(distintas)))

    def test_cuatro_variantes_no_alcanzan_el_umbral(self):
        cuatro = [f'{{"query": "t{n % 4}"}}' for n in range(12)]
        self.assertFalse(runner._ciclo_de_llamadas(_turnos(cuatro)))

    def test_un_mensaje_del_usuario_en_medio_reinicia_la_cuenta(self):
        mensajes = _turnos(REAL * 4) + [{"role": "user", "content": "otra cosa"}] + _turnos(REAL)[1:]
        self.assertFalse(runner._ciclo_de_llamadas(mensajes))

    def test_un_turno_sin_herramientas_corta_la_ventana(self):
        mensajes = _turnos(REAL * 3) + [{"role": "assistant", "content": "texto"}] + _turnos(REAL)[1:]
        self.assertFalse(runner._ciclo_de_llamadas(mensajes))


if __name__ == "__main__":
    unittest.main()
