import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from hermeshq.core.security import create_access_token, get_current_user, ruta_permitida_a_cuenta_de_servicio
from hermeshq.models.user import User


class _Resultado:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user

    def scalars(self):
        return self

    def first(self):
        return self._user


class _Sesion:
    def __init__(self, user):
        self.user = user

    async def execute(self, *_a, **_k):
        return _Resultado(self.user)

    async def get(self, *_a, **_k):
        return self.user


def _usuario(nombre, rol="user"):
    return User(id="u1", username=nombre, display_name=nombre, password_hash="h", role=rol, is_active=True)


def _peticion(ruta):
    return SimpleNamespace(url=SimpleNamespace(path=ruta))


class TestRutas(unittest.TestCase):
    def test_solo_adjuntos(self):
        for ruta in ("/api/agents/abc/attachments", "/api/agents/abc/attachments/f1", "/api/agents/abc/attachments/"):
            self.assertTrue(ruta_permitida_a_cuenta_de_servicio(ruta, "/api"), ruta)

    def test_el_resto_de_la_plataforma_no(self):
        for ruta in (
            "/api/agents",
            "/api/agents/abc",
            "/api/agents/abc/tasks",
            "/api/tasks",
            "/api/secrets",
            "/api/users",
            "/api/settings",
            "/api/agents/abc/attachments/f1/otra",
            "/api/agents/abc/files",
        ):
            self.assertFalse(ruta_permitida_a_cuenta_de_servicio(ruta, "/api"), ruta)


class TestGetCurrentUser(unittest.IsolatedAsyncioTestCase):
    async def _llamar(self, usuario, ruta):
        token, _ = create_access_token(usuario.id, subject_kind="id")
        return await get_current_user(
            request=_peticion(ruta), bearer_token=token, cookie_token=None, db=_Sesion(usuario)
        )

    async def test_el_bridge_entrega_adjuntos(self):
        r = await self._llamar(_usuario("bridge-teams"), "/api/agents/a1/attachments/f1")
        self.assertEqual(r.username, "bridge-teams")

    async def test_el_bridge_no_toca_otras_rutas(self):
        for ruta in ("/api/agents", "/api/tasks", "/api/agents/a1/tasks"):
            with self.assertRaises(HTTPException) as cm:
                await self._llamar(_usuario("bridge-teams"), ruta)
            self.assertEqual(cm.exception.status_code, 403, ruta)

    async def test_control_negativo_un_usuario_normal_conserva_sus_rutas(self):
        r = await self._llamar(_usuario("bruno"), "/api/tasks")
        self.assertEqual(r.username, "bruno")
        r = await self._llamar(_usuario("admin", "admin"), "/api/agents")
        self.assertEqual(r.username, "admin")
