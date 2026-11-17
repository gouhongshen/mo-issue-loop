import tempfile
import unittest
from pathlib import Path

from mo_runner.topology import configure


class TopologyObservabilityTest(unittest.TestCase):
    def test_statement_metadata_is_collectable_on_every_node(self):
        with tempfile.TemporaryDirectory() as directory:
            configure(directory)
            for name in ("log", "tn", "cn", "cn1"):
                with self.subTest(node=name):
                    config = (Path(directory) / (name + ".toml")).read_text()
                    # The legacy trace switch gates statement, log and error
                    # exporters too; a SQL-only readiness probe misses this gap.
                    self.assertIn("disableTrace = false", config)
                    self.assertNotRegex(config, r"disable(?:Trace|-trace)\s*=\s*true")
                    self.assertIn("disableMetric = true", config)
