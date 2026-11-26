from __future__ import annotations

AJUSTES_OBLIGATORIOS = ("m365_agent_token_enabled", "approval_central_enabled", "approval_central_enforced")
TOOLSETS_M365 = frozenset(
    {"hermeshq_ms365_mail", "hermeshq_ms365_calendar", "hermeshq_ms365_teams", "hermeshq_sharepoint"}
)


def no_declarados(ajustes) -> list[str]:
    declarados = set(getattr(ajustes, "model_fields_set", set()) or set())
    return [campo for campo in AJUSTES_OBLIGATORIOS if campo not in declarados]


def usa_m365(agente) -> bool:
    return bool(TOOLSETS_M365 & set(getattr(agente, "enabled_toolsets", None) or []))


def efecto(agente, ajustes) -> dict:
    from hermeshq.services.aprobacion_central import central_activa_para

    return {
        "token_m365": bool(ajustes.m365_agent_token_enabled),
        "aprobacion_central": central_activa_para(agente, ajustes),
    }


def cambios_respecto_a_produccion(agente, ajustes) -> list[str]:
    if not usa_m365(agente):
        return []
    resultado = efecto(agente, ajustes)
    cambios = []
    if not resultado["token_m365"]:
        cambios.append("deja de recibir token M365: sus escrituras solo pueden ir por el broker con aprobación central")
    if resultado["aprobacion_central"]:
        cambios.append("toda escritura M365 (correo, reunión, Teams, compartir) pasa a requerir aprobación")
    return cambios


def diagnostico(ajustes, agentes) -> dict:
    faltan = no_declarados(ajustes)
    filas = []
    for agente in agentes:
        filas.append(
            {
                "agente": getattr(agente, "name", "?"),
                "usa_m365": usa_m365(agente),
                "modo": str(getattr(agente, "approval_central_mode", None) or "heredar"),
                **efecto(agente, ajustes),
                "cambios": cambios_respecto_a_produccion(agente, ajustes),
            }
        )
    afectados = [f["agente"] for f in filas if f["cambios"]]
    return {
        "ajustes": {campo: bool(getattr(ajustes, campo)) for campo in AJUSTES_OBLIGATORIOS},
        "no_declarados": faltan,
        "ambiguo": bool(faltan) and any(f["usa_m365"] for f in filas),
        "agentes": filas,
        "agentes_con_cambios": afectados,
    }
