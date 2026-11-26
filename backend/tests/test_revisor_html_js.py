import importlib.util
import json
import shutil
from pathlib import Path

import pytest

import hermeshq.scripts.hermes_task_runner as runner

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_REVISOR = _RAIZ / "assets" / "revisor-html" / "revisar.js"
_CON_NAVEGADOR = (_RAIZ / "assets" / "revisor-html" / "node_modules" / "jsdom").is_dir()

requiere_node = pytest.mark.skipif(not shutil.which("node"), reason="sin node")
requiere_jsdom = pytest.mark.skipif(not (shutil.which("node") and _CON_NAVEGADOR), reason="sin jsdom instalado")


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


COMILLA_SIN_CERRAR = """<!DOCTYPE html><html><head><title>Estructura DEMO</title></head><body>
<div id="tabs"><button>Opción 1</button></div><div id="v1"></div>
<script>
function detail(i,l,t){return `<div class="detail"><h3>${i} ${l}</h3><p>${t}</p></div>`;}
function render1(v,data){
  v.innerHTML=`<div class="grid">${data.map(s=>`<div class="obj">${s}</div>`).join('')}</div>
      ${detail('🏭','Sucursal seleccionada','Aquí se despliegan las áreas.')};
  return;
}
function render2(v){
  v.innerHTML=`<p class="hint">Sucursal — selecciona un área.</p>`;
}
render1(document.getElementById('v1'),['SUC1','SUC2']);
</script></body></html>"""

TERNARIO_ROTO = """<!DOCTYPE html><html><body><ul id="arbol"></ul>
<script>
var nodo = {nombre: 'DEMO'}, tieneHijos = true;
var dataAttr = tieneHijos ? (' data-nombre="' + nodo.nombre + '"' : ' data-nombre="' + nodo.nombre + '"';
document.getElementById('arbol').innerHTML = '<li' + dataAttr + '>DEMO</li>';
</script></body></html>"""

ELEMENTO_INEXISTENTE = """<!DOCTYPE html><html><body><aside id="sidebar"></aside>
<script>
const sb = document.getElementById('sideBar');
sb.addEventListener('transitionend', function(){});
</script></body></html>"""

FALLA_AL_CLIC = """<!DOCTYPE html><html><body><div id="grid"></div><button id="reset2">⟲ Restablecer</button>
<script>
const S2 = {};
document.getElementById('grid').textContent = 'DEMO';
document.getElementById('reset2').addEventListener('click', function(){ S2.socs.forEach(function(){}); });
</script></body></html>"""

PAGINA_SANA = """<!DOCTYPE html><html><head><title>Explorador Demo</title></head><body>
<nav><button class="tab" data-tab="1">Opción 1</button><button class="tab" data-tab="2">Opción 2</button></nav>
<input type="search" id="busqueda"><select id="soc"><option>SEDE</option><option>SERV</option></select>
<canvas id="graf"></canvas><div id="panel"></div><svg><rect id="r"></rect></svg>
<script>
const datos = {GRUPO_A: ['SUC1','SUC2','SUC6'], GRUPO_B: ['SUC7','SUC8']};
let actual = 'GRUPO_A';
function pintar(){
  const filtro = document.getElementById('busqueda').value.toLowerCase();
  document.getElementById('panel').innerHTML = datos[actual].filter(s => s.toLowerCase().includes(filtro))
    .map(s => `<div class="obj" onclick="elegir('${s}')">${s}</div>`).join('');
}
function elegir(s){ document.getElementById('panel').dataset.sel = s; }
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => { pintar(); }));
document.getElementById('busqueda').addEventListener('input', pintar);
document.getElementById('soc').addEventListener('change', e => { actual = e.target.value; pintar(); });
const ctx = document.getElementById('graf').getContext('2d');
ctx.fillStyle = '#1B3666'; ctx.fillRect(0, 0, ctx.measureText('x').width + 10, 10);
const oscuro = window.matchMedia('(prefers-color-scheme: dark)').matches;
document.getElementById('r').getBBox();
window.scrollTo(0, 0);
document.getElementById('panel').scrollIntoView();
new ResizeObserver(() => {}).observe(document.body);
pintar();
</script></body></html>"""

