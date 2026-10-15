# tests/test_pool_report.py
"""Client ids must match the config, and the pool report must be truthful."""

import importlib.util
import os
import sys
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from Thunder.utils.config_parser import TokenParser  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "check_pool",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "check_pool.py"),
)
check_pool = importlib.util.module_from_spec(_SPEC)
sys.modules["check_pool"] = check_pool
_SPEC.loader.exec_module(check_pool)


class TokenParserIdsTest(unittest.TestCase):
    """A reported 'Client ID 28' has to mean MULTI_TOKEN28, not the 28th survivor."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("MULTI_TOKEN1", "MULTI_TOKEN3", "MULTI_TOKEN10", "MULTI_TOKEN29")}
        for key in self._saved:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_ids_keep_the_configured_ordinal(self):
        os.environ["MULTI_TOKEN1"] = "111:aaa"
        os.environ["MULTI_TOKEN3"] = "333:ccc"
        os.environ["MULTI_TOKEN10"] = "101010:jjj"

        tokens = TokenParser().parse_from_env()

        self.assertEqual(sorted(tokens), [1, 3, 10])
        self.assertEqual(tokens[3], "333:ccc")
        self.assertEqual(tokens[10], "101010:jjj")

    def test_a_gap_does_not_renumber_the_rest(self):
        os.environ["MULTI_TOKEN1"] = "111:aaa"
        os.environ["MULTI_TOKEN29"] = "292929:zzz"

        tokens = TokenParser().parse_from_env()

        self.assertEqual(sorted(tokens), [1, 29])
        self.assertEqual(tokens[29], "292929:zzz")

    def test_empty_slots_are_skipped(self):
        os.environ["MULTI_TOKEN1"] = "   "
        os.environ["MULTI_TOKEN3"] = "333:ccc"

        tokens = TokenParser().parse_from_env()

        self.assertEqual(sorted(tokens), [3])


class ConfiguredSlotsTest(unittest.TestCase):
    def test_reads_and_normalises_slots(self):
        environ = {
            "MULTI_TOKEN1": " 111:aaa ",
            "MULTI_TOKEN2": '"222:bbb"',
            "MULTI_TOKEN7": "",
            "MULTI_TOKENX": "ignored",
            "OTHER": "nope",
        }
        slots = check_pool.configured_slots(environ)
        self.assertEqual(slots, {1: "111:aaa", 2: "222:bbb"})

    def test_missing_slots_are_reported_between_the_ends(self):
        slots = {1: "a", 3: "c"}
        self.assertEqual(check_pool.missing_slots(slots, upto=4), [2])

    def test_no_slots_means_no_missing_list(self):
        self.assertEqual(check_pool.missing_slots({}), [])

    def test_mask_never_reveals_the_secret(self):
        masked = check_pool.mask_token("123456789:AAF-secret-value")
        self.assertNotIn("secret", masked)
        self.assertTrue(masked.startswith("123456789:"))
        self.assertEqual(check_pool.mask_token(""), "(empty)")


if __name__ == "__main__":
    unittest.main()
