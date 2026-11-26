from __future__ import annotations

import json

import pytest

from hermeshq.services import graph_proxy as gp

TODAS = {"ms365-mail": {}, "ms365-calendar": {}, "ms365-teams": {}, "sharepoint": {}}


def _graph_falso(metodo, url, token, cuerpo, tipo, limite):
    if "/me/messages/" in url and "parentFolderId" in url:
        return 200, json.dumps({"id": "m", "parentFolderId": "CARPETA-BAJAS"}).encode()
    if url.split("?")[0].endswith("/me/drive"):
        return 200, json.dumps({"id": "drive-propia"}).encode()
    if "/sites/" in url and url.split("?")[0].endswith("/drive"):
        return 200, json.dumps({"id": "drive-sitio"}).encode()
    if "/sites/" in url:
        return 200, json.dumps({"id": "sitio-1"}).encode()
    return 200, b'{"value": []}'


@pytest.fixture(autouse=True)
def graph(monkeypatch):
    monkeypatch.setattr(gp, "_llamar", _graph_falso)
    monkeypatch.setattr(gp, "_llamar_cabeceras", lambda m, u, t, e, lim: _graph_falso(m, u, t, None, None, lim))


def _ejecutar(politica, ruta, metodo="GET", cuerpo=None):
    return gp.ejecutar(metodo, ruta, cuerpo, None, politica, lambda: "t", lambda t: "drive-propia")


class TestCompatibilidad:
    def test_correo_de_cualquier_carpeta_como_antes(self):
        politica, origen = gp.politica_de_agente({"ms365-mail": {}}, compatibilidad=True)
        assert origen == "compatibilidad"
        assert _ejecutar(politica, "/me/mailFolders/AAMkCarpetaBajas=/messages?$top=10")["status"] == 200
        assert _ejecutar(politica, "/me/messages/AAMkMensaje1=?$select=subject")["status"] == 200
        assert _ejecutar(politica, '/me/mailFolders/AAMkCarpetaBajas=/messages?$search="baja"&$top=5')["status"] == 200
        assert _ejecutar(politica, "/me/messages/AAMkMensaje1=/attachments")["status"] == 200
        assert _ejecutar(politica, "/me/messages/AAMkMensaje1=", "PATCH", {"isRead": True})["status"] == 200

    def test_solo_lo_que_esta_instalado(self):
        politica, _ = gp.politica_de_agente({"ms365-mail": {}}, compatibilidad=True)
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/calendarView?startDateTime=2026-11-01T00:00:00Z&endDateTime=2026-11-02T00:00:00Z")
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/drive/root/children")

    def test_calendario_teams_y_sharepoint_con_todas_las_integraciones(self):
        politica, _ = gp.politica_de_agente(TODAS, compatibilidad=True)
        assert (
            _ejecutar(politica, "/me/calendarView?startDateTime=2026-11-01T00:00:00Z&endDateTime=2026-11-02T00:00:00Z")[
                "status"
            ]
            == 200
        )
        assert _ejecutar(politica, "/me/chats")["status"] == 200
        assert _ejecutar(politica, "/me/drive/root/children")["status"] == 200
        assert _ejecutar(politica, "/me/drive/root:/Documentos/Informes:/children")["status"] == 200
        assert _ejecutar(politica, "/drives/drive-otra/root:/Compartido:/children")["status"] == 200
        assert (
            _ejecutar(politica, "/sites/contoso.sharepoint.com:/sites/Finanzas:/drive/root:/Actas:/children")["status"]
            == 200
        )

    def test_las_escrituras_siguen_necesitando_aprobacion(self):
        assert gp.clase_de("POST", "/me/sendMail") in ("accion_externa", "denegada")
        assert gp.clase_de("POST", "/me/events") in ("accion_externa", "denegada")
        assert gp.clase_de("POST", "/me/chats/19:x/messages") in ("accion_externa", "denegada")
        politica, _ = gp.politica_de_agente(TODAS, compatibilidad=True)
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/drive/items/item-1234/createLink", "POST", {"type": "view", "scope": "anonymous"})

    def test_politica_explicita_manda_y_restringe(self):
        configs = {**TODAS, "m365_graph": {"correo": {"operaciones": ["mail_list"], "carpetas": ["inbox"]}}}
        politica, origen = gp.politica_de_agente(configs, compatibilidad=True)
        assert origen == "m365_graph"
        assert _ejecutar(politica, "/me/mailFolders/inbox/messages?$top=5")["status"] == 200
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/mailFolders/AAMkCarpetaBajas=/messages?$top=10")
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/drive/root/children")

    def test_compatibilidad_desactivada_deniega_todo(self):
        politica, origen = gp.politica_de_agente(TODAS, compatibilidad=False)
        assert origen == "sin_politica"
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/mailFolders/inbox/messages?$top=5")

    def test_sin_integraciones_m365_no_hay_acceso(self):
        politica, origen = gp.politica_de_agente({"voice-edge": {}}, compatibilidad=True)
        assert origen == "sin_politica"
        with pytest.raises(gp.RutaNoPermitida):
            _ejecutar(politica, "/me/mailFolders/inbox/messages?$top=5")

    def test_por_defecto_la_compatibilidad_esta_activada(self):
        _, origen = gp.politica_de_agente({"ms365-mail": {}})
        assert origen == "compatibilidad"
