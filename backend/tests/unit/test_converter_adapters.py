from __future__ import annotations

import hashlib
import html
import io
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from backend.converters.external_protocol import (
    ExternalProcessConverter,
    _run_bounded,
    conversion_result_from_payload,
)
from backend.converters.hdf5_adapter import Hdf5Converter
from backend.converters.image_adapter import ImageMetadataConverter
from backend.converters.ooxml_adapter import OOXMLConverter, _xlsx_cell_column
from backend.converters.parquet_adapter import ParquetConverter
from backend.converters.pdf_adapter import PdfTextConverter
from backend.spi.conversion import (
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConverterDescriptor,
)
from backend.worker.converter_host import _conversion_request


@dataclass
class MemoryBlob:
    content: bytes
    copied_count: int | None = None

    @property
    def byte_size(self) -> int:
        return len(self.content)

    def open_binary(self):
        return io.BytesIO(self.content)

    def copy_to(self, destination: Path) -> int:
        destination.write_bytes(self.content)
        return len(self.content) if self.copied_count is None else self.copied_count


def request(
    content: bytes,
    format_id: str,
    media_type: str,
    *,
    limits: ConversionLimits | None = None,
    blob: MemoryBlob | None = None,
) -> ConversionRequest:
    return ConversionRequest(
        source=blob or MemoryBlob(content),
        source_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        format_id=format_id,
        media_type=media_type,
        original_filename=f"source.{format_id}",
        limits=limits or ConversionLimits(max_input_bytes=max(1, len(content))),
    )


def descriptor() -> ConverterDescriptor:
    return ConverterDescriptor(
        converter_id="tests.external",
        converter_version="1.0.0",
        spi_version=CONVERTER_SPI_VERSION,
        formats=frozenset({"text"}),
        media_types=frozenset({"text/plain"}),
        priority=1,
        execution_mode="subprocess",
        network_required=False,
        deterministic=True,
        license_expression="Apache-2.0",
        source_url="https://example.invalid/converter",
        capabilities=frozenset({"markdown"}),
        distribution_digest="a" * 64,
    )


def result_payload(**overrides):
    payload = {
        "protocol": CONVERTER_SPI_VERSION,
        "markdown": "converted\n",
        "assets": [],
        "metadata": {},
        "locations": [],
        "warnings": [],
        "completeness": "complete",
        "quality_score": "1.000",
    }
    payload.update(overrides)
    return payload


