import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

LIMITE_CUERPO = 200_000


class CorreoIlegible(Exception):
    pass


class _SoloTexto(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._omitir = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._omitir += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"):
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._omitir:
            self._omitir -= 1

    def handle_data(self, data):
        if not self._omitir:
            self.partes.append(data)


def _html_a_texto(html: str) -> str:
    parser = _SoloTexto()
    parser.feed(html)
    texto = "".join(parser.partes)
    texto = re.sub(r"[ \t\r\f\v]+", " ", texto)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", texto).strip()


def _cuerpo(mensaje: EmailMessage) -> str:
    parte = mensaje.get_body(preferencelist=("plain", "html"))
    if parte is None:
        return ""
    try:
        contenido = parte.get_content()
    except (LookupError, UnicodeError):
        return ""
    if parte.get_content_type() == "text/html":
        return _html_a_texto(contenido)
    return str(contenido).strip()


def _adjuntos(mensaje: EmailMessage) -> list[str]:
    lista = []
    for parte in mensaje.iter_attachments():
        nombre = parte.get_filename() or "(sin nombre)"
        try:
            tamano = len(parte.get_payload(decode=True) or b"")
        except Exception:
            tamano = 0
        lista.append(f"{nombre} ({parte.get_content_type()}, {tamano // 1024 or 1} KB)")
    return lista


def eml_a_texto(datos: bytes) -> str:
    try:
        mensaje = BytesParser(policy=policy.default).parsebytes(datos)
    except Exception as exc:
        raise CorreoIlegible(str(exc))
    if not any(mensaje.get(c) for c in ("From", "To", "Subject", "Date")) and not mensaje.get_body():
        raise CorreoIlegible("no tiene cabeceras ni cuerpo de un correo")
    lineas = []
    for etiqueta, clave in (("De", "From"), ("Para", "To"), ("Cc", "Cc"), ("Fecha", "Date"), ("Asunto", "Subject")):
        valor = str(mensaje.get(clave) or "").strip()
        if valor:
            lineas.append(f"{etiqueta}: {valor}")
    cuerpo = _cuerpo(mensaje)
    if len(cuerpo) > LIMITE_CUERPO:
        cuerpo = cuerpo[:LIMITE_CUERPO] + "\n[cuerpo recortado]"
    adjuntos = _adjuntos(mensaje)
    texto = "\n".join(lineas) + "\n\n" + (cuerpo or "(sin cuerpo de texto)")
    if adjuntos:
        texto += "\n\nAdjuntos del correo (no se abren aquí): " + "; ".join(adjuntos)
    return texto + "\n"
