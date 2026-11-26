import http.client
import unittest
import urllib.error
from unittest import mock

from hermeshq.services import correo_central as cc
from hermeshq.services import graph_proxy as gp


class LecturaDeEnviadosCaida(unittest.TestCase):
    def _con_error(self, error):
        def _falla(*_a, **_k):
            raise error

        with mock.patch.object(gp, "_llamar", _falla):
            return cc.buscar_en_enviados("token-simulado", "aprob-1")

    def test_conexion_cortada_es_envio_rechazado(self):
        for error in (
            ConnectionResetError("reset"),
            urllib.error.URLError("sin ruta"),
            http.client.RemoteDisconnected("cerrada"),
            TimeoutError("lento"),
        ):
            with self.assertRaises(cc.EnvioRechazado, msg=type(error).__name__) as ctx:
                self._con_error(error)
            self.assertEqual(ctx.exception.motivo, "enviados_no_disponibles")

    def test_respuesta_no_200_sigue_siendo_envio_rechazado(self):
        with mock.patch.object(gp, "_llamar", lambda *_a, **_k: (503, b"")), self.assertRaises(cc.EnvioRechazado):
            cc.buscar_en_enviados("token-simulado", "aprob-1")


if __name__ == "__main__":
    unittest.main()
