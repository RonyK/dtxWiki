from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.converters.external_protocol import _run_bounded
from backend.spi.conversion import (
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
)
from backend.worker.converter_host import main as converter_host_main


class _LocaleBoundInput:
    def __init__(self, content: bytes) -> None:
        self.buffer = io.BytesIO(content)


class _Cp1252Output:
    """Fail if the host accidentally returns to locale text output."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, content: str) -> int:
        content.encode("cp1252", errors="strict")
        return len(content)

    def flush(self) -> None:
        return None


def _package(members: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content.encode("utf-8"))
    return output.getvalue()


def _docx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": """
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Override PartName="/word/document.xml"
                    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
                </Types>
            """,
            "word/document.xml": """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>한글 Word 😀</w:t></w:r></w:p></w:body>
                </w:document>
            """,
        }
    )


def _xlsx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": """
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Override PartName="/xl/workbook.xml"
                    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
                </Types>
            """,
            "xl/workbook.xml": """
                <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <sheets><sheet name="한글 시트 😀" sheetId="1" r:id="rId1"/></sheets>
                </workbook>
            """,
            "xl/_rels/workbook.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
                    Target="worksheets/sheet1.xml"/>
                </Relationships>
            """,
            "xl/worksheets/sheet1.xml": """
                <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                  <sheetData><row><c t="inlineStr"><is>
                    <t>한글 Excel 😀</t>
                  </is></c></row></sheetData>
                </worksheet>
            """,
        }
    )


def _pptx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": """
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Override PartName="/ppt/presentation.xml"
                    ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
                </Types>
            """,
            "ppt/presentation.xml": """
                <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
                </p:presentation>
            """,
            "ppt/_rels/presentation.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
                    Target="slides/slide1.xml"/>
                </Relationships>
            """,
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>
                    <a:t>한글 PowerPoint 😀</a:t>
                  </a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
                </p:sld>
            """,
        }
    )


@pytest.mark.parametrize(
    ("format_id", "media_type", "content", "expected"),
    (
        # The OOXML bytes are zip archives whose entry timestamps differ per
        # build, so pytest derived a different node id from them on every
        # collection. Name the cases by format instead.
        pytest.param(
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx(),
            "한글 Word 😀",
            id="docx",
        ),
        pytest.param(
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            _xlsx(),
            "한글 Excel 😀",
            id="xlsx",
        ),
        pytest.param(
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx(),
            "한글 PowerPoint 😀",
            id="pptx",
        ),
    ),
)
def test_ooxml_host_writes_strict_utf8_bytes_under_cp1252_text_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    media_type: str,
    content: bytes,
    expected: str,
) -> None:
    (tmp_path / "source.bin").write_bytes(content)
    limits = ConversionLimits(max_input_bytes=len(content))
    request = {
        "protocol": CONVERTER_SPI_VERSION,
        "source_path": "source.bin",
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
        "format_id": format_id,
        "media_type": media_type,
        "original_filename": f"한글-{format_id}-😀.{format_id}",
        "limits": asdict(limits),
        "options": {},
        "trace_id": "unicode-protocol-regression",
    }
    encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
    stdout = _Cp1252Output()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _LocaleBoundInput(encoded))
    monkeypatch.setattr(sys, "stdout", stdout)

    assert converter_host_main(["--converter", "dtxwiki.ooxml"]) == 0

    payload = json.loads(stdout.buffer.getvalue().decode("utf-8", errors="strict"))
    assert expected in payload["markdown"]
    assert payload["protocol"] == CONVERTER_SPI_VERSION


def test_parent_rejects_non_utf8_converter_response(tmp_path: Path) -> None:
    command = (
        str(Path(sys.executable).resolve()),
        "-c",
        "import sys;sys.stdout.buffer.write(b'\\xff')",
    )

    with pytest.raises(ConversionError) as error:
        _run_bounded(
            command,
            cwd=tmp_path,
            stdin=b"",
            stdout_limit=64,
            timeout_seconds=5,
        )

    assert error.value.code == "converter_result_invalid"
