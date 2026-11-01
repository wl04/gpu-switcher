"""Unit tests for the minimal .po -> .mo compiler (tools/po2mo.py).

A broken .mo silently breaks i18n (every lookup falls back to the msgid),
so the parser and the writer are pinned by these tests: multi-line joins,
unescaping, unterminated-string errors, and a round-trip through gettext.
"""
import gettext
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                 "..", "tools")))
import po2mo


class ParsePo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _po_file(self, text: str) -> str:
        path = os.path.join(self._tmp.name, "catalog.po")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_ParseJoins_MultiLineMsgstr_IntoSingleString(self):
        path = self._po_file(
            'msgid "Hello"\n'
            'msgstr "part one"\n'
            '"part two"\n')
        self.assertEqual(po2mo.parse_po(path), {"Hello": "part onepart two"})

    def test_ParsePo_UnescapesNewlinesAndQuotes(self):
        path = self._po_file(
            'msgid "say \\"hi\\""\n'
            'msgstr "first\\nsecond\\tend"\n')
        self.assertEqual(po2mo.parse_po(path),
                         {'say "hi"': "first\nsecond\tend"})

    def test_ParsePo_RaisesValueError_OnUnterminatedString(self):
        path = self._po_file('msgid "ok"\nmsgstr "abc\n')
        with self.assertRaises(ValueError):
            po2mo.parse_po(path)


class WriteMo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_WriteMo_ProducesCatalogReadableByGettext(self):
        entries = {
            "": "Content-Type: text/plain; charset=UTF-8\n",
            "Hello": "Привет",
            "Run in a terminal after reboot or login:":
                "В терминале после перезагрузки или входа:",
        }
        path = os.path.join(self._tmp.name, "catalog.mo")
        po2mo.write_mo(entries, path)
        with open(path, "rb") as f:
            catalog = gettext.GNUTranslations(f)
        self.assertEqual(catalog.gettext("Hello"), "Привет")
        self.assertEqual(
            catalog.gettext("Run in a terminal after reboot or login:"),
            "В терминале после перезагрузки или входа:")
        # Unknown msgids pass through unchanged.
        self.assertEqual(catalog.gettext("missing"), "missing")


if __name__ == "__main__":
    unittest.main()
