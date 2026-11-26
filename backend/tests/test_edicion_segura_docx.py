from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from lxml import etree

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
SCRIPT = _RAIZ / "plugin_templates" / "hermeshq_documentos" / "editar_docx.py"
_PLUGIN = _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSW = f'xmlns:w="{W}"'


def _cargar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


editor = _cargar("editar_docx_mod", SCRIPT)
plugin = _cargar("hermeshq_documentos_edicion", _PLUGIN)

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _documento(cuerpo: str, prologo: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"{prologo}<w:document {NSW}><w:body>{cuerpo}</w:body></w:document>"
    )


def _docx(destino: Path, cuerpo: str = "<w:p><w:r><w:t>texto viejo aqui</w:t></w:r></w:p>", documento=None):
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", documento if documento is not None else _documento(cuerpo))
    return destino


def _correr(tmp: Path, **entrada) -> dict:
    archivo_entrada = tmp / f"entrada-{os.urandom(4).hex()}.json"
    archivo_entrada.write_text(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in entrada.items()}))
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(archivo_entrada)], capture_output=True, text=True, timeout=60
    )
    return json.loads(res.stdout.strip().splitlines()[-1])


def _arbol(archivo: Path):
    with zipfile.ZipFile(archivo) as z:
        return etree.fromstring(z.read("word/document.xml"))


def _runs(archivo: Path) -> list[tuple[str, bool, bool]]:
    salida = []
    for r in _arbol(archivo).iter(f"{{{W}}}r"):
        texto = "".join(t.text or "" for t in r.iter(f"{{{W}}}t"))
        rpr = r.find(f"{{{W}}}rPr")
        negrita = rpr is not None and rpr.find(f"{{{W}}}b") is not None
        cursiva = rpr is not None and rpr.find(f"{{{W}}}i") is not None
        salida.append((texto, negrita, cursiva))
    return salida


def _texto(archivo: Path) -> str:
    return "".join(t.text or "" for t in _arbol(archivo).iter(f"{{{W}}}t"))


def _sha(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


class TestBuscarReemplazarPreservaFormato:
    def test_solo_cambia_el_fragmento_con_la_coincidencia(self, tmp_path):
        src = _docx(
            tmp_path / "in.docx",
            '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Hola </w:t></w:r>'
            '<w:r><w:t xml:space="preserve">mundo viejo</w:t></w:r>'
            '<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve"> fin</w:t></w:r></w:p>',
        )
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["ok"] is True
        assert _runs(out) == [("Hola ", True, False), ("mundo nuevo", False, False), (" fin", False, True)]

    def test_coincidencia_que_cruza_fragmentos_no_arrastra_el_resto(self, tmp_path):
        src = _docx(
            tmp_path / "in.docx",
            "<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Antes </w:t></w:r>"
            "<w:r><w:t>tex</w:t></w:r><w:r><w:t>to vie</w:t></w:r><w:r><w:t>jo</w:t></w:r>"
            "<w:r><w:rPr><w:i/></w:rPr><w:t> despues</w:t></w:r></w:p>",
        )
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["resumen"]["buscar_reemplazar"][0]["reemplazos"] == 1
        assert _texto(out) == "Antes texto nuevo despues"
        runs = _runs(out)
        assert runs[0] == ("Antes ", True, False)
        assert runs[-1] == (" despues", False, True)

    def test_max_se_respeta_exactamente(self, tmp_path):
        src = _docx(tmp_path / "in.docx", "<w:p><w:r><w:t>a a a</w:t></w:r></w:p><w:p><w:r><w:t>a</w:t></w:r></w:p>")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "a", "reemplazar": "b", "max": 2, "todas": True}],
        )
        assert datos["resumen"]["buscar_reemplazar"][0]["reemplazos"] == 2
        assert _texto(out) == "b b aa"

    def test_parrafo_anidado_no_se_reemplaza_dos_veces(self, tmp_path):
        src = _docx(
            tmp_path / "in.docx",
            "<w:p><w:r><w:t>uno </w:t></w:r><w:r><w:pict><w:txbxContent>"
            "<w:p><w:r><w:t>caja viejo</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>",
        )
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "viejo viejo"}],
        )
        assert datos["resumen"]["buscar_reemplazar"][0]["reemplazos"] == 1
        assert _texto(out) == "uno caja viejo viejo"

    def test_sin_coincidencias_se_avisa(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "inexistente", "reemplazar": "x"}],
        )
        assert datos["ok"] is True
        assert datos["sin_cambios"] is True
        assert datos["avisos"]


