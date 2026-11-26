import hermeshq.scripts.hermes_task_runner as runner
from tests.test_revisor_html_js import requiere_jsdom

ROTO_TODO_INERTE = (
    "<html><body><div id='a'></div><script>"
    "var xs=['a','b','c','d','e','f','g','h'];"
    "document.getElementById('a').innerHTML=xs.map(function(x){return '<div onclick=\"abrir(\\''+x+'\\')\">'+x+'</div>'}).join('');"
    "function abrir(x){var y=x}"
    "</script></body></html>"
)
INERTE_CON_LISTENERS = (
    "<html><body><div id='a'></div><script>"
    "var xs=['a','b','c','d','e','f','g','h'];var raiz=document.getElementById('a');"
    "xs.forEach(function(x){var d=document.createElement('div');d.textContent=x;d.addEventListener('click',function(){var y=x});raiz.appendChild(d);});"
    "</script></body></html>"
)
SANO_CON_LISTENERS = (
    "<html><body><div id='a'></div><script>"
    "var xs=['a','b','c','d','e','f','g','h'];var raiz=document.getElementById('a');"
    "xs.forEach(function(x){var d=document.createElement('div');d.textContent=x;d.addEventListener('click',function(){d.classList.toggle('abierto')});raiz.appendChild(d);});"
    "</script></body></html>"
)
SANO = (
    "<html><body><div id='a'></div><script>"
    "var xs=['a','b','c','d','e','f','g','h'];var abierto=null;"
    "function pintar(){document.getElementById('a').innerHTML=xs.map(function(x){return '<div onclick=\"abrir(\\''+x+'\\')\">'+x+"
    "(abierto===x?' (abierto)':'')+'</div>'}).join('')}"
    "function abrir(x){abierto=x;pintar()}pintar();"
    "</script></body></html>"
)


def _revisar(tmp_path, html):
    ruta = tmp_path / "x.html"
    ruta.write_text(html, encoding="utf-8")
    return runner.revisar_html_js(ruta)


def _pendientes(tmp_path, monkeypatch, html):
    (tmp_path / "outputs").mkdir(exist_ok=True)
    (tmp_path / "outputs" / "app.html").write_text(html, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return runner.errores_js_de_adjuntos("Listo.\nMEDIA:outputs/app.html", str(tmp_path))


@requiere_jsdom
class TestClicSinEfecto:
    def test_pagina_sana_cuenta_clics_con_efecto(self, tmp_path):
        r = _revisar(tmp_path, SANO)
        assert r["con_onclick"] >= 1 and r["sin_efecto"] == 0

    def test_pagina_inerte_cuenta_todos_sin_efecto(self, tmp_path):
        r = _revisar(tmp_path, ROTO_TODO_INERTE)
        assert r["con_onclick"] == 8 and r["sin_efecto"] == 8

    def test_positivo_todo_inerte_se_marca(self, tmp_path, monkeypatch):
        pendientes = _pendientes(tmp_path, monkeypatch, ROTO_TODO_INERTE)
        assert [p["tipo"] for p in pendientes] == ["html_sin_efecto"]
        assert "no cambia" in pendientes[0]["resumen"]
        nota = runner.nota_verificacion(pendientes)
        assert "navegador de prueba" in nota
        final = runner.respuesta_verificada(
            "Listo, el explorador de carpetas queda en el archivo que te adjunto con sus tres pestañas.\nMEDIA:outputs/app.html",
            pendientes,
        )
        assert "no responde al usuario" in final and "MEDIA:outputs/app.html" in final

    def test_negativo_pagina_sana_no_se_marca(self, tmp_path, monkeypatch):
        assert _pendientes(tmp_path, monkeypatch, SANO) == []

    def test_negativo_pocos_onclick_no_cuentan(self, tmp_path, monkeypatch):
        html = "<html><body><button onclick='void 0'>a</button><button onclick='void 0'>b</button><script>1</script></body></html>"
        assert _pendientes(tmp_path, monkeypatch, html) == []

    def test_positivo_clics_con_addeventlistener_que_no_hacen_nada(self, tmp_path, monkeypatch):
        pendientes = _pendientes(tmp_path, monkeypatch, INERTE_CON_LISTENERS)
        assert [p["tipo"] for p in pendientes] == ["html_sin_efecto"]

    def test_negativo_clics_con_addeventlistener_que_cambian_la_pagina(self, tmp_path, monkeypatch):
        assert _pendientes(tmp_path, monkeypatch, SANO_CON_LISTENERS) == []