CON_CDN = """<!DOCTYPE html><html><head>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script></head><body>
<canvas id="c"></canvas><script>new Chart(document.getElementById('c'), {type: 'bar', data: {}});</script>
</body></html>"""


def _revisar(tmp_path, html, nombre="pagina.html"):
    ruta = tmp_path / nombre
    ruta.write_text(html, encoding="utf-8")
    return runner.revisar_html_js(ruta)


@requiere_node
class TestRevisorSintaxis:
    def test_comilla_invertida_sin_cerrar_del_caso_real(self, tmp_path):
        revision = _revisar(tmp_path, COMILLA_SIN_CERRAR)
        assert revision["modo"] == "sintaxis"
        error = revision["errores_carga"][0]
        assert error["mensaje"].startswith("SyntaxError")
        assert error["linea"] == 11
        assert "selecciona un área" in error["codigo"]

    def test_ternario_con_parentesis_sin_cerrar(self, tmp_path):
        error = _revisar(tmp_path, TERNARIO_ROTO)["errores_carga"][0]
        assert error["mensaje"] == "SyntaxError: Unexpected token ':'"
        assert error["linea"] == 4
        assert "tieneHijos ? (" in error["codigo"]

    def test_json_y_plantillas_no_se_ejecutan(self, tmp_path):
        html = (
            '<html><body><script type="application/json" id="hq-spec">{"a": [1, 2}</script>'
            '<script type="text/template"><div>${x</div></script></body></html>'
        )
        assert _revisar(tmp_path, html)["errores_carga"] == []


class TestRevisorAusente:
    def test_sin_node_no_bloquea(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda _n: None)
        assert _revisar(tmp_path, TERNARIO_ROTO, "otra.html") is None

    def test_salida_ilegible_no_bloquea(self, tmp_path, monkeypatch):
        class _Proceso:
            stdout = "Killed"

        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: _Proceso())
        monkeypatch.setattr(runner.shutil, "which", lambda _n: "/usr/bin/node")
        assert _revisar(tmp_path, TERNARIO_ROTO, "tercera.html") is None


ROTO = {
    "modo": "sintaxis",
    "errores_carga": [
        {"mensaje": "SyntaxError: Unexpected token ':'", "linea": 852, "codigo": "var dataAttr = tieneHijos ? ("}
    ],
    "errores_clic": [],
}
CLIC = {
    "modo": "jsdom",
    "errores_carga": [],
    "errores_clic": [
        {
            "fase": "clic",
            "mensaje": "TypeError: Cannot read properties of undefined (reading 'forEach')",
            "linea": 537,
            "codigo": "S2.socs.forEach(s=>{",
            "elemento": "button#reset2 «⟲ Restablecer»",
        }
    ],
}
RESPUESTA = (
    "## Listo — Explorador de Carpetas Demo\n\nConstruí la app de navegación de carpetas con árbol colapsable, "
    "breadcrumbs y buscador en tiempo real, fiel a la paleta corporativa.\n\nMEDIA: outputs/Explorador_Accesos_Demo.html"
)


