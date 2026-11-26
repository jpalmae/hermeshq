from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FIRMA_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OFFICE = {
    ".docx": ("word/", "Word"),
    ".xlsx": ("xl/", "Excel"),
    ".xlsm": ("xl/", "Excel"),
    ".pptx": ("ppt/", "PowerPoint"),
}
IMAGENES = {
    ".png": (b"\x89PNG\r\n\x1a\n", "PNG"),
    ".jpg": (b"\xff\xd8\xff", "JPG"),
    ".jpeg": (b"\xff\xd8\xff", "JPG"),
    ".gif": (b"GIF8", "GIF"),
}


@dataclass
class Resultado:
    valido: bool
    extension: str
    motivo: str | None = None
    avisos: list[str] = field(default_factory=list)


def _tipo_office(nombres: list[str]) -> str | None:
    for extension in (".docx", ".xlsx", ".pptx"):
        if any(n.startswith(OFFICE[extension][0]) for n in nombres):
            return extension
    return None


def _por_firma(cabecera: bytes, ruta: Path) -> str:
    if cabecera.startswith(b"%PDF-"):
        return ".pdf"
    for extension, (firma, _nombre) in IMAGENES.items():
        if cabecera.startswith(firma):
            return extension
    if cabecera.startswith(b"PK\x03\x04") and zipfile.is_zipfile(ruta):
        with zipfile.ZipFile(ruta) as z:
            return _tipo_office(z.namelist()) or ".zip"
    return ""


AREA_FONDO = 0.85
AREA_DECORACION = 0.02
TOLERANCIA_BORDE = 0.01
REPETICION_PIE = 0.6
MIN_LAMINAS_PIE = 3
LARGO_PIE = 60
PLACEHOLDERS_DE_PIE = {13, 15, 16}
TIPOS_OBJETO = {7, 10, 16, 21, 24}
TEXTO_DE_PIE = re.compile(
    r"^(?:\d{1,4}|p[aá]gina \d+(?: de \d+)?|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|(?:https?://)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/?)$",
    re.IGNORECASE,
)


def _texto_normal(texto: str) -> str:
    return " ".join(texto.split()).lower()


def _formas(formas):
    for forma in formas:
        if getattr(forma, "shape_type", None) == 6:
            yield from _formas(forma.shapes)
        else:
            yield forma


def _proporciones(forma, ancho: int, alto: int) -> tuple[float, int]:
    try:
        izquierda, arriba = int(forma.left or 0), int(forma.top or 0)
        w, h = int(forma.width or 0), int(forma.height or 0)
    except (TypeError, ValueError):
        return 0.0, 0
    if not ancho or not alto:
        return 0.0, 0
    margen_x, margen_y = ancho * TOLERANCIA_BORDE, alto * TOLERANCIA_BORDE
    bordes = sum(
        (
            izquierda <= margen_x,
            arriba <= margen_y,
            izquierda + w >= ancho - margen_x,
            arriba + h >= alto - margen_y,
        )
    )
    return (w * h) / (ancho * alto), bordes


def _tipo_placeholder(forma) -> int | None:
    if not getattr(forma, "is_placeholder", False):
        return None
    try:
        return int(forma.placeholder_format.type)
    except (AttributeError, ValueError, TypeError):
        return None


def _es_imagen(forma) -> bool:
    return forma._element.tag.endswith("}pic")


def _texto_decorativo(forma, texto: str, pies: set[str]) -> bool:
    if _tipo_placeholder(forma) in PLACEHOLDERS_DE_PIE:
        return True
    normal = _texto_normal(texto)
    return bool(TEXTO_DE_PIE.match(normal)) or normal in pies


def _tiene_contenido(forma, ancho: int, alto: int, pies: set[str]) -> bool:
    if getattr(forma, "has_text_frame", False):
        texto = forma.text_frame.text.strip()
        if texto:
            return not _texto_decorativo(forma, texto, pies)
    if getattr(forma, "has_table", False) or getattr(forma, "has_chart", False):
        return True
    area, bordes = _proporciones(forma, ancho, alto)
    if _es_imagen(forma):
        return AREA_DECORACION <= area < AREA_FONDO and bordes < 2
    try:
        tipo = forma.shape_type
    except (AttributeError, NotImplementedError):
        tipo = None
    if tipo in TIPOS_OBJETO or forma._element.tag.endswith("}graphicFrame"):
        return area >= AREA_DECORACION
    return False


