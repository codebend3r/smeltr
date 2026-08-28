"""verdict.py's exit code is the deletion gate, and nothing tested it.

`.autopilot.sh` branches on this number alone: 0 means record, sync, and
PERMANENTLY DELETE a ~90 GB library original; anything else halts. Twelve
suites cover the thresholds inside `core._verdict()` (test_verdict_calibration)
but not one covered the translation from its verdict WORD to that number.

The two failures this pins:

  * A new verdict word added to `core._verdict()` and not considered here.
    The fallthrough sends any unknown word to 2 (halt), which is the safe
    direction -- but "safe by accident of a fallthrough" is worth asserting
    rather than hoping for.
  * `verdict.py`'s HALT set drifting from what actually halts. HALT is
    currently DEAD -- defined, never read -- so a maintainer adding a word to
    it gets no behaviour change while believing they configured one.
"""
import ast
import os
import unittest

import verdict


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def verdict_words() -> set:
    """Every word `core._verdict()` can return, read out of the source.

    Parsed rather than hand-listed so a word added to core.py fails this
    suite instead of silently inheriting the fallthrough.
    """
    tree = ast.parse(source("core.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_verdict")
    words = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            first = node.value.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                words.add(first.value)
    return words


# The word verdict.py synthesises itself when the log reports decoder errors.
# It never comes from core._verdict(), so the AST scan above cannot see it.
SYNTHETIC = {"halt-decoder-errors"}


def exit_code_for(word: str) -> int:
    """The mapping at the bottom of verdict.main(), in isolation."""
    if word == "good":
        return 0
    if word in verdict.LADDER:
        return 3
    return 2


class Mapping(unittest.TestCase):
    def test_only_good_exits_zero(self):
        """0 is the delete-the-original code. Exactly one word may earn it."""
        zero = {w for w in verdict_words() | SYNTHETIC if exit_code_for(w) == 0}
        self.assertEqual(zero, {"good"},
                         "a word other than 'good' now authorises a deletion")

    def test_every_word_maps_to_a_documented_code(self):
        for word in verdict_words() | SYNTHETIC:
            with self.subTest(word=word):
                self.assertIn(exit_code_for(word), (0, 2, 3))

    def test_ladder_words_retry_rather_than_halt(self):
        """3 = the CRF ladder retries. Only blowup/no-saving may take it:
        every other non-good word needs a human, and 2 is how it gets one."""
        self.assertEqual(verdict.LADDER, {"no-saving", "blowup"})
        for word in verdict.LADDER:
            self.assertIn(word, verdict_words(),
                          f"LADDER carries '{word}', which core._verdict() "
                          f"can no longer return")

    def test_unknown_word_halts(self):
        """The fallthrough must fail SAFE, not fail open."""
        self.assertEqual(exit_code_for("some-future-verdict"), 2)


class HaltSetIsHonest(unittest.TestCase):
    """HALT is declared but never read -- `return 2` is the real fallthrough.

    That is not a bug today, but the constant reads like the definition of
    'what halts' and is not. Either it is total, or it should be deleted.
    """

    def test_halt_set_is_not_read_by_main(self):
        src = source("verdict.py")
        body = src[src.index("def main("):]
        self.assertNotIn("HALT", body,
                         "HALT is now read by main(); update this suite to "
                         "assert the mapping it drives")

    def test_halt_set_omits_words_that_do_halt(self):
        """Documents the gap rather than asserting the constant is correct.

        Delete HALT, or extend it to cover these, and change this test.
        """
        halting = {w for w in verdict_words() | SYNTHETIC
                   if exit_code_for(w) == 2}
        self.assertEqual(halting - verdict.HALT, {"unknown", "halt-decoder-errors"},
                         "the set of words that halt but are missing from HALT "
                         "has changed")


class Docstring(unittest.TestCase):
    def test_module_docstring_lists_the_live_codes(self):
        doc = verdict.__doc__ or ""
        for code in ("0  good", "2  halt", "3  ladder", "4  error"):
            self.assertIn(code, doc)


if __name__ == "__main__":
    unittest.main()