class TestVerificacion:
    def _preparar(self, tmp_path, monkeypatch, revision):
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "Explorador_Accesos_Demo.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(runner, "revisar_html_js", lambda _ruta: revision)
        return [p for p in runner.verificar_afirmaciones(RESPUESTA, [], 0, cwd=str(tmp_path)) if "html" in p["tipo"]]

    def test_html_roto_pide_corregir_el_archivo_con_la_linea(self, tmp_path, monkeypatch):
        pendientes = self._preparar(tmp_path, monkeypatch, ROTO)
        assert [p["tipo"] for p in pendientes] == ["html_roto"]
        assert pendientes[0]["frase"] == "outputs/Explorador_Accesos_Demo.html"
        nota = runner.nota_verificacion(pendientes)
        assert "navegador de prueba" in nota
        assert "línea 852: SyntaxError: Unexpected token ':'" in nota
        assert "patch" in nota and "MEDIA:" in nota
        assert "afirmaciones sin respaldo" not in nota

    def test_si_sigue_roto_no_se_entrega_ni_se_describe(self, tmp_path, monkeypatch):
        pendientes = self._preparar(tmp_path, monkeypatch, ROTO)
        final = runner.respuesta_verificada(RESPUESTA, pendientes)
        assert "MEDIA:" not in final
        assert "árbol colapsable" not in final
        assert "No te entrego «Explorador_Accesos_Demo.html»" in final
        assert (
            "lo abrí en un navegador de prueba y su JavaScript falla (línea 852: SyntaxError: Unexpected token ':')"
            in final
        )

    def test_error_al_clic_se_entrega_con_aviso(self, tmp_path, monkeypatch):
        pendientes = self._preparar(tmp_path, monkeypatch, CLIC)
        assert [p["tipo"] for p in pendientes] == ["html_clic"]
        assert "al hacer clic en button#reset2" in runner.nota_verificacion(pendientes)
        final = runner.respuesta_verificada(RESPUESTA, pendientes)
        assert "MEDIA: outputs/Explorador_Accesos_Demo.html" in final
        assert "al hacer clic en button#reset2 «⟲ Restablecer» su JavaScript falla (línea 537: TypeError" in final

    def test_pagina_sana_no_genera_pendientes(self, tmp_path, monkeypatch):
        sana = {"modo": "jsdom", "errores_carga": [], "errores_clic": [], "clics": 12}
        assert self._preparar(tmp_path, monkeypatch, sana) == []

    def test_otros_formatos_no_se_revisan(self, tmp_path, monkeypatch):
        (tmp_path / "informe.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(runner, "revisar_html_js", lambda _r: pytest.fail("revisó un PDF"))
        assert runner.errores_js_de_adjuntos("Listo.\n\nMEDIA: informe.pdf", str(tmp_path)) == []

    def test_nota_mixta_conserva_el_detalle(self, tmp_path, monkeypatch):
        pendientes = self._preparar(tmp_path, monkeypatch, ROTO) + [{"tipo": "subida", "frase": "subido a OneDrive"}]
        nota = runner.nota_verificacion(pendientes)
        assert "(error: línea 852" in nota
        assert "sharepoint_upload_file" in nota


def test_revisor_en_su_lugar():
    assert _REVISOR.is_file()
    paquete = json.loads((_REVISOR.parent / "package.json").read_text(encoding="utf-8"))
    assert paquete["dependencies"]["jsdom"] == "30.0.1"


RESPUESTA_CORREGIDO_FALSO = (
    "No pude subir el archivo. Esto es lo que pasó:\n\n"
    "- El archivo `outputs/prueba_js.html` tenía un error de JavaScript (faltaba un paréntesis). Ya lo corregí — "
    "ahora el código está válido.\n"
    "- Al subirlo a OneDrive (carpeta **UltraBot-archivos**), la herramienta lo rechazó 4 veces seguidas con un error "
    "de JavaScript que ya no existe en el archivo. Es decir, el archivo ya está bien pero el sistema de subida sigue "
    "rechazándolo.\n"
    "- Registré la incidencia con el administrador (ID `55967e31`) para que la evalúe."
)
ARREGLO_FALLIDO = (
    "<html><body><h1>Sociedades Demo</h1><div id=\"v\"></div><script>var u = true ? ('GRUPO_A' : 'GRUPO_B'); "
    "document.getElementById('v').textContent = u;</script></body></html>"
)


def _turno_con_patch(ruta):
    return [
        {"role": "user", "content": "Sube a mi OneDrive el archivo outputs/prueba_js.html tal como está."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "p1", "function": {"name": "patch", "arguments": json.dumps({"path": ruta, "old_string": "("})}}
            ],
        },
        {"role": "tool", "tool_call_id": "p1", "content": json.dumps({"success": True})},
    ]