def _pies_repetidos(laminas) -> set[str]:
    if len(laminas) < MIN_LAMINAS_PIE:
        return set()
    conteo: dict[str, int] = {}
    for lamina in laminas:
        vistos = {
            _texto_normal(f.text_frame.text)
            for f in _formas(lamina.shapes)
            if getattr(f, "has_text_frame", False) and 0 < len(f.text_frame.text.strip()) <= LARGO_PIE
        }
        for texto in vistos:
            conteo[texto] = conteo.get(texto, 0) + 1
    return {t for t, n in conteo.items() if n >= max(MIN_LAMINAS_PIE, REPETICION_PIE * len(laminas))}


def _laminas_vacias(ruta: Path) -> list[int]:
    try:
        from pptx import Presentation
    except ImportError:
        return []
    presentacion = Presentation(str(ruta))
    ancho, alto = int(presentacion.slide_width or 0), int(presentacion.slide_height or 0)
    laminas = list(presentacion.slides)
    pies = _pies_repetidos(laminas)
    return [
        numero
        for numero, lamina in enumerate(laminas, start=1)
        if not any(_tiene_contenido(f, ancho, alto, pies) for f in _formas(lamina.shapes))
    ]


def _enumerar(numeros: list[int]) -> str:
    textos = [str(n) for n in numeros]
    return textos[0] if len(textos) == 1 else f"{', '.join(textos[:-1])} y {textos[-1]}"


