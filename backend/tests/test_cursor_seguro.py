import pytest

from hermeshq.services import cursor_seguro as cs

S = "secreto-cursor-sintetico"
CTX = {"usuario": "u1", "agente": "a1", "tarea": "t1"}
AHORA = 1_000_000.0


def emitido(**kw):
    base = dict(contexto=CTX, carpeta="inbox", consulta="plan", top=10, offset=10, ahora=AHORA)
    base.update(kw)
    return cs.emitir(S, **base)


def validar(cur, **kw):
    base = dict(contexto=CTX, carpeta="inbox", consulta="plan", top=10, ahora=AHORA + 1)
    base.update(kw)
    return cs.validar(S, cur, **base)


class TestCursor:
    def test_valido_devuelve_offset(self):
        assert validar(emitido()) == 10

    @pytest.mark.parametrize(
        "cambio",
        [
            {"contexto": {"usuario": "u1", "agente": "a1", "tarea": "t-otra"}},
            {"contexto": {"usuario": "u1", "agente": "a-otro", "tarea": "t1"}},
            {"contexto": {"usuario": "u-otro", "agente": "a1", "tarea": "t1"}},
            {"carpeta": "sentitems"},
            {"consulta": "otra consulta"},
            {"top": 25},
        ],
    )
    def test_cambio_de_vinculo_se_rechaza(self, cambio):
        with pytest.raises(cs.CursorNoValido):
            validar(emitido(), **cambio)

    def test_expirado(self):
        with pytest.raises(cs.CursorNoValido) as e:
            validar(emitido(), ahora=AHORA + cs.DURACION_SEGUNDOS + 5)
        assert str(e.value) == "caducado"

    def test_manipulado_en_el_cuerpo(self):
        cur = emitido()
        cuerpo, sello = cur.split(".", 1)
        import base64
        import json

        datos = json.loads(base64.urlsafe_b64decode(cuerpo))
        datos["c"] = "sentitems"
        forjado = base64.urlsafe_b64encode(json.dumps(datos, sort_keys=True, separators=(",", ":")).encode()).decode()
        with pytest.raises(cs.CursorNoValido) as e:
            validar(forjado + "." + sello)
        assert str(e.value) == "firma"

    def test_con_otro_secreto(self):
        cur = cs.emitir("otro", CTX, "inbox", "plan", 10, 10, AHORA)
        with pytest.raises(cs.CursorNoValido):
            validar(cur)

    @pytest.mark.parametrize("basura", ["", "x", "....", "@@@.@@@", "AAAA.BBBB"])
    def test_basura_se_rechaza(self, basura):
        with pytest.raises(cs.CursorNoValido):
            validar(basura)

    def test_offset_fuera_de_limite_no_se_emite_valido(self):
        cur = emitido(offset=cs.MAX_OFFSET + 1)
        with pytest.raises(cs.CursorNoValido):
            validar(cur)