def ooxml_package(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


def docx_members() -> dict[str, bytes]:
    return {
        "[Content_Types].xml": b"""
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Override PartName="/word/document.xml"
                ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
            </Types>
        """,
        "word/document.xml": b"""
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body>
            </w:document>
        """,
    }


MALICIOUS_MARKDOWN_TEXT = """Visible line
```mermaid
graph TD
  A-->B
```
<script>alert(1)</script>
[link](https://example.invalid/link)
![image](https://example.invalid/image.png)
`internal backtick`"""


def assert_markdown_text_is_inert(markdown: str) -> None:
    assert re.search(r"(?<!\\)`{3,}\s*mermaid", markdown) is None
    assert re.search(r"(?<!\\)<script", markdown, re.IGNORECASE) is None
    assert re.search(r"(?<!\\)!\[", markdown) is None
    assert re.search(r"(?<!\\)\[link\]\(", markdown) is None
    assert "\\`\\`\\`mermaid" in markdown
    assert "\\<script\\>" in markdown
    assert "\\[link\\]\\(" in markdown
    assert "\\!\\[image\\]\\(" in markdown
    assert "\\`internal backtick\\`" in markdown
    assert " ⏎ " in markdown


def test_external_process_uses_fixed_absolute_argv_and_bounded_json(tmp_path):
    responder = tmp_path / "responder.py"
    responder.write_text(
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "assert request['source_path']=='source.bin'\n"
        f"json.dump({result_payload()!r},sys.stdout)\n",
        encoding="utf-8",
    )
    converter = ExternalProcessConverter(
        descriptor(), command=(str(Path(sys.executable).resolve()), str(responder))
    )

    result = converter.convert(request(b"hello", "text", "text/plain"))

    assert result.markdown == "converted\n"
    assert result.quality_score == Decimal("1.000")


def test_bundled_ooxml_subprocess_host_round_trip():
    content = ooxml_package(docx_members())
    converter = ExternalProcessConverter(
        OOXMLConverter().descriptor(),
        command=(
            sys.executable,
            "-m",
            "backend.worker.converter_host",
            "--converter",
            "dtxwiki.ooxml",
        ),
        preserve_executable_path=True,
    )

    result = converter.convert(
        request(
            content,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    )

    assert result.markdown == "Hello\n"


def test_bundled_ooxml_subprocess_health_handshake():
    converter = ExternalProcessConverter(
        OOXMLConverter().descriptor(),
        command=(
            sys.executable,
            "-m",
            "backend.worker.converter_host",
            "--converter",
            "dtxwiki.ooxml",
        ),
        preserve_executable_path=True,
    )

    assert converter.healthcheck() == (True, None)


def test_subprocess_health_rejects_descriptor_mismatch(tmp_path):
    responder = tmp_path / "health_responder.py"
    responder.write_text(
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "assert request['operation']=='health'\n"
        "json.dump({"
        f"'protocol':{CONVERTER_SPI_VERSION!r},"
        "'operation':'health','status':'ready',"
        "'converter_id':'wrong.converter','converter_version':'1.0.0',"
        "'formats':['text'],'media_types':['text/plain'],"
        "'output_schema_version':'1.0'},sys.stdout)\n",
        encoding="utf-8",
    )
    converter = ExternalProcessConverter(
        descriptor(), command=(str(Path(sys.executable).resolve()), str(responder))
    )

    assert converter.healthcheck() == (
        False,
        "converter_health_unavailable",
    )


def test_external_process_rejects_relative_executable_and_staging_mismatch():
    with pytest.raises(ValueError, match="absolute"):
        ExternalProcessConverter(descriptor(), command=("python", "worker.py"))

    converter = ExternalProcessConverter(
        descriptor(), command=(str(Path(sys.executable).resolve()), "-c", "pass")
    )
    blob = MemoryBlob(b"hello", copied_count=4)
    with pytest.raises(ConversionError) as exc_info:
        converter.convert(request(b"hello", "text", "text/plain", blob=blob))
    assert exc_info.value.code == "converter_source_integrity"


@pytest.mark.parametrize(
    "payload",
    [
        result_payload(unexpected=True),
        result_payload(markdown="x" * 33),
        result_payload(assets={}),
        result_payload(metadata=[]),
        result_payload(quality_score="NaN"),
        result_payload(completeness="partial"),
    ],
)
def test_external_result_envelope_is_strict(payload):
    limits = ConversionLimits(max_input_bytes=64, max_output_bytes=32)

    with pytest.raises(ConversionError) as exc_info:
        conversion_result_from_payload(payload, limits=limits)

    assert exc_info.value.code == "converter_result_invalid"


def test_external_error_message_is_not_trusted_as_safe_output():
    payload = {
        "protocol": CONVERTER_SPI_VERSION,
        "error": {
            "code": "converter_failed",
            "message": "source secret must not be reflected",
            "retryable": False,
            "fallback_allowed": True,
        },
    }

    with pytest.raises(ConversionError) as exc_info:
        conversion_result_from_payload(
            payload, limits=ConversionLimits(max_input_bytes=64)
        )

    assert exc_info.value.code == "converter_failed"
    assert exc_info.value.safe_message == "The converter reported a failure."


def test_bounded_process_rejects_output_flood_and_timeout(tmp_path):
    executable = str(Path(sys.executable).resolve())
    with pytest.raises(ConversionError) as output_error:
        _run_bounded(
            (executable, "-c", "print('x'*4096)"),
            cwd=tmp_path,
            stdin=b"",
            stdin_limit=32,
            stdout_limit=64,
            timeout_seconds=5,
        )
    assert output_error.value.code == "converter_transport_output_limit"

    with pytest.raises(ConversionError) as timeout_error:
        _run_bounded(
            (executable, "-c", "import time;time.sleep(10)"),
            cwd=tmp_path,
            stdin=b"",
            stdin_limit=32,
            stdout_limit=64,
            timeout_seconds=0.05,
        )
    assert timeout_error.value.code == "converter_timed_out"
    assert timeout_error.value.retryable is False
    assert timeout_error.value.fallback_allowed is False


def test_docx_conversion_and_archive_limits_use_request_limits():
    content = ooxml_package(docx_members())
    result = OOXMLConverter().convert(
        request(
            content,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    )
    assert result.markdown == "Hello\n"

    output_limits = ConversionLimits(max_input_bytes=len(content), max_output_bytes=4)
    output_limited = OOXMLConverter().convert(
        request(
            content,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            limits=output_limits,
        )
    )
    assert len(output_limited.markdown.encode("utf-8")) <= 4
    assert output_limited.completeness == "truncated"

    limits = ConversionLimits(max_input_bytes=len(content), max_archive_entries=1)
    with pytest.raises(ConversionError) as exc_info:
        OOXMLConverter().convert(
            request(
                content,
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                limits=limits,
            )
        )
    assert exc_info.value.code == "container_entry_limit"


def test_docx_serializes_attacker_text_and_omits_direct_hidden_runs():
    members = docx_members()
    attacker_text = html.escape(MALICIOUS_MARKDOWN_TEXT)
    members["word/document.xml"] = f"""
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p>
              <w:r><w:t xml:space="preserve">{attacker_text}</w:t></w:r>
              <w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden-docx-secret</w:t></w:r>
              <w:r><w:rPr><w:webHidden/></w:rPr><w:t>hidden-web-secret</w:t></w:r>
              <w:r>
                <w:rPr><w:vanish w:val="0"/></w:rPr>
                <w:t>explicitly visible</w:t>
              </w:r>
            </w:p>
          </w:body>
        </w:document>
    """.encode()
    content = ooxml_package(members)

    result = OOXMLConverter().convert(
        request(
            content,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    )

    assert_markdown_text_is_inert(result.markdown)
    assert "hidden-docx-secret" not in result.markdown
    assert "hidden-web-secret" not in result.markdown
    assert "explicitly visible" in result.markdown
    assert result.metadata["hidden_runs_omitted"] == 2
    assert {warning.code for warning in result.warnings} == {"hidden_content_omitted"}


def test_pptx_serializes_attacker_text_and_omits_hidden_slides():
    attacker_text = html.escape(MALICIOUS_MARKDOWN_TEXT)
    members = {
        "[Content_Types].xml": b"""
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Override PartName="/ppt/presentation.xml"
              ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
          </Types>
        """,
        "ppt/presentation.xml": b"""
          <p:presentation
            xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <p:sldIdLst>
              <p:sldId id="256" r:id="rId1"/>
              <p:sldId id="257" r:id="rId2"/>
            </p:sldIdLst>
          </p:presentation>
        """,
        "ppt/_rels/presentation.xml.rels": b"""
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
              Target="slides/slide1.xml"/>
            <Relationship Id="rId2"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
              Target="slides/slide2.xml"/>
          </Relationships>
        """,
        "ppt/slides/slide1.xml": f"""
          <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
            <p:cSld><p:spTree>
              <p:sp>
                <p:nvSpPr><p:cNvPr id="1" name="Visible" hidden="0"/></p:nvSpPr>
                <p:txBody><a:p><a:r><a:t>{attacker_text}</a:t></a:r></a:p></p:txBody>
              </p:sp>
              <p:sp>
                <p:nvSpPr><p:cNvPr id="2" name="Hidden" hidden="true"/></p:nvSpPr>
                <p:txBody><a:p><a:r><a:t>hidden-shape-secret</a:t></a:r></a:p></p:txBody>
              </p:sp>
            </p:spTree></p:cSld>
          </p:sld>
        """.encode(),
        "ppt/slides/slide2.xml": b"""
          <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" show="0">
            <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>
              <a:t>hidden-pptx-secret</a:t>
            </a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
          </p:sld>
        """,
    }
    content = ooxml_package(members)

    result = OOXMLConverter().convert(
        request(
            content,
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    )

    assert_markdown_text_is_inert(result.markdown)
    assert "hidden-pptx-secret" not in result.markdown
    assert "hidden-shape-secret" not in result.markdown
    assert result.metadata["slides_rendered"] == 1
    assert result.metadata["hidden_slides_omitted"] == 1
    assert result.metadata["hidden_shapes_omitted"] == 1
    assert {warning.code for warning in result.warnings} == {"hidden_content_omitted"}


def test_ooxml_rejects_external_relationship_active_content_and_unsafe_path():
    external = docx_members()
    external["word/_rels/document.xml.rels"] = b"""
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://example.invalid/hyperlink"
            Target="https://example.invalid/payload" TargetMode="External"/>
        </Relationships>
    """
    active = docx_members()
    active["word/vbaProject.bin"] = b"not executed"
    unsafe = docx_members()
    unsafe["../escape.xml"] = b"<escape/>"

    for members, code in (
        (external, "external_relationship_denied"),
        (active, "active_content_denied"),
        (unsafe, "container_path_invalid"),
    ):
        content = ooxml_package(members)
        with pytest.raises(ConversionError) as exc_info:
            OOXMLConverter().convert(
                request(
                    content,
                    "docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
        assert exc_info.value.code == code


@pytest.mark.parametrize(
    "member",
    [
        "word/webExtensions/webExtension1.xml",
        "word/customUI/customUI.xml",
        "xl/connections.xml",
        "xl/queryTables/queryTable1.xml",
        "xl/model/model.xml",
        "xl/customData/customData1.xml",
        "word/payload.js",
        "word/payload.html",
        "word/payload.svg",
        "word/payload.jar",
        "xl/query.iqy",
    ],
)
def test_ooxml_converter_rejects_unreferenced_active_payloads(member):
    active = docx_members()
    active[member] = b"not executed"

    with pytest.raises(ConversionError) as exc_info:
        OOXMLConverter().convert(
            request(
                ooxml_package(active),
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )

    assert exc_info.value.code == "active_content_denied"
    assert exc_info.value.fallback_allowed is False


def test_ooxml_rejects_dtd_and_uses_xml_depth_limit():
    dtd = docx_members()
    dtd["word/document.xml"] = (
        b"<w:document xmlns:w='urn:w'>"
        + b" " * 5000
        + b"<!DOCTYPE x><w:body/></w:document>"
    )
    dtd_content = ooxml_package(dtd)
    with pytest.raises(ConversionError) as dtd_error:
        OOXMLConverter().convert(
            request(
                dtd_content,
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
    assert dtd_error.value.code == "xml_entity_denied"

    content = ooxml_package(docx_members())
    limits = ConversionLimits(max_input_bytes=len(content), max_depth=2)
    with pytest.raises(ConversionError) as depth_error:
        OOXMLConverter().convert(
            request(
                content,
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                limits=limits,
            )
        )
    assert depth_error.value.code == "xml_structure_limit"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE w:document [<!ENTITY blocked "denied">]>',
        '<!ENTITY blocked "denied">',
    ],
)
def test_ooxml_rejects_nul_interleaved_xml_declarations(encoding, declaration):
    members = docx_members()
    document = (
        '<?xml version="1.0"?>'
        + declaration
        + '<w:document xmlns:w="urn:w"><w:body/></w:document>'
    )
    members["word/document.xml"] = document.encode(encoding)
    content = ooxml_package(members)

    with pytest.raises(ConversionError) as exc_info:
        OOXMLConverter().convert(
            request(
                content,
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )

    assert exc_info.value.code == "xml_entity_denied"


def test_xlsx_never_executes_or_emits_formula_source():
    members = {
        "[Content_Types].xml": b"""
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Override PartName="/xl/workbook.xml"
              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
          </Types>
        """,
        "xl/workbook.xml": b"""
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <sheets><sheet name="Safe" sheetId="1" r:id="rId1"/></sheets>
          </workbook>
        """,
        "xl/_rels/workbook.xml.rels": b"""
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet1.xml"/>
          </Relationships>
        """,
        "xl/worksheets/sheet1.xml": b"""
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData><row><c><f>HYPERLINK(&quot;https://example.invalid&quot;)</f><v>cached</v></c></row></sheetData>
          </worksheet>
        """,
    }
    content = ooxml_package(members)

    result = OOXMLConverter().convert(
        request(
            content,
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )

    assert "cached" not in result.markdown
    assert "Formula omitted" in result.markdown
    assert "HYPERLINK" not in result.markdown
    assert result.metadata["formulas_executed"] is False
    assert result.metadata["formula_cells_omitted"] == 1


@pytest.mark.parametrize(
    ("member_name", "member_xml"),
    [
        (
            "xl/workbook.xml",
            b"""
              <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <definedNames><definedName name="ExternalData">
                  WEBSERVICE(&quot;https://example.invalid/data&quot;)
                </definedName></definedNames>
              </workbook>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData><row><c><f>RTD(&quot;unsafe.progid&quot;,,&quot;topic&quot;)</f></c></row></sheetData>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData><row><c><f>cmd|'/c calc'!A0</f></c></row></sheetData>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData><row><c><f>_xlfn.IMAGE(&quot;https://example.invalid/image&quot;)</f></c></row></sheetData>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData><row><c><f>CALL(&quot;native&quot;,&quot;entry&quot;,&quot;J&quot;)</f></c></row></sheetData>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData><row><c><f>SUM('[Book.xlsx]Sheet1'!A1)</f></c></row></sheetData>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <dataValidations count="1"><dataValidation type="custom" sqref="A1">
                  <formula1>WEBSERVICE(&quot;https://example.invalid/data&quot;)</formula1>
                </dataValidation></dataValidations>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <dataValidations count="1"><dataValidation type="custom" sqref="A1">
                  <formula2>_xlfn.IMAGE(&quot;https://example.invalid/image&quot;)</formula2>
                </dataValidation></dataValidations>
              </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            b"""
              <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <conditionalFormatting sqref="A1">
                  <cfRule type="expression" priority="1">
                    <formula>RTD(&quot;unsafe.progid&quot;,,&quot;topic&quot;)</formula>
                  </cfRule>
                </conditionalFormatting>
              </worksheet>
            """,
        ),
    ],
    ids=[
        "defined-name-webservice",
        "cell-rtd",
        "cell-dde",
        "cell-image",
        "cell-native-call",
        "cell-external-workbook",
        "data-validation-formula1",
        "data-validation-formula2",
        "conditional-format-formula",
    ],
)
def test_xlsx_converter_rejects_external_capable_formulas(member_name, member_xml):
    members = {
        "[Content_Types].xml": b"""
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Override PartName="/xl/workbook.xml"
              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
          </Types>
        """,
        "xl/workbook.xml": b"""
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" />
        """,
        member_name: member_xml,
    }

    with pytest.raises(ConversionError) as exc_info:
        OOXMLConverter().convert(
            request(
                ooxml_package(members),
                "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )

    assert exc_info.value.code == "active_content_denied"
    assert exc_info.value.fallback_allowed is False


def test_xlsx_serializes_attacker_text_and_omits_hidden_content():
    sheet_title = html.escape(MALICIOUS_MARKDOWN_TEXT, quote=True).replace(
        "\n", "&#10;"
    )
    attacker_text = html.escape(MALICIOUS_MARKDOWN_TEXT)
    members = {
        "[Content_Types].xml": b"""
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Override PartName="/xl/workbook.xml"
              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
          </Types>
        """,
        "xl/workbook.xml": f"""
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <sheets>
              <sheet name="{sheet_title}" sheetId="1" r:id="rId1"/>
              <sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/>
              <sheet name="Very hidden" sheetId="3" state="veryHidden" r:id="rId3"/>
            </sheets>
          </workbook>
        """.encode(),
        "xl/_rels/workbook.xml.rels": b"""
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet1.xml"/>
            <Relationship Id="rId2"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet2.xml"/>
            <Relationship Id="rId3"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet3.xml"/>
          </Relationships>
        """,
        "xl/worksheets/sheet1.xml": f"""
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <cols>
              <col min="2" max="2" hidden="1"/>
              <col min="4" max="5" hidden="true"/>
            </cols>
            <sheetData>
              <row>
                <c r="A1" t="inlineStr"><is><t>{attacker_text}</t></is></c>
                <c r="B1" t="inlineStr"><is><t>hidden-column-secret</t></is></c>
                <c r="C1" t="inlineStr"><is><t>visible-column-value</t></is></c>
                <c r="D1" t="inlineStr"><is><t>hidden-range-secret</t></is></c>
              </row>
              <row hidden="1">
                <c t="inlineStr"><is><t>hidden-row-secret</t></is></c>
              </row>
              <row>
                <c r="A3">
                  <f>HYPERLINK(&quot;https://example.invalid&quot;)</f>
                  <v>cached-formula-secret</v>
                </c>
              </row>
              <row>
                <c t="inlineStr"><is><t>fallback-visible-a</t></is></c>
                <c t="inlineStr"><is><t>hidden-fallback-secret</t></is></c>
                <c t="inlineStr"><is><t>fallback-visible-c</t></is></c>
              </row>
            </sheetData>
          </worksheet>
        """.encode(),
        "xl/worksheets/sheet2.xml": b"""
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData>
              <row><c t="inlineStr"><is><t>hidden-sheet-secret</t></is></c></row>
            </sheetData>
          </worksheet>
        """,
        "xl/worksheets/sheet3.xml": b"""
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData>
              <row><c t="inlineStr"><is><t>very-hidden-sheet-secret</t></is></c></row>
            </sheetData>
          </worksheet>
        """,
    }
    content = ooxml_package(members)

    result = OOXMLConverter().convert(
        request(
            content,
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )

    assert_markdown_text_is_inert(result.markdown)
    assert "hidden-row-secret" not in result.markdown
    assert "hidden-column-secret" not in result.markdown
    assert "hidden-range-secret" not in result.markdown
    assert "hidden-fallback-secret" not in result.markdown
    assert "hidden-sheet-secret" not in result.markdown
    assert "very-hidden-sheet-secret" not in result.markdown
    assert "visible\\-column\\-value" in result.markdown
    assert "fallback\\-visible\\-a" in result.markdown
    assert "fallback\\-visible\\-c" in result.markdown
    assert "cached-formula-secret" not in result.markdown
    assert "HYPERLINK" not in result.markdown
    assert "Formula omitted" in result.markdown
    assert result.metadata["sheets_rendered"] == 1
    assert result.metadata["formula_cells_omitted"] == 1
    assert result.metadata["hidden_sheets_omitted"] == 2
    assert result.metadata["hidden_rows_omitted"] == 1
    assert result.metadata["hidden_columns_omitted"] == 3
    assert {warning.code for warning in result.warnings} == {"hidden_content_omitted"}


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [("0", "1"), ("2", "1"), ("1", "16385"), ("invalid", "2")],
)
def test_xlsx_rejects_invalid_or_unbounded_hidden_column_ranges(minimum, maximum):
    members = {
        "[Content_Types].xml": b"""
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Override PartName="/xl/workbook.xml"
              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
          </Types>
        """,
        "xl/workbook.xml": b"""
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <sheets><sheet name="Safe" sheetId="1" r:id="rId1"/></sheets>
          </workbook>
        """,
        "xl/_rels/workbook.xml.rels": b"""
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet1.xml"/>
          </Relationships>
        """,
        "xl/worksheets/sheet1.xml": f"""
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <cols><col min="{minimum}" max="{maximum}" hidden="1"/></cols>
            <sheetData><row><c r="A1"><v>safe</v></c></row></sheetData>
          </worksheet>
        """.encode(),
    }
    content = ooxml_package(members)

    with pytest.raises(ConversionError) as exc_info:
        OOXMLConverter().convert(
            request(
                content,
                "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )

    assert exc_info.value.code == "invalid_ooxml"
    assert exc_info.value.fallback_allowed is False


@pytest.mark.parametrize("reference", ["A0", "A1048577", "XFE1", "a1"])
def test_xlsx_rejects_invalid_or_out_of_range_cell_coordinates(reference):
    cell = ElementTree.fromstring(f'<c r="{reference}" />')

    with pytest.raises(ConversionError) as exc_info:
        _xlsx_cell_column(cell, fallback=1)

    assert exc_info.value.code == "invalid_ooxml"
    assert exc_info.value.fallback_allowed is False


def test_converter_host_validates_exact_envelope_path_size_and_hash(
    tmp_path, monkeypatch
):
    content = b"hello"
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    monkeypatch.chdir(tmp_path)
    limits = ConversionLimits(max_input_bytes=len(content))
    payload = {
        "protocol": CONVERTER_SPI_VERSION,
        "source_path": "source.bin",
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
        "format_id": "text",
        "media_type": "text/plain",
        "original_filename": "source.txt",
        "limits": asdict(limits),
        "options": {},
        "trace_id": "trace",
    }

    converted = _conversion_request(payload)
    assert converted.byte_size == len(content)

    for field, value, code in (
        ("source_path", "../source.bin", "converter_source_invalid"),
        ("source_sha256", "0" * 64, "converter_source_integrity"),
        ("byte_size", True, "converter_request_invalid"),
        ("original_filename", "../source.txt", "converter_request_invalid"),
    ):
        invalid = dict(payload)
        invalid[field] = value
        with pytest.raises(ConversionError) as exc_info:
            _conversion_request(invalid)
        assert exc_info.value.code == code

    extra = dict(payload, unexpected=True)
    with pytest.raises(ConversionError) as extra_error:
        _conversion_request(extra)
    assert extra_error.value.code == "converter_request_invalid"


@pytest.mark.parametrize(
    ("converter", "module_name", "format_id", "media_type"),
    [
        (PdfTextConverter(), "pypdf", "pdf", "application/pdf"),
        (ImageMetadataConverter(), "PIL", "png", "image/png"),
        (Hdf5Converter(), "h5py", "hdf5", "application/x-hdf5"),
        (
            ParquetConverter(),
            "pyarrow.parquet",
            "parquet",
            "application/vnd.apache.parquet",
        ),
    ],
)
def test_optional_converter_missing_dependency_is_a_safe_error(
    converter, module_name, format_id, media_type, monkeypatch
):
    monkeypatch.setitem(sys.modules, module_name, None)

    with pytest.raises(ConversionError) as exc_info:
        converter.convert(request(b"not parsed", format_id, media_type))

    assert exc_info.value.code == "converter_dependency_missing"
    assert "not installed" in exc_info.value.safe_message
