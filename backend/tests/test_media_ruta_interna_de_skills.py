from hermeshq.services.hermes_runtime import extraccion_interna, ruta_interna_de_skills


def test_ruta_interna_de_skills():
    assert ruta_interna_de_skills("/app/workspaces/agent-x/.hermes/skills/usuario/prueba-w5/SKILL.md") is True
    assert ruta_interna_de_skills("C:\\ws\\.hermes\\skills\\usuario\\a\\SKILL.md") is True


def test_otras_rutas_siguen_avisando():
    assert ruta_interna_de_skills("/app/workspaces/agent-x/outputs/informe.docx") is False
    assert ruta_interna_de_skills("/etc/passwd") is False
    assert ruta_interna_de_skills("") is False
    assert ruta_interna_de_skills(None) is False


def test_la_extraccion_interna_de_datos_no_se_adjunta():
    assert extraccion_interna("work/hse-tablero/datos.json") is True
    assert extraccion_interna("/app/workspaces/agent-x/work/hse/datos_norm.json") is True
    assert extraccion_interna("C:\\ws\\work\\hse\\datos.json") is True
    assert extraccion_interna("outputs/datos.json") is False
    assert extraccion_interna("work/hse/informe.html") is False
    assert extraccion_interna("datos.json") is False
    assert extraccion_interna(None) is False
