import json

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: HTML interactivo.]\nRol: ingeniero de datos y diseñador UX. Usa la skill "
    "informes-interactivos HTML para crear un informe ejecutivo sobre la estructura del Servidor de Informes Demo. Explorador "
    "de carpetas (reutilizable en pestañas 1 y 2): navegación por clic con breadcrumb y botón Volver. Pestaña 3: "
    "visualización interactiva (tarjetas, matriz de permisos y simulador que muestra qué ve cada rol, con candado en lo "
    "bloqueado). Matriz con resaltado al pasar el cursor."
)
PEDIDO_INFORME = (
    "Hazme un informe interactivo en HTML de las ventas del trimestre, con una tabla por sucursal y un diagrama de "
    "carpetas de cómo guardamos los archivos."
)
RESPUESTA_FALSA = (
    "Listo, el HTML quedó en tu OneDrive.\n"
    "- **Explorador de carpetas** con navegación por clic, breadcrumb y botón Volver.\n"
    "- **Matriz de permisos** con resaltado al pasar el cursor.\n"
    "- **Simulador** de roles con candado en lo bloqueado.\n"
    "MEDIA:outputs/app.html"
)
HTML_ESTATICO = "<html><body><h1>Estructura</h1><div class='carpetas'>SUC1 SUC2 SUC3</div></body></html>"
HTML_COMPLETO = (
    "<html><body><nav id='breadcrumb'></nav><button>Volver</button><section id='simulador'></section>"
    "<table id='matriz'></table><span>🔒</span><script>document.title='x'</script></body></html>"
)


def _turno(mensaje, *llamadas):
    salida = [{"role": "user", "content": mensaje}]
    for i, (nombre, argumentos) in enumerate(llamadas):
        salida.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "function": {"name": nombre, "arguments": json.dumps(argumentos)}}],
            }
        )
        salida.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps({"success": True})})
    return salida


class TestFuncionesPrometidas:
    def _caso(self, tmp_path, html, respuesta):
        (tmp_path / "outputs").mkdir(exist_ok=True)
        (tmp_path / "outputs" / "app.html").write_text(html, encoding="utf-8")
        mensajes = _turno(PEDIDO, ("write_file", {"path": "outputs/app.html"}))
        sin_media = "\n".join(linea for linea in respuesta.split("\n") if not linea.startswith("MEDIA:"))
        return runner.funciones_prometidas_sin_html(sin_media, respuesta, mensajes, 0, str(tmp_path))

    def test_caso_real_promete_lo_que_el_archivo_no_tiene(self, tmp_path):
        pendientes = self._caso(tmp_path, HTML_ESTATICO, RESPUESTA_FALSA)
        assert len(pendientes) == 1 and pendientes[0]["tipo"] == "funciones_inexistentes"
        for esperado in ("breadcrumb", "simulador", "matriz de permisos", "candado", "botón Volver"):
            assert esperado in pendientes[0]["funciones"]
        final = runner.respuesta_verificada(RESPUESTA_FALSA, pendientes)
        assert "Simulador" not in final and "breadcrumb" not in final.split("\n\n", 1)[-1]
        assert "MEDIA:outputs/app.html" in final and "no contiene estas funciones" in final

    def test_nota_pide_agregarlas_o_quitarlas(self):
        nota = runner.nota_verificacion([{"tipo": "funciones_inexistentes", "frase": "simulador"}])
        assert "no aparecen en el archivo" in nota and "quítalas" in nota

    def test_control_negativo_el_html_las_tiene(self, tmp_path):
        assert self._caso(tmp_path, HTML_COMPLETO, RESPUESTA_FALSA) == []

    def test_control_negativo_reconoce_que_no_las_incluye(self, tmp_path):
        respuesta = "Listo.\nNo incluye simulador ni candado; la matriz tampoco quedó.\nMEDIA:outputs/app.html"
        assert self._caso(tmp_path, HTML_ESTATICO, respuesta) == []

    def test_control_negativo_sin_html_no_revisa(self, tmp_path):
        mensajes = _turno("hola")
        assert runner.funciones_prometidas_sin_html(RESPUESTA_FALSA, RESPUESTA_FALSA, mensajes, 0, str(tmp_path)) == []
