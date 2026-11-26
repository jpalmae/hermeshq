import importlib.util
import json
from pathlib import Path

import pytest
from test_sharepoint_read_file import _docx_bytes, _pptx_bytes, _xlsx_bytes

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos"


def _pdf(paginas: list[str]) -> bytes:
    weasyprint = pytest.importorskip("weasyprint")
    pytest.importorskip("pypdf")
    html = "".join(f'<p style="page-break-after: always">{texto}</p>' for texto in paginas)
    return weasyprint.HTML(string=html).write_pdf()


@pytest.fixture
def leer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_documentos_lector_test", PLUGIN / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    def _leer(**args):
        return json.loads(modulo._leer_documento(args))

    (tmp_path / "uploads").mkdir()
    _leer.modulo = modulo
    return tmp_path, _leer


class TestLectura:
    def test_pdf_por_paginas(self, leer):
        tmp, _leer = leer
        (tmp / "uploads" / "contrato.pdf").write_bytes(_pdf(["Cláusula primera", "Cláusula segunda", "Anexo"]))
        r = _leer(path="uploads/contrato.pdf")
        assert r["success"] is True and r["tipo"] == "pdf" and r["paginas"] == 3, r
        assert "Cláusula primera" in r["texto"] and "Anexo" in r["texto"]
        parcial = _leer(path="uploads/contrato.pdf", desde_pagina=2, hasta_pagina=2)
        assert "Cláusula segunda" in parcial["texto"] and "primera" not in parcial["texto"]

    def test_word_excel_powerpoint_y_texto(self, leer):
        tmp, _leer = leer
        (tmp / "uploads" / "a.docx").write_bytes(_docx_bytes())
        (tmp / "uploads" / "b.xlsx").write_bytes(_xlsx_bytes())
        (tmp / "uploads" / "c.pptx").write_bytes(_pptx_bytes())
        (tmp / "uploads" / "d.csv").write_text("zona,monto\nNorte,10\n", encoding="utf-8")
        assert "Consentimiento informado" in _leer(path="uploads/a.docx")["texto"]
        assert "## Hoja: Datos" in _leer(path="uploads/b.xlsx")["texto"]
        assert "Titulo uno" in _leer(path="uploads/c.pptx")["texto"]
        assert "Norte,10" in _leer(path="uploads/d.csv")["texto"]

    def test_texto_largo_se_corta_y_dice_como_seguir(self, leer):
        tmp, _leer = leer
        (tmp / "uploads" / "largo.txt").write_text("x" * 50_000, encoding="utf-8")
        r = _leer(path="uploads/largo.txt", max_caracteres=1000)
        assert r["success"] is True and len(r["texto"]) == 1000 and r["truncado"] is True
        assert "desde_pagina" in r["siguiente"]


class TestRechazos:
    def test_fuera_del_espacio_de_trabajo(self, leer):
        _, _leer = leer
        r = _leer(path="/etc/passwd")
        assert r["success"] is False and "fuera" in r["error"]

    def test_inexistente_formato_no_soportado_y_pdf_escaneado(self, leer):
        tmp, _leer = leer
        assert _leer(path="uploads/no.pdf")["success"] is False
        (tmp / "uploads" / "x.bin").write_bytes(b"\x00\x01")
        assert "no soportado" in _leer(path="uploads/x.bin")["error"]
        (tmp / "uploads" / "escaneo.pdf").write_bytes(_pdf([""]))
        r = _leer(path="uploads/escaneo.pdf")
        assert r["success"] is False and "escaneo" in r["error"]

    def test_archivo_demasiado_grande(self, leer, monkeypatch):
        tmp, _leer = leer
        (tmp / "uploads" / "grande.txt").write_bytes(b"a" * 2048)
        monkeypatch.setattr(_leer.modulo, "LIMITE_LECTURA_BYTES", 1024)
        r = _leer(path="uploads/grande.txt")
        assert r["success"] is False and "MB" in r["error"]


def test_esta_registrada_en_el_toolset_y_en_el_manifiesto():
    yaml = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    assert "  - hq_leer_documento" in yaml
    assert 'name="hq_leer_documento"' in (PLUGIN / "__init__.py").read_text(encoding="utf-8")


class TestPdfEscaneado:
    def test_caso_real_escaneo_queda_como_imagenes_para_leer_con_vision(self, leer):
        fitz = pytest.importorskip("fitz")
        tmp, _leer = leer
        documento = fitz.open()
        for _ in range(8):
            documento.new_page()
        (tmp / "uploads" / "SUC-P-065.pdf").write_bytes(documento.tobytes())
        r = _leer(path="uploads/SUC-P-065.pdf")
        assert r["success"] is False and r["escaneado"] is True and r["paginas"] == 8
        assert len(r["imagenes"]) == 6 and all(Path(i).is_file() for i in r["imagenes"])
        assert "vision_analyze" in r["siguiente"] and "desde_pagina=7" in r["siguiente"]
        assert "No uses otro archivo como fuente sin decírselo" in r["siguiente"]
