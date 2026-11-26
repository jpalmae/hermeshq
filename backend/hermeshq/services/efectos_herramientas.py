EVENTO_INICIO_HERRAMIENTA = "task.tool_started"
EVENTO_FIN_HERRAMIENTA = "task.tool_finished"
EVENTO_ACTIVACION_HERRAMIENTA = "task.tool_activated"

SIN_EFECTOS_FUERA_DEL_ESPACIO = frozenset(
    {
        "ms365_mail_list",
        "ms365_mail_search",
        "ms365_mail_get",
        "ms365_mail_get_many",
        "ms365_mail_folders",
        "ms365_mail_attachments",
        "ms365_find_person",
        "ms365_calendar_list_events",
        "ms365_calendar_get_event",
        "sharepoint_search",
        "sharepoint_list_files",
        "sharepoint_list_drives",
        "sharepoint_get_file",
        "sharepoint_read_file",
        "sharepoint_download_file",
        "sharepoint_download_folder",
        "teams_list_chats",
        "teams_list_teams",
        "teams_get_chat_messages",
        "web_search",
        "web_fetch",
        "skill_view",
        "skills_list",
        "hq_skills_ver",
        "hq_herramientas",
        "hq_schedule_list",
        "hq_schedule_runs",
        "hq_trabajo_listar",
        "hq_trabajo_estado",
        "hq_list_agents",
        "session_search",
        "vision_analyze",
        "todo",
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "execute_code",
        "terminal",
        "hq_leer_varios",
        "hq_buscar_varios",
        "hq_leer_documento",
        "hq_editar_varios",
        "hq_html_capturar",
        "hq_xlsx_generate",
        "hq_xlsx_editar",
        "hq_pdf_generate",
        "hq_docx_generate",
        "hq_docx_editar",
        "hq_pptx_generate",
        "hq_pptx_from_template",
    }
)


def efectos_posibles(tool_events: int, iniciadas: list[str] | None) -> bool:
    nombres = [str(n or "") for n in (iniciadas or [])]
    if any(n not in SIN_EFECTOS_FUERA_DEL_ESPACIO for n in nombres):
        return True
    return int(tool_events or 0) > len(nombres)
