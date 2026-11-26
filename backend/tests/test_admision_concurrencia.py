import asyncio
import unittest

from hermeshq.services import admision
from hermeshq.services.admision import CHAT, CRON, SEGUNDO_PLANO, ControlAdmision, normalizar

LIBRE = {"chat": 0, "cron": 0, "segundo_plano": 0}


TABLA_CONFIGURACIONES = [
    ((8, 2, 0), (8, 2, 6, 5, False), False),
    ((8, 2, 5), (8, 2, 6, 5, False), False),
    ((8, 2, 6), (8, 2, 6, 5, False), True),
    ((8, 2, 99), (8, 2, 6, 5, False), True),
    ((8, 0, 0), (8, 0, 8, 7, False), False),
    ((4, 1, 3), (4, 1, 3, 2, False), True),
    ((4, 1, 9), (4, 1, 3, 2, False), True),
    ((4, 2, 0), (4, 2, 2, 1, False), False),
    ((3, 2, 3), (3, 2, 1, 1, False), True),
    ((3, 2, 0), (3, 2, 1, 1, False), True),
    ((3, 5, 0), (3, 2, 1, 1, False), True),
    ((2, 2, 0), (2, 1, 1, 1, False), True),
    ((2, 0, 5), (2, 0, 2, 1, False), True),
    ((1, 2, 0), (1, 0, 1, 1, True), True),
    ((1, 0, 0), (1, 0, 1, 1, False), True),
    ((0, 0, 0), (1, 0, 1, 1, False), True),
    ((5, -1, -3), (5, 0, 5, 4, False), True),
    (("x", None, "y"), (1, 0, 1, 1, False), True),
]


class _Reloj:
    def __init__(self) -> None:
        self.ahora = 0.0

    def __call__(self) -> float:
        return self.ahora


async def _tomar(control: ControlAdmision, clase: str, tomados: list[str], nombre: str) -> None:
    await control.adquirir(clase)
    tomados.append(nombre)