class TestEntradaHostil:
    def test_operacion_desconocida_no_produce_salida(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path, archivo=src, salida=out, operaciones=[{"op": "listas_a_parrafos"}, {"op": "borrar_todo"}]
        )
        assert datos["ok"] is False
        assert "operacion_desconocida" in datos["error"]
        assert not out.exists()

    def test_xml_con_entidad_externa_se_rechaza_y_no_filtra(self, tmp_path):
        secreto = tmp_path / "secreto.txt"
        secreto.write_text("CLAVE-ULTRA-SECRETA")
        documento = _documento(
            "<w:p><w:r><w:t>&xxe; viejo</w:t></w:r></w:p>",
            prologo=f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file://{secreto}">]>',
        )
        src = _docx(tmp_path / "in.docx", documento=documento)
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["ok"] is False
        assert "xml_con_dtd" in datos["error"]
        assert not out.exists()
        assert "CLAVE-ULTRA-SECRETA" not in json.dumps(datos)

    def test_entidades_internas_masivas_se_rechazan(self, tmp_path):
        documento = _documento(
            "<w:p><w:r><w:t>&b;</w:t></w:r></w:p>",
            prologo='<!DOCTYPE w:document [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
        )
        src = _docx(tmp_path / "in.docx", documento=documento)
        out = tmp_path / "out.docx"
        datos = _correr(tmp_path, archivo=src, salida=out, operaciones=[{"op": "listas_a_parrafos"}])
        assert datos["ok"] is False
        assert not out.exists()

    def test_paquete_descomprimido_desmesurado_se_rechaza(self, tmp_path):
        src = tmp_path / "bomba.docx"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", _documento("<w:p/>"))
            z.writestr("relleno.bin", b"\0" * 4096)
        with (
            patch.object(editor, "MAX_BYTES_DESCOMPRIMIDOS", 2048),
            pytest.raises(editor.DocumentoRechazado, match="descomprimido_demasiado_grande"),
        ):
            editor._leer_paquete(str(src))

    def test_partes_duplicadas_se_rechazan(self, tmp_path):
        src = tmp_path / "dup.docx"
        with pytest.warns(UserWarning), zipfile.ZipFile(src, "w") as z:
            z.writestr("word/document.xml", _documento("<w:p/>"))
            z.writestr("word/document.xml", _documento("<w:p><w:r><w:t>otro</w:t></w:r></w:p>"))
        out = tmp_path / "out.docx"
        datos = _correr(tmp_path, archivo=src, salida=out, operaciones=[{"op": "listas_a_parrafos"}])
        assert datos["ok"] is False
        assert "partes_duplicadas" in datos["error"]

    def test_archivo_que_no_es_zip(self, tmp_path):
        src = tmp_path / "falso.docx"
        src.write_text("no soy un docx")
        datos = _correr(tmp_path, archivo=src, salida=tmp_path / "o.docx", operaciones=[{"op": "listas_a_parrafos"}])
        assert datos["ok"] is False
        assert "no_es_un_docx" in datos["error"]


