"""Upload parser: encodings and delimiters that Excel / Meltwater exports actually use."""
import codecs

from file_helpers.file_parser import parse_upload

NL, TAB = chr(10), chr(9)
ROWS = ["title,content,date", "First,hello,2026-01-02", "Second,world,2026-01-03"]


def _csv(sep=",", encoding="utf-8", bom=b""):
    text = NL.join(r.replace(",", sep) for r in ROWS) + NL
    return bom + text.encode(encoding)


def _titles(content):
    return [r["title"] for r in parse_upload("upload.csv", content)]


def test_plain_utf8():
    assert _titles(_csv()) == ["First", "Second"]


def test_utf8_with_bom():
    assert _titles(_csv(bom=codecs.BOM_UTF8)) == ["First", "Second"]


def test_utf16_tab_separated_excel_unicode_text():
    # Excel "Unicode Text" / Meltwater: FF FE mark, tab-separated, .csv extension.
    content = _csv(sep=TAB, encoding="utf-16-le", bom=codecs.BOM_UTF16_LE)
    assert content[:2] == b"" + bytes([0xFF, 0xFE])
    assert _titles(content) == ["First", "Second"]


def test_cp1252_western_excel_csv():
    rows = ["title,content,date", "Caf" + chr(0xE9) + ",r" + chr(0xE9) + "sum" + chr(0xE9) + ",2026-01-02"]
    content = (NL.join(rows) + NL).encode("cp1252")
    assert _titles(content) == ["Caf" + chr(0xE9)]


def test_semicolon_separated():
    assert _titles(_csv(sep=";")) == ["First", "Second"]