HTML = {".html", ".htm"}
MAX_HTML = 40 * 1024 * 1024
_SCRIPT_ABRE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CIERRA = re.compile(r"</script(?=[\s/>])", re.IGNORECASE)
_SCRIPT_ABRE_SIN_ATRIB = re.compile(r"<script(?=[\s/>])", re.IGNORECASE)
_NO_EJECUTABLE = re.compile(
    r"type\s*=\s*[\"']?(?!(?:text|application)/(?:x-)?(?:java|ecma)script|module)[\w/+.-]", re.IGNORECASE
)
_BLOQUES_TEXTO = re.compile(r"<(style|pre|code|textarea|xmp|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMENTARIO_HTML = re.compile(r"<!--.*?-->", re.DOTALL)
_ETIQUETA = re.compile(r"<[^>]*>")
_FIRMA_JS = re.compile(
    r"\bfunction\s+[A-Za-z_$][\w$]*\s*\([^)]*\)\s*\{"
    r"|\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*[^;]{0,200};"
    r"|\bdocument\.(?:getElementById|querySelector(?:All)?|createElement)\s*\("
    r"|=>\s*\{"
)


def _fin_de_script(html: str, inicio: int) -> tuple[int, int]:
    n = len(html)
    estado = "datos"
    i = inicio
    while i < n:
        cierre = _SCRIPT_CIERRA.search(html, i)
        fin_cierre = (html.find(">", cierre.end()) + 1 or n) if cierre else n
        if estado == "datos":
            comentario = html.find("<!--", i)
            if comentario != -1 and (cierre is None or comentario < cierre.start()):
                estado, i = "escapado", comentario + 4
                continue
            return (cierre.start(), fin_cierre) if cierre else (n, n)
        guion = html.find("-->", i)
        if estado == "escapado":
            abre = _SCRIPT_ABRE_SIN_ATRIB.search(html, i)
            candidatos = [
                (cierre.start() if cierre else n, "cierre"),
                (guion if guion != -1 else n, "guion"),
                (abre.start() if abre else n, "abre"),
            ]
            posicion, tipo = min(candidatos)
            if posicion >= n:
                return n, n
            if tipo == "cierre":
                return cierre.start(), fin_cierre
            if tipo == "guion":
                estado, i = "datos", posicion + 3
            else:
                estado, i = "doble", abre.end()
            continue
        if guion != -1 and (cierre is None or guion < cierre.start()):
            estado, i = "datos", guion + 3
        elif cierre is not None:
            estado, i = "escapado", cierre.end()
        else:
            return n, n
    return n, n


def _revisar_html(texto: str) -> str | None:
    fuera: list[tuple[int, str]] = []
    posicion = 0
    while True:
        apertura = _SCRIPT_ABRE.search(texto, posicion)
        if apertura is None:
            fuera.append((posicion, texto[posicion:]))
            break
        fuera.append((posicion, texto[posicion : apertura.start()]))
        fin_cuerpo, despues = _fin_de_script(texto, apertura.end())
        if fin_cuerpo >= len(texto) and not _NO_EJECUTABLE.search(apertura.group(0)):
            linea = texto.count("\n", 0, apertura.start()) + 1
            return f"el HTML está incompleto: el <script> de la línea {linea} no se cierra"
        posicion = despues
    for inicio, trozo in fuera:
        visible = _ETIQUETA.sub(" ", _COMENTARIO_HTML.sub(" ", _BLOQUES_TEXTO.sub(" ", trozo)))
        hallazgo = _FIRMA_JS.search(visible)
        if hallazgo:
            fragmento = hallazgo.group(0)
            indice = trozo.find(fragmento.split("(")[0].split("=")[0].strip())
            linea = texto.count("\n", 0, inicio + max(indice, 0)) + 1
            return (
                f"el HTML muestra código JavaScript como texto (desde la línea {linea}: «{fragmento[:60]}»); "
                "suele ser un '</script>' dentro de un texto de JavaScript, que debe escribirse '<\\/script>'"
            )
    return None


def _hojas_sin_datos(ruta: Path) -> tuple[list[str], list[str]]:
    from openpyxl import load_workbook

    libro = load_workbook(str(ruta), read_only=True, data_only=False)
    con, sin = [], []
    try:
        for ws in libro.worksheets:
            hay = False
            for fila in ws.iter_rows(max_row=500, values_only=True):
                if any(v not in (None, "") for v in fila):
                    hay = True
                    break
            (con if hay else sin).append(ws.title)
    finally:
        libro.close()
    return con, sin


def _docx_sin_texto(ruta: Path) -> bool:
    from docx import Document

    doc = Document(str(ruta))
    if any(p.text.strip() for p in doc.paragraphs):
        return False
    return not any(c.text.strip() for t in doc.tables for fila in t.rows for c in fila.cells)


def _laminas_con_partes_compartidas(ruta: Path) -> list[int]:
    import posixpath
    import re

    with zipfile.ZipFile(ruta) as z:
        nombres = set(z.namelist())
        presentacion = z.read("ppt/presentation.xml").decode("utf-8", "replace")
        rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8", "replace")
        destino = {
            m.group(1): m.group(2)
            for m in re.finditer(r'<Relationship\b[^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)
        }
        destino.update(
            {
                m.group(2): m.group(1)
                for m in re.finditer(r'<Relationship\b[^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)
            }
        )
        orden = []
        for rid in re.findall(r'<p:sldId\b[^>]*?r:id="([^"]+)"', presentacion):
            objetivo = destino.get(rid)
            if objetivo:
                orden.append(posixpath.normpath(posixpath.join("ppt", objetivo)))
        usadas: dict[str, int] = {}
        repetidas = []
        for numero, lamina in enumerate(orden, start=1):
            ruta_rels = posixpath.join(posixpath.dirname(lamina), "_rels", posixpath.basename(lamina) + ".rels")
            if ruta_rels not in nombres:
                continue
            xml = z.read(ruta_rels).decode("utf-8", "replace")
            compartida = False
            for m in re.finditer(r"<Relationship\b[^>]*>", xml):
                etiqueta = m.group(0)
                tipo = re.search(r'Type="([^"]+)"', etiqueta)
                objetivo = re.search(r'Target="([^"]+)"', etiqueta)
                if not tipo or not objetivo or 'TargetMode="External"' in etiqueta:
                    continue
                if not tipo.group(1).endswith(("/oleObject", "/package", "/tags")):
                    continue
                parte = posixpath.normpath(posixpath.join(posixpath.dirname(lamina), objetivo.group(1)))
                if parte in usadas and usadas[parte] != numero:
                    compartida = True
                usadas.setdefault(parte, numero)
            if compartida:
                repetidas.append(numero)
        return repetidas


def _laminas_con_texto_fuera(ruta: Path) -> list[int]:
    try:
        from pptx import Presentation
    except ImportError:
        return []
    presentacion = Presentation(str(ruta))
    ancho, alto = int(presentacion.slide_width or 0), int(presentacion.slide_height or 0)
    if not ancho or not alto:
        return []
    fuera = []
    for numero, lamina in enumerate(presentacion.slides, start=1):
        for forma in _formas(lamina.shapes):
            try:
                if not (forma.has_text_frame and forma.text_frame.text.strip()):
                    continue
                izq, arr, an, al = forma.left, forma.top, forma.width, forma.height
                if None in (izq, arr, an, al) or an <= 0 or al <= 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            if izq >= ancho or arr >= alto or izq + an <= 0 or arr + al <= 0:
                continue
            sobra_x = max(0, -izq) + max(0, izq + an - ancho)
            sobra_y = max(0, -arr) + max(0, arr + al - alto)
            if sobra_x > 0.15 * an or sobra_y > 0.15 * al:
                fuera.append(numero)
                break
    return fuera


def validar(ruta: Path) -> Resultado:
    ruta = Path(ruta)
    extension = ruta.suffix.lower()
    try:
        tamano = ruta.stat().st_size
        with ruta.open("rb") as archivo:
            cabecera = archivo.read(16)
    except OSError as exc:
        return Resultado(False, extension, f"no se pudo leer ({exc})")
    if tamano == 0:
        return Resultado(False, extension, "el archivo está vacío")
    if not extension:
        detectada = _por_firma(cabecera, ruta)
        if not detectada:
            return Resultado(False, "", "no tiene extensión y no se reconoce su tipo")
        extension = detectada
    if extension == ".pdf":
        if not cabecera.startswith(b"%PDF-"):
            return Resultado(False, extension, "no es un PDF (el contenido es de otro tipo)")
        with ruta.open("rb") as archivo:
            archivo.seek(max(0, tamano - 2048))
            if b"%%EOF" not in archivo.read():
                return Resultado(False, extension, "el PDF está incompleto (se cortó al generarse)")
        return Resultado(True, extension)
    if extension in IMAGENES:
        firma, nombre = IMAGENES[extension]
        if not cabecera.startswith(firma):
            return Resultado(False, extension, f"no es una imagen {nombre}")
        return Resultado(True, extension)
    if extension in OFFICE or extension == ".zip":
        if cabecera.startswith(FIRMA_CFB) and extension in OFFICE:
            return Resultado(
                True,
                extension,
                avisos=["El archivo está protegido con contraseña: el usuario la necesitará para abrirlo."],
            )
        if not cabecera.startswith(b"PK"):
            return Resultado(
                False,
                extension,
                "no es un archivo de Office válido (el contenido es de otro tipo)"
                if extension in OFFICE
                else "no es un ZIP",
            )
        try:
            with zipfile.ZipFile(ruta) as z:
                if z.testzip() is not None:
                    return Resultado(False, extension, "el archivo está dañado (ZIP con errores)")
                nombres = z.namelist()
        except (zipfile.BadZipFile, OSError, EOFError):
            return Resultado(False, extension, "el archivo está dañado (no se puede abrir)")
        if extension == ".zip":
            return Resultado(True, extension)
        prefijo, nombre = OFFICE[extension]
        if "[Content_Types].xml" not in nombres:
            return Resultado(False, extension, "no es un archivo de Office válido (falta su estructura)")
        if not any(n.startswith(prefijo) for n in nombres):
            real = _tipo_office(nombres)
            otro = OFFICE[real][1] if real else "otro formato"
            return Resultado(False, extension, f"dice ser {nombre} pero es un archivo de {otro}")
        avisos = []
        if extension == ".xlsx":
            try:
                con, sin = _hojas_sin_datos(ruta)
            except Exception:  # noqa: BLE001
                con, sin = [None], []
            if not con:
                return Resultado(False, extension, "el Excel no tiene datos (todas sus hojas están vacías)")
            if sin:
                avisos.append(
                    f"Ojo: la hoja {', '.join(sin)} está vacía."
                    if len(sin) == 1
                    else f"Ojo: las hojas {', '.join(sin)} están vacías."
                )
        if extension == ".docx":
            try:
                if _docx_sin_texto(ruta):
                    return Resultado(False, extension, "el Word no tiene texto (está vacío)")
            except Exception:  # noqa: BLE001
                pass
        if extension == ".pptx":
            try:
                compartidas = _laminas_con_partes_compartidas(ruta)
            except Exception:  # noqa: BLE001
                compartidas = []
            if compartidas:
                return Resultado(
                    False,
                    extension,
                    f"las láminas {_enumerar(compartidas)} comparten objetos incrustados con otra lámina y "
                    "PowerPoint las mostraría en blanco al abrirlo o pediría repararlo",
                )
            try:
                fuera = _laminas_con_texto_fuera(ruta)
            except Exception:  # noqa: BLE001
                fuera = []
            if fuera:
                palabra = "la lámina" if len(fuera) == 1 else "las láminas"
                avisos.append(
                    f"Ojo: en {palabra} {_enumerar(fuera)} hay texto cortado por el borde de la lámina; "
                    "revisa que ninguna caja se salga."
                )
            vacias = _laminas_vacias(ruta)
            if vacias:
                verbo = "está vacía" if len(vacias) == 1 else "están vacías"
                palabra = "la lámina" if len(vacias) == 1 else "las láminas"
                avisos.append(f"Ojo: {palabra} {_enumerar(vacias)} {verbo}.")
        return Resultado(True, extension, avisos=avisos)
    if extension in HTML and tamano <= MAX_HTML:
        try:
            motivo = _revisar_html(ruta.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return Resultado(False, extension, f"no se pudo leer ({exc})")
        if motivo:
            return Resultado(False, extension, motivo)
    return Resultado(True, extension)
