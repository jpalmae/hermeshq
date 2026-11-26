import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from hermeshq.services import desglose_tiempos as dt

T0 = 1_000_000.0


def _paso(t, n):
    return {"evento": "paso", "n": n, "t": T0 + t}


def _herr(t, nombre, ms):
    return {"evento": "herramienta", "name": nombre, "ms": ms, "t": T0 + t}


class CausaProbable(unittest.TestCase):
    def test_runner_bloqueado_si_nunca_llego_al_modelo(self):
        d = dt.desglose([], T0, T0 + 300, 0.1)
        self.assertEqual(d["causa_probable"], "runner_bloqueado")
        self.assertFalse(d["terminado_por_el_runner"])

    def test_inferencia_lenta(self):
        eventos = [_paso(1, 1), {"evento": "primer_texto", "paso": 1, "t": T0 + 250}, {"evento": "fin", "t": T0 + 290}]
        d = dt.desglose(eventos, T0, T0 + 300, 0.0)
        self.assertEqual(d["causa_probable"], "inferencia_lenta")
        self.assertAlmostEqual(d["inferencia_s"], 289, places=1)
        self.assertAlmostEqual(d["primer_texto_s"], 249, places=1)

    def test_herramientas_lentas_con_su_detalle(self):
        eventos = [_paso(1, 1), _herr(200, "vision_analyze", 180_000), _paso(201, 2), {"evento": "fin", "t": T0 + 220}]
        d = dt.desglose(eventos, T0, T0 + 230, 0.0)
        self.assertEqual(d["causa_probable"], "herramientas_lentas")
        self.assertEqual(d["herramientas_mas_lentas"][0]["herramienta"], "vision_analyze")

    def test_bucle_por_llamadas_repetidas(self):
        eventos = [_paso(1, 1)] + [_herr(2 + i, "execute_code", 500) for i in range(6)]
        d = dt.desglose(eventos, T0, T0 + 300, 0.0)
        self.assertEqual(d["causa_probable"], "bucle")
        self.assertEqual(d["racha_maxima"], {"herramienta": "execute_code", "veces": 6})

    def test_las_llamadas_con_argumentos_quedan_para_diagnosticar_un_timeout(self):
        eventos = (
            [_paso(1, 1)]
            + [
                {
                    "evento": "llamada",
                    "name": "vision_analyze",
                    "args": f'{{"image_url": "a.png", "question": "q{i}"}}',
                    "huella": f"h{i}",
                    "t": T0 + 2 + i,
                }
                for i in range(12)
            ]
            + [{"evento": "llamada", "name": "read_file", "args": "{}", "huella": "x", "t": T0 + 20} for _ in range(3)]
        )
        d = dt.desglose(eventos, T0, T0 + 600, 0.0)
        self.assertEqual(len(d["ultimas_llamadas"]), 10)
        self.assertEqual(d["ultimas_llamadas"][-1]["name"], "read_file")
        self.assertEqual((d["llamadas_registradas"], d["llamadas_identicas_max"]), (15, 3))

    def test_timeout_sin_fin_del_runner_queda_registrado(self):
        d = dt.desglose([_paso(3, 1)], T0, T0 + 600, 0.0)
        self.assertFalse(d["terminado_por_el_runner"])
        self.assertEqual(d["arranque_runner_s"], 3.0)


class SilencioFinal(unittest.TestCase):
    def test_caso_real_e6bf6bc5_sin_actividad_es_una_respuesta_que_no_llego(self):
        eventos = [_paso(7, 1), {"evento": "recorte", "paso": 1, "recortados": 0, "mensajes": 6, "t": T0 + 7.1}]
        d = dt.desglose(eventos, T0, T0 + 602, 0.1)
        self.assertAlmostEqual(d["silencio_final_s"], 594.9, places=1)
        self.assertAlmostEqual(d["inferencia_s"], 595, places=0)
        self.assertEqual(d["causa_probable"], "modelo_sin_respuesta")

    def test_con_razonamiento_hasta_el_final_es_una_generacion_larga(self):
        eventos = [_paso(7, 1)] + [
            {"evento": "actividad", "paso": 1, "razonamiento_chars": 2000 * i, "t": T0 + 7 + 15 * i}
            for i in range(1, 39)
        ]
        d = dt.desglose(eventos, T0, T0 + 602, 0.1)
        self.assertEqual(d["causa_probable"], "inferencia_lenta")
        self.assertEqual(d["actividad_final"], {"paso": 1, "razonamiento_chars": 76000})
        eventos.append({"evento": "generando_herramienta", "paso": 1, "name": "write_file", "t": T0 + 100})
        silencio = [e for e in eventos if e["t"] <= T0 + 100]
        d = dt.desglose(silencio, T0, T0 + 602, 0.1)
        self.assertEqual(d["causa_probable"], "generacion_larga")
        self.assertEqual(d["ultima_herramienta_generada"], "write_file")

    def test_los_estados_de_hermes_quedan_para_diagnosticar(self):
        eventos = [
            _paso(1, 1),
            {"evento": "estado", "tipo": "lifecycle", "texto": "Retrying API call (2/3)", "t": T0 + 200},
        ]
        d = dt.desglose(eventos, T0, T0 + 600, 0.0)
        self.assertEqual(d["estados"], ["Retrying API call (2/3)"])

    def test_terminado_por_el_runner_no_mide_silencio(self):
        eventos = [_paso(1, 1), {"evento": "fin", "t": T0 + 20}]
        self.assertNotIn("silencio_final_s", dt.desglose(eventos, T0, T0 + 25, 0.0))


class AdjuntarALaTarea(unittest.TestCase):
    def test_lee_el_registro_del_runner_y_lo_guarda_en_la_tarea(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp) / ".hermes" / "tiempos"
            carpeta.mkdir(parents=True)
            inicio = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
            eventos = [
                {"evento": "paso", "n": 1, "t": inicio.timestamp() + 2},
                {"evento": "fin", "t": inicio.timestamp() + 5},
            ]
            (carpeta / "t-1.jsonl").write_text(
                "\n".join(json.dumps(e) for e in eventos) + "\nbasura\n", encoding="utf-8"
            )
            tarea = SimpleNamespace(
                id="t-1",
                metadata_json={"source": "public_chat"},
                queued_at=inicio - timedelta(seconds=1),
                started_at=inicio,
                completed_at=inicio + timedelta(seconds=6),
            )
            dt.adjuntar(tarea, Path(tmp))
        tiempos = tarea.metadata_json["tiempos"]
        self.assertEqual(tarea.metadata_json["source"], "public_chat")
        self.assertEqual((tiempos["cola_s"], tiempos["arranque_runner_s"], tiempos["cierre_s"]), (1.0, 2.0, 1.0))

    def test_sin_registro_ni_workspace_no_rompe_la_tarea(self):
        tarea = SimpleNamespace(id="x", metadata_json={}, queued_at=None, started_at=None, completed_at=None)
        dt.adjuntar(tarea, None)
        self.assertEqual(tarea.metadata_json, {})
        dt.adjuntar(tarea, Path("/no/existe"))
        self.assertEqual(tarea.metadata_json["tiempos"]["causa_probable"], "runner_bloqueado")


if __name__ == "__main__":
    unittest.main()