class TestEscrituraSegura:
    def test_no_sobrescribe_una_salida_existente(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        out.write_bytes(b"version previa del usuario")
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["ok"] is False
        assert datos["error"] == "salida_existe"
        assert out.read_bytes() == b"version previa del usuario"

    def test_nunca_escribe_sobre_el_original(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        antes = src.read_bytes()
        datos = _correr(tmp_path, archivo=src, salida=src, operaciones=[{"op": "listas_a_parrafos"}])
        assert datos["ok"] is False
        assert src.read_bytes() == antes

    def test_huellas_y_sin_temporales(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["huella_origen"] == _sha(src)
        assert datos["huella_salida"] == _sha(out)
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None

    def test_huella_distinta_no_edita(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path, archivo=src, salida=out, huella_origen="0" * 64, operaciones=[{"op": "listas_a_parrafos"}]
        )
        assert datos["ok"] is False
        assert datos["error"] == "documento_cambio"
        assert datos["huella_actual"] == _sha(src)
        assert not out.exists()

    def test_huella_correcta_edita(self, tmp_path):
        src = _docx(tmp_path / "in.docx")
        out = tmp_path / "out.docx"
        datos = _correr(
            tmp_path,
            archivo=src,
            salida=out,
            huella_origen=_sha(src),
            operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}],
        )
        assert datos["ok"] is True


@pytest.fixture
def espacio(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sharepoint_downloads").mkdir(parents=True)
    with patch.dict(os.environ, {"HERMES_SESSION_CWD": str(ws)}):
        yield ws


def _editar(**args) -> dict:
    return json.loads(plugin._editar_docx(args))


REEMPLAZO = [{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}]


class TestPluginConfinamiento:
    def test_ruta_absoluta_fuera_del_espacio(self, espacio, tmp_path):
        ajeno = _docx(tmp_path / "ajeno.docx")
        datos = _editar(archivo=str(ajeno), operaciones=REEMPLAZO)
        assert datos["success"] is False
        assert "fuera del espacio" in datos["error"]
        assert not (tmp_path / "ajeno_editado.docx").exists()

    def test_salto_de_directorio(self, espacio, tmp_path):
        _docx(tmp_path / "ajeno.docx")
        datos = _editar(archivo="../ajeno.docx", operaciones=REEMPLAZO)
        assert datos["success"] is False
        assert not (tmp_path / "ajeno_editado.docx").exists()

    def test_enlace_simbolico_hacia_fuera(self, espacio, tmp_path):
        ajeno = _docx(tmp_path / "ajeno.docx")
        (espacio / "atajo.docx").symlink_to(ajeno)
        datos = _editar(archivo="atajo.docx", operaciones=REEMPLAZO)
        assert datos["success"] is False
        assert not (tmp_path / "ajeno_editado.docx").exists()
        datos = _editar(archivo="atajo", operaciones=REEMPLAZO)
        assert datos["success"] is False

    def test_coincidencia_ambigua_no_elige_por_su_cuenta(self, espacio):
        _docx(espacio / "sharepoint_downloads" / "aaaaaaaa_Politica Accesos.docx")
        _docx(espacio / "sharepoint_downloads" / "bbbbbbbb_Politica Respaldos.docx")
        datos = _editar(archivo="Politica", operaciones=REEMPLAZO)
        assert datos["success"] is False
        assert "varios documentos" in datos["error"]
        assert not list((espacio / "sharepoint_downloads").glob("*_editado*"))

    def test_mismo_documento_bajado_dos_veces_usa_el_mas_reciente(self, espacio):
        viejo = _docx(espacio / "sharepoint_downloads" / "aaaaaaaa_Politica.docx")
        nuevo = _docx(espacio / "sharepoint_downloads" / "bbbbbbbb_Politica.docx")
        os.utime(viejo, (1_000_000, 1_000_000))
        datos = _editar(archivo="Politica", operaciones=REEMPLAZO)
        assert datos["success"] is True
        assert datos["original"] == f"sharepoint_downloads/{nuevo.name}"


class TestPluginVersiones:
    def test_ediciones_sucesivas_no_se_pisan(self, espacio):
        _docx(espacio / "doc.docx")
        primera = _editar(archivo="doc.docx", operaciones=REEMPLAZO)
        contenido_primera = (espacio / primera["salida"]).read_bytes()
        segunda = _editar(
            archivo="doc.docx", operaciones=[{"op": "buscar_reemplazar", "buscar": "aqui", "reemplazar": "alla"}]
        )
        assert primera["salida"] == "doc_editado.docx"
        assert segunda["salida"] == "doc_editado-2.docx"
        assert (espacio / primera["salida"]).read_bytes() == contenido_primera
        assert _texto(espacio / segunda["salida"]) == "texto viejo alla"

    def test_ediciones_concurrentes_producen_versiones_distintas(self, espacio):
        _docx(espacio / "doc.docx")
        resultados = []

        def editar(n):
            resultados.append(
                _editar(
                    archivo="doc.docx",
                    operaciones=[{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": f"v{n}"}],
                )
            )

        hilos = [threading.Thread(target=editar, args=(n,)) for n in range(4)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        assert all(r["success"] for r in resultados), resultados
        salidas = {r["salida"] for r in resultados}
        assert len(salidas) == 4
        textos = {_texto(espacio / s) for s in salidas}
        assert textos == {f"texto v{n} aqui" for n in range(4)}
        assert not list((espacio / "work").glob(".editar_docx_entrada*"))

    def test_huella_obsoleta_bloquea_la_edicion(self, espacio):
        doc = _docx(espacio / "doc.docx")
        huella_leida = _sha(doc)
        _docx(doc, "<w:p><w:r><w:t>otra persona cambio esto viejo</w:t></w:r></w:p>")
        datos = _editar(archivo="doc.docx", operaciones=REEMPLAZO, huella_origen=huella_leida)
        assert datos["success"] is False
        assert "cambió" in datos["error"]
        assert datos["huella_actual"] == _sha(doc)
        assert not (espacio / "doc_editado.docx").exists()

    def test_cadena_de_ediciones_con_huella(self, espacio):
        _docx(espacio / "doc.docx")
        primera = _editar(archivo="doc.docx", operaciones=REEMPLAZO)
        segunda = _editar(
            archivo=primera["salida"],
            operaciones=[{"op": "buscar_reemplazar", "buscar": "aqui", "reemplazar": "alla"}],
            huella_origen=primera["huella_salida"],
        )
        assert segunda["success"] is True
        assert _texto(espacio / segunda["salida"]) == "texto nuevo alla"

    def test_sin_cambios_pide_no_subir(self, espacio):
        _docx(espacio / "doc.docx")
        datos = _editar(
            archivo="doc.docx", operaciones=[{"op": "buscar_reemplazar", "buscar": "zzz", "reemplazar": "x"}]
        )
        assert datos["success"] is False
        assert "No la subas" in datos["error"]
        assert datos["no_aplicadas"] == [{"operacion": 1, "op": "buscar_reemplazar", "buscar": "zzz"}]

    def test_edicion_parcial_no_se_da_por_hecha(self, espacio):
        _docx(espacio / "doc.docx")
        datos = _editar(
            archivo="doc.docx",
            operaciones=[
                {"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"},
                {"op": "buscar_reemplazar", "buscar": "$5.900.000", "reemplazar": "$5.900.000"},
            ],
        )
        assert datos["success"] is False
        assert "INCOMPLETA" in datos["error"]
        assert "$5.900.000" in datos["error"]
        assert datos["no_aplicadas"] == [{"operacion": 2, "op": "buscar_reemplazar", "buscar": "$5.900.000"}]
        assert _texto(espacio / datos["salida"]) == "texto nuevo aqui"
        assert datos["salida"] in datos["siguiente"]

    def test_operacion_desconocida_se_informa(self, espacio):
        _docx(espacio / "doc.docx")
        datos = _editar(archivo="doc.docx", operaciones=[{"op": "reescribir_todo"}])
        assert datos["success"] is False
        assert "operacion_desconocida" in datos["error"]
        assert not (espacio / "doc_editado.docx").exists()