class TestControlAdmision(unittest.IsolatedAsyncioTestCase):
    def _control(self, total, reserva=2, tope=0, envejecimiento=300.0) -> tuple[ControlAdmision, _Reloj]:
        reloj = _Reloj()
        return ControlAdmision(normalizar(total, reserva, tope, envejecimiento)[0], reloj=reloj), reloj

    async def _encolar(self, control, reloj, clase, nombre, tomados, avance=1.0) -> asyncio.Task:
        reloj.ahora += avance
        tarea = asyncio.create_task(_tomar(control, clase, tomados, nombre))
        await asyncio.sleep(0)
        return tarea

    async def test_cupo_uno_el_cron_entra_antes_que_el_segundo_plano_que_ya_esperaba(self):
        control, reloj = self._control(3, reserva=2, tope=3)
        tomados: list[str] = []
        await self._encolar(control, reloj, SEGUNDO_PLANO, "bg-1", tomados)
        esperando = [await self._encolar(control, reloj, SEGUNDO_PLANO, f"bg-{n}", tomados) for n in (2, 3, 4)]
        crons = [await self._encolar(control, reloj, CRON, f"cron-{n}", tomados) for n in (1, 2)]
        self.assertEqual(tomados, ["bg-1"])
        control.liberar(SEGUNDO_PLANO)
        await asyncio.sleep(0)
        self.assertEqual(tomados, ["bg-1", "cron-1"])
        control.liberar(CRON)
        await asyncio.sleep(0)
        control.liberar(CRON)
        await asyncio.sleep(0)
        self.assertEqual(tomados, ["bg-1", "cron-1", "cron-2", "bg-2"])
        for _ in range(3):
            control.liberar(SEGUNDO_PLANO)
            await asyncio.sleep(0)
        await asyncio.gather(*esperando, *crons)
        self.assertEqual(tomados[-2:], ["bg-3", "bg-4"])

    async def test_envejecimiento_el_segundo_plano_no_sufre_inanicion_con_rafagas_de_crons(self):
        control, reloj = self._control(3, reserva=2, envejecimiento=60.0)
        tomados: list[str] = []
        await self._encolar(control, reloj, CRON, "cron-0", tomados)
        fondo = await self._encolar(control, reloj, SEGUNDO_PLANO, "bg", tomados)
        pendientes = [fondo]
        for n in range(1, 200):
            pendientes.append(await self._encolar(control, reloj, CRON, f"cron-{n}", tomados, avance=5.0))
            control.liberar(CRON if tomados[-1].startswith("cron") else SEGUNDO_PLANO)
            await asyncio.sleep(0)
            if "bg" in tomados:
                break
        self.assertIn("bg", tomados)
        posicion = tomados.index("bg")
        self.assertLessEqual(posicion, 14, tomados)
        self.assertGreaterEqual(posicion, 10, tomados)
        for tarea in pendientes:
            if not tarea.done():
                control.liberar(CRON if tomados[-1].startswith("cron") else SEGUNDO_PLANO)
                await asyncio.sleep(0)
        for tarea in pendientes:
            tarea.cancel()
        await asyncio.gather(*pendientes, return_exceptions=True)

    async def test_total_uno_el_chat_pasa_primero_y_los_no_interactivos_envejecen(self):
        control, reloj = self._control(1, reserva=2, envejecimiento=60.0)
        tomados: list[str] = []
        await self._encolar(control, reloj, SEGUNDO_PLANO, "bg-1", tomados)
        cron = await self._encolar(control, reloj, CRON, "cron", tomados)
        fondo = await self._encolar(control, reloj, SEGUNDO_PLANO, "bg-2", tomados)
        chat = await self._encolar(control, reloj, CHAT, "chat-1", tomados)
        control.liberar(SEGUNDO_PLANO)
        await asyncio.sleep(0)
        self.assertEqual(tomados, ["bg-1", "chat-1"])
        await self._encolar(control, reloj, CHAT, "chat-tarde", tomados, avance=120.0)
        control.liberar(CHAT)
        await asyncio.sleep(0)
        self.assertEqual(tomados[-1], "cron", "un cron que esperó más que el envejecimiento no cede al chat")
        control.liberar(CRON)
        await asyncio.sleep(0)
        control.liberar(tomados[-1].split("-")[0] if tomados[-1].startswith("chat") else SEGUNDO_PLANO)
        await asyncio.sleep(0)
        self.assertEqual(sorted(tomados), sorted(["bg-1", "chat-1", "cron", "chat-tarde", "bg-2"]))
        await asyncio.gather(cron, fondo, chat)
        self.assertLessEqual(sum(control.en_curso.values()), 1)

    async def test_bajar_el_total_en_caliente_nunca_admite_mas_que_el_nuevo_total(self):
        control, reloj = self._control(4, reserva=0)
        tomados: list[str] = []
        for n in range(4):
            await self._encolar(control, reloj, CHAT, f"chat-{n}", tomados)
        control.configurar(normalizar(2, 0, 0)[0])
        nuevos = [await self._encolar(control, reloj, CHAT, f"nuevo-{n}", tomados) for n in range(3)]
        for liberados in range(1, 4):
            control.liberar(CHAT)
            await asyncio.sleep(0)
            self.assertLessEqual(sum(control.en_curso.values()), max(2, 4 - liberados))
        self.assertEqual([t for t in tomados if t.startswith("nuevo")], ["nuevo-0"])
        control.configurar(normalizar(6, 0, 0)[0])
        await asyncio.gather(*nuevos)
        self.assertEqual(control.en_curso[CHAT], 4)

    async def test_cancelacion_justo_despues_de_recibir_el_cupo_lo_devuelve(self):
        control, reloj = self._control(3, reserva=2)
        tomados: list[str] = []
        await self._encolar(control, reloj, CRON, "cron-1", tomados)
        segundo = await self._encolar(control, reloj, CRON, "cron-2", tomados)
        control.liberar(CRON)
        segundo.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await segundo
        self.assertEqual(control.en_curso, LIBRE)
        self.assertEqual(control.estado()["en_espera"], LIBRE)

    async def test_liberar_de_mas_no_corrompe_los_contadores(self):
        control, _ = self._control(3)
        with self.assertLogs("hermeshq.services.admision", level="ERROR"):
            control.liberar(CRON)
        self.assertEqual(control.en_curso, LIBRE)


CONFIGURACIONES_EXTREMAS = [(1, 2, 0), (1, 0, 0), (2, 2, 0), (2, 0, 9), (3, 2, 3), (3, 5, 0), (4, 1, 9), (8, 2, 0)]


if __name__ == "__main__":
    unittest.main()


class TestNormalizacion(unittest.TestCase):
    def test_tabla_de_configuraciones(self):
        for (total, reserva, tope), esperado, con_aviso in TABLA_CONFIGURACIONES:
            with self.subTest(total=total, reserva=reserva, tope=tope):
                limites, avisos = normalizar(total, reserva, tope)
                obtenido = (
                    limites.total,
                    limites.reserva,
                    limites.no_interactivo,
                    limites.segundo_plano,
                    limites.chat_preferente,
                )
                self.assertEqual(obtenido, esperado)
                self.assertEqual(bool(avisos), con_aviso, avisos)
                self.assertGreaterEqual(limites.no_interactivo, 1)
                self.assertLessEqual(limites.no_interactivo, limites.total)
                self.assertEqual(limites.reserva + limites.no_interactivo, limites.total)
                if limites.no_interactivo >= 2:
                    self.assertLess(limites.segundo_plano, limites.no_interactivo)

    def test_envejecimiento_invalido_se_normaliza(self):
        self.assertEqual(normalizar(8, 2, 0, -5)[0].envejecimiento_s, 0.0)
        self.assertEqual(normalizar(8, 2, 0, "x")[0].envejecimiento_s, admision.ENVEJECIMIENTO_POR_DEFECTO_S)

    def test_configuracion_por_defecto_no_avisa(self):
        self.assertEqual(normalizar(8, 2, 0)[1], [])