@requiere_node
class TestCorregidoFalso:
    def _caso(self, tmp_path, monkeypatch, html):
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "prueba_js.html").write_text(html, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        mensajes = _turno_con_patch("outputs/prueba_js.html")
        return runner.verificar_afirmaciones(RESPUESTA_CORREGIDO_FALSO, mensajes, 0, cwd=str(tmp_path))

    def test_caso_real_corregido_que_sigue_roto(self, tmp_path, monkeypatch):
        pendientes = [p for p in self._caso(tmp_path, monkeypatch, ARREGLO_FALLIDO) if p["tipo"] == "html_no_corregido"]
        assert [p["frase"] for p in pendientes][:1] == ["Ya lo corregí — ahora el código está válido."]
        assert len(pendientes) == 3
        nota = runner.nota_verificacion(pendientes)
        assert "tu cambio no lo arregló" in nota and "línea 1: SyntaxError" in nota
        final = runner.respuesta_verificada(RESPUESTA_CORREGIDO_FALSO, pendientes)
        assert final.startswith(
            "⚠️ Revisé lo que se ejecutó y corregí esta respuesta: «prueba_js.html» no quedó corregido"
        )
        assert "Ya lo corregí" not in final
        assert "ya está bien pero" not in final
        assert "55967e31" in final

    def test_arreglo_real_no_se_marca(self, tmp_path, monkeypatch):
        sano = TERNARIO_ROTO.replace("tieneHijos ? (' data-nombre", "tieneHijos ? ' data-nombre")
        assert [p for p in self._caso(tmp_path, monkeypatch, sano) if p["tipo"] == "html_no_corregido"] == []

    def test_esta_bien_sin_html_del_turno_no_revisa(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "revisar_html_js", lambda _r: pytest.fail("revisó sin HTML"))
        mensajes = [{"role": "user", "content": "¿cómo va?"}]
        assert runner.html_dado_por_corregido("Todo está bien con tu correo.", "", mensajes, 0, str(tmp_path)) == []


class TestSubidaFalsaConEtiqueta:
    RESPUESTA = (
        "## Listo — Explorador de Carpetas Demo\n\nConstruí la app de navegación de carpetas.\n\n"
        "**Entrega:** El archivo ya está subido a tu OneDrive:\n"
        "`Escritorio/Ana/Proyectos/Proyecto Demo/PROYECTO DEMO/Etapa 2 - Estructura/Explorador_Accesos_Apollo.html`"
    )

    def _mensajes(self, listado):
        return [
            {"role": "user", "content": "HAGAMOS LA OPCION 1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "l1", "function": {"name": "sharepoint_list_files"}}],
            },
            {"role": "tool", "tool_call_id": "l1", "content": json.dumps({"success": True, "items": listado})},
        ]

    def test_caso_real_se_detecta(self):
        listado = [{"name": "Filial Sur", "folder": True}, {"name": "Etapa 2 - Estructura", "folder": True}]
        tipos = [p["tipo"] for p in runner.verificar_afirmaciones(self.RESPUESTA, self._mensajes(listado), 0)]
        assert "subida" in tipos

    def test_si_el_listado_muestra_el_archivo_no_se_marca(self):
        listado = [{"name": "Explorador_Accesos_Apollo.html", "size": 60871}]
        tipos = [p["tipo"] for p in runner.verificar_afirmaciones(self.RESPUESTA, self._mensajes(listado), 0)]
        assert "subida" not in tipos

    def test_etiqueta_sola_sigue_siendo_propuesta(self):
        assert runner._PIDE_O_PROPONE.search("Entregable el informe en la carpeta")
        assert not runner._PIDE_O_PROPONE.search("**Entrega:** El archivo ya está subido a tu OneDrive:")
