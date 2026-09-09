import re
import unittest

import unhog


class VersionTests(unittest.TestCase):
    def test_version_string(self):
        self.assertIsInstance(unhog.__version__, str)
        self.assertTrue(unhog.__version__)
        self.assertFalse(unhog.__version__.startswith("v"))
        # From git: "0.1.0", "0.1.0-3-g1a2b3c4-dirty", a bare hash, or "dev".
        # From setuptools-scm (installed package): "0.1.0", "0.1.1.dev3+g1a2b3c4.d20260908".
        self.assertRegex(unhog.__version__, r"^(\d+\.\d+\.\d+(-\d+-g[0-9a-f]+)?(-dirty)?"
                                            r"|\d+\.\d+\.\d+(\.dev\d+)?(\+[0-9a-z.]+)?"
                                            r"|[0-9a-f]{7,}(-dirty)?|dev)$")


if __name__ == "__main__":
    unittest.main()
