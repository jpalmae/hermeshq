import re

REFERENCIA_PREFIX = re.compile(r"^\[Respuesta corta a tu último mensaje: .*?\]\n", re.DOTALL)


def pedido_original(texto: str) -> str:
    return REFERENCIA_PREFIX.sub("", texto or "", count=1)
