import unittest

from rns_resolve.nodereg import derive_site_name, read_node_name


class DeriveSiteNameTest(unittest.TestCase):
    def test_bold_unicode_and_emoji(self):
        # Mathematical Sans-Serif Bold RNS-RESOLVE plus a compass emoji.
        bold = ("\U0001D5E5\U0001D5E1\U0001D5E6-\U0001D5E5\U0001D5D8"
                "\U0001D5E6\U0001D5E2\U0001D5DF\U0001D5E9\U0001D5D8"
                " \U0001F9ED")
        self.assertEqual(derive_site_name(bold), "rns-resolve")

    def test_spaces_become_dashes(self):
        bold_tmt = ("\U0001D5E7\U0001D5DB\U0001D5D8 \U0001D5E0\U0001D5DC"
                    "\U0001D5DF\U0001D5D7 \U0001D5E7\U0001D5D4\U0001D5DE"
                    "\U0001D5D8 \U0001F4F0")
        self.assertEqual(derive_site_name(bold_tmt), "the-mild-take")

    def test_plain_name_passthrough(self):
        self.assertEqual(derive_site_name("windy-valley.market"),
                         "windy-valley.market")

    def test_nothing_usable_raises(self):
        with self.assertRaises(ValueError):
            derive_site_name("\U0001F9ED \U0001F4F0")

    def test_dash_runs_collapse_and_trim(self):
        self.assertEqual(derive_site_name("  A  --  B  "), "a-b")


class ReadNodeNameTest(unittest.TestCase):
    def test_reads_value_and_skips_comments(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("# node_name = commented\n[node]\n"
                         "enable_node = yes\n"
                         "node_name = My Node \U0001F9ED\n")
            self.assertEqual(read_node_name(p), "My Node \U0001F9ED")

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_node_name("/nonexistent/nowhere"))
