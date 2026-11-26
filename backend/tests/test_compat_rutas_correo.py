from __future__ import annotations

import json

import pytest

from hermeshq.services import graph_proxy as gp

RUTAS_MODO_BROKER = {
    "ms365_inbox_estado: no leídos": "/me/mailFolders/inbox/messages?$top=100&$select=id,conversationId,receivedDateTime"
    "&$filter=isRead%20eq%20false",
    "ms365_inbox_estado: respondidos": "/me/mailFolders/sentitems/messages?$top=100&$select=conversationId,sentDateTime"
    "&$orderby=sentDateTime%20desc",
    "ms365_correo_sync: bandeja": "/me/mailFolders/inbox/messages?$top=50&$select=id,subject,from,receivedDateTime,"
    "bodyPreview,isRead,hasAttachments",
    "ms365_find_person: correo": "/me/mailFolders/inbox/messages?$search=%22ana%22&$top=25",
    "ms365_find_person: enviados": "/me/mailFolders/sentitems/messages?$search=%22ana%22&$top=25",
    "ms365_find_person: agenda": "/me/calendarView?startDateTime=2026-10-01T00:00:00Z&endDateTime=2026-11-09T00:00:00Z"
    "&$top=100&$select=organizer,attendees,start",
    "ms365_mail_get": "/me/messages/AAMkAGI2THVSAAA=?$select=subject,body,from",
    "ms365_mail_search": '/me/mailFolders/inbox/messages?$search="presupuesto"&$top=10',
    "ms365_mail_attachments": "/me/messages/AAMkAGI2THVSAAA=/attachments?$select=id,name,size,isInline,contentType"
    "&$top=50",
}
POLITICA = {
    "correo": {"operaciones": sorted(gp.OPERACIONES_CORREO), "carpetas": ["inbox", "sentitems"]},
    "calendario": {"operaciones": ["lectura"]},
}


def _graph_falso(metodo, url, token, cuerpo, tipo, limite):
    if url.split("?")[0].endswith(("/mailFolders/inbox", "/mailFolders/sentitems")):
        return 200, json.dumps({"id": "INBOX-ID" if "inbox" in url else "SENT-ID"}).encode()
    if "/me/messages/" in url and "/attachments" not in url:
        return 200, json.dumps({"id": "x", "parentFolderId": "INBOX-ID"}).encode()
    return 200, b'{"value": []}'


@pytest.mark.parametrize("herramienta", sorted(RUTAS_MODO_BROKER))
def test_lectura_del_plugin_sigue_permitida(herramienta, monkeypatch):
    monkeypatch.setattr(gp, "_llamar", _graph_falso)
    monkeypatch.setattr(gp, "_llamar_cabeceras", lambda m, u, t, e, lim: _graph_falso(m, u, t, None, None, lim))
    politica = gp.politica_desde_configuracion(POLITICA)
    resultado = gp.ejecutar("GET", RUTAS_MODO_BROKER[herramienta], None, None, politica, lambda: "t", lambda t: None)
    assert resultado["status"] == 200


def test_sin_politica_de_correo_se_siguen_denegando():
    politica = gp.politica_desde_configuracion({})
    with pytest.raises(gp.RutaNoPermitida):
        gp.ejecutar("GET", RUTAS_MODO_BROKER["ms365_mail_get"], None, None, politica, lambda: "t", lambda t: None)
