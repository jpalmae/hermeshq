from __future__ import annotations

import json
import re
import unicodedata

META = "hq_herramientas"
NUCLEO = ("read_file", "search_files", "write_file", "patch", "todo", "skills_list", "skill_view", "clarify", META)
MAX_RESULTADOS = 8
_PALABRAS_VACIAS = {
    "de",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "y",
    "o",
    "en",
    "para",
    "por",
    "con",
    "que",
    "del",
    "al",
    "a",
    "mi",
    "me",
    "se",
    "lo",
    "su",
    "sus",
    "the",
    "to",
    "of",
    "and",
    "or",
    "for",
    "in",
    "on",
    "with",
}

ESQUEMA_META = {
    "type": "function",
    "function": {
        "name": META,
        "description": (
            "Busca y activa herramientas que no están en tu lista actual. Tu lista se reduce a lo que este pedido "
            "parece necesitar; tienes muchas más (correo, calendario, Teams, SharePoint, Excel, Word, PowerPoint, PDF, "
            "informes, imágenes, audio, tareas programadas, otros agentes, código). Si necesitas algo que no ves, "
            "llama esta herramienta con 'buscar' (qué quieres hacer) y luego usa las que devuelve: quedan activas en "
            "esta tarea. No digas que no puedes hacer algo sin buscar antes aquí."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "buscar": {
                    "type": "string",
                    "description": "Qué necesitas hacer, en pocas palabras (ej. 'enviar correo con adjunto').",
                },
                "activar": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Nombres exactos de herramientas a activar (si ya los conoces).",
                },
            },
        },
    },
}


def _normal(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return sin.lower()


def _terminos(texto: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{3,}", _normal(texto)) if t not in _PALABRAS_VACIAS}


def nombre(herramienta: dict) -> str:
    return str(((herramienta or {}).get("function") or {}).get("name") or "")


def usadas_en_historial(historial: list | None) -> set[str]:
    usadas = set()
    for mensaje in historial or []:
        if not isinstance(mensaje, dict):
            continue
        for llamada in mensaje.get("tool_calls") or []:
            fn = (llamada or {}).get("function") or {}
            if fn.get("name"):
                usadas.add(str(fn["name"]))
        if mensaje.get("role") == "tool" and mensaje.get("name"):
            usadas.add(str(mensaje["name"]))
    return usadas


def expuestas(
    catalogo: list[dict],
    *,
    toolset_de: dict[str, str],
    toolsets_pedido: list[str] | None,
    herramientas_pedido: list[str] | None = None,
    usadas: set[str] | None = None,
    activadas: set[str] | None = None,
) -> set[str]:
    disponibles = {nombre(h) for h in catalogo}
    if toolsets_pedido is None:
        return disponibles | {META}
    pedidos = set(toolsets_pedido)
    elegidas = {n for n in disponibles if toolset_de.get(n) in pedidos}
    elegidas |= set(herramientas_pedido or ()) & disponibles
    elegidas |= (usadas or set()) & disponibles
    elegidas |= (activadas or set()) & disponibles
    elegidas |= set(NUCLEO) & (disponibles | {META})
    return elegidas | {META}


def filtrar(herramientas: list[dict], visibles: set[str]) -> list[dict]:
    salida = [h for h in herramientas if nombre(h) in visibles]
    if META in visibles and not any(nombre(h) == META for h in salida):
        salida.append(ESQUEMA_META)
    return salida


def buscar(
    catalogo: list[dict], consulta: str, *, toolset_de: dict[str, str] | None = None, limite: int = MAX_RESULTADOS
) -> list[dict]:
    pedido = _terminos(consulta)
    if not pedido:
        return []
    puntuadas = []
    for herramienta in catalogo:
        n = nombre(herramienta)
        if not n or n == META:
            continue
        fn = herramienta.get("function") or {}
        en_nombre = _terminos(n.replace("_", " ") + " " + (toolset_de or {}).get(n, "").replace("_", " "))
        en_texto = _terminos(str(fn.get("description") or ""))
        puntos = 3 * len(pedido & en_nombre) + len(pedido & en_texto)
        puntos += sum(1 for p in pedido for t in en_nombre | en_texto if len(p) >= 5 and t.startswith(p[:5]) and t != p)
        if puntos:
            puntuadas.append((puntos, n, fn))
    puntuadas.sort(key=lambda x: (-x[0], x[1]))
    return [
        {"nombre": n, "para_que": str(fn.get("description") or "").split(". ")[0][:220]}
        for _p, n, fn in puntuadas[:limite]
    ]


def responder_meta(
    args: dict, catalogo: list[dict], activadas: set[str], *, toolset_de: dict[str, str] | None = None
) -> str:
    disponibles = {nombre(h) for h in catalogo}
    pedidas = [str(x) for x in (args.get("activar") or []) if str(x)]
    nuevas = [n for n in pedidas if n in disponibles]
    desconocidas = [n for n in pedidas if n not in disponibles]
    encontradas = buscar(catalogo, str(args.get("buscar") or ""), toolset_de=toolset_de) if args.get("buscar") else []
    for item in encontradas:
        nuevas.append(item["nombre"])
    activadas.update(nuevas)
    resultado: dict = {"success": True, "activadas": sorted(set(nuevas))}
    if encontradas:
        resultado["encontradas"] = encontradas
    if desconocidas:
        resultado["no_existen"] = desconocidas
    if not nuevas:
        resultado["success"] = False
        resultado["error"] = "No encontré herramientas para eso. Prueba con otras palabras o dile al usuario qué falta."
    else:
        resultado["siguiente"] = "Ya puedes llamar estas herramientas en esta misma tarea."
    return json.dumps(resultado, ensure_ascii=False)


def activar_diferida(agente, herramienta: str, diferidos, registro, estado: dict | None = None) -> str | None:
    if not herramienta or not diferidos:
        return None
    entrada = registro.get_entry(herramienta)
    if entrada is None or getattr(entrada, "toolset", None) not in set(diferidos):
        return None
    definiciones = registro.get_definitions({herramienta}, quiet=True)
    if not definiciones:
        return None
    if herramienta not in agente.valid_tool_names:
        agente.tools = [*(agente.tools or []), definiciones[0]]
        agente.valid_tool_names.add(herramienta)
    if estado is not None:
        if herramienta not in {nombre(h) for h in estado["catalogo"]}:
            estado["catalogo"].append(definiciones[0])
        estado["toolset_de"][herramienta] = entrada.toolset
        estado["activadas"].add(herramienta)
    return herramienta
