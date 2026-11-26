from hermeshq.services import archivos_conversacion as ac

NOMBRE = "DOC-MTZ-01_Matriz_Demo.xlsx"
PEDIDO_REAL = (
    f'quiero que en una nueva versión de este archivo "C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana'
    f'\\Proyectos\\IA\\Evaluación Casos de Uso\\{NOMBRE}" se hagan las modificaciones pertinentes para el diseño.'
)


def _workspace(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / NOMBRE).write_bytes(b"x")
    return tmp_path


def test_caso_real_la_ruta_de_onedrive_con_el_archivo_no_recibe_la_nota_del_workspace(tmp_path):
    assert ac.archivos_nombrados(_workspace(tmp_path), PEDIDO_REAL) == []


def test_url_de_sharepoint_con_el_archivo_tampoco(tmp_path):
    pedido = f"Modifica https://contoso.sharepoint.com/personal/x/Documents/Proyectos/{NOMBRE} y deja otra versión."
    assert ac.archivos_nombrados(_workspace(tmp_path), pedido) == []


def test_control_negativo_ruta_de_onedrive_solo_como_destino_si_recibe_la_nota(tmp_path):
    pedido = f"Modifica {NOMBRE} y guárdalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana."
    assert ac.archivos_nombrados(_workspace(tmp_path), pedido) == [f"work/{NOMBRE}"]


def test_control_negativo_nombre_sin_ruta_si_recibe_la_nota(tmp_path):
    assert ac.archivos_nombrados(_workspace(tmp_path), f"Agrega una columna a {NOMBRE}") == [f"work/{NOMBRE}"]


PEDIDO_CON_ADJUNTO = (
    "[Archivos adjuntos, ya disponibles en tu workspace:\n"
    f"- {NOMBRE} (28 KB) -> uploads/4236224a-404c-4a73-86f9-b7bb7e5c8aad.xlsx]\n\n"
    f"vuelve a intentarlo porque el archivo que te pido modificar tiene 3 pestañas {NOMBRE}"
)


def test_caso_real_el_adjunto_del_mismo_nombre_gana_a_la_copia_vieja_del_workspace(tmp_path):
    assert ac.archivos_nombrados(_workspace(tmp_path), PEDIDO_CON_ADJUNTO) == []


def test_control_negativo_adjunto_de_otro_nombre_no_oculta_la_copia(tmp_path):
    pedido = (
        "[Archivos adjuntos, ya disponibles en tu workspace:\n- otro.xlsx (3 KB) -> uploads/x.xlsx]\n\n"
        f"Agrega una columna a {NOMBRE}"
    )
    assert ac.archivos_nombrados(_workspace(tmp_path), pedido) == [f"work/{NOMBRE}"]


def test_nombres_adjuntos_lee_varias_lineas():
    texto = "[Archivos adjuntos, ya disponibles en tu workspace:\n- A b.xlsx (1 KB) -> uploads/1.xlsx\n- c.pdf (2 MB) -> uploads/2.pdf]"
    assert ac._nombres_adjuntos(texto) == {"a b.xlsx", "c.pdf"}
