import hashlib
import stat
import tempfile
import unittest
from pathlib import Path
import json

from project_memory.config import (
    ProjectConfig,
    ProjectPaths,
    load_project_config,
    save_project_config,
)


class ConfigTests(unittest.TestCase):
    def test_save_rejects_root_or_alias_collision_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp) / "data"
            root_a, root_b, alias_a = (Path(tmp) / name for name in ("a", "b", "alias-a"))
            root_a.mkdir(); root_b.mkdir(); alias_a.mkdir()
            save_project_config(ProjectConfig.create("a", root_a, [alias_a]), ProjectPaths.for_root(root_a, data_home))
            registry_before = (data_home / "registry.json").read_bytes()
            paths_b = ProjectPaths.for_root(root_b, data_home)
            config_b = ProjectConfig.create("b", root_b, [alias_a])
            with self.assertRaisesRegex(ValueError, "project_path_collision"):
                save_project_config(config_b, paths_b)
            self.assertFalse(paths_b.config_file.exists())
            self.assertEqual((data_home / "registry.json").read_bytes(), registry_before)
            self.assertEqual(set(json.loads((data_home / "registry.json").read_text())["projects"]), {ProjectConfig.create("a", root_a).project_id})

    def test_same_project_update_may_reuse_its_root_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_home, root, alias = Path(tmp) / "data", Path(tmp) / "root", Path(tmp) / "alias"
            root.mkdir(); alias.mkdir()
            config = ProjectConfig.create("a", root, [alias])
            paths = ProjectPaths.for_root(root, data_home)
            save_project_config(config, paths)
            save_project_config(ProjectConfig.create("renamed", root, [alias]), paths)
            self.assertEqual(load_project_config(alias, data_home).name, "renamed")
    def test_two_projects_share_data_home_without_config_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp) / "data"
            root_a = Path(tmp) / "a"
            root_b = Path(tmp) / "b"
            alias_a = Path(tmp) / "alias-a"
            root_a.mkdir(); root_b.mkdir(); alias_a.mkdir()
            config_a = ProjectConfig.create("a", root_a, [alias_a])
            config_b = ProjectConfig.create("b", root_b)
            save_project_config(config_a, ProjectPaths.for_root(root_a, data_home))
            save_project_config(config_b, ProjectPaths.for_root(root_b, data_home))
            self.assertEqual(load_project_config(root_a, data_home).name, "a")
            self.assertEqual(load_project_config(alias_a, data_home).name, "a")
            self.assertEqual(load_project_config(root_b, data_home).name, "b")
            self.assertEqual(ProjectPaths.for_root(alias_a, data_home).project_id, config_a.project_id)

    def test_paths_config_aliases_and_permissions(self):
        with tempfile.TemporaryDirectory() as home:
            temp_home = Path(home)
            root = Path("/tmp/example")
            paths = ProjectPaths.for_root(root, data_home=temp_home)
            config = ProjectConfig.create(
                "example", root, aliases=[Path("/tmp/alias")]
            )
            self.assertEqual(paths.project_id, config.project_id)
            save_project_config(config, paths)
            self.assertEqual(stat.S_IMODE(paths.config_file.stat().st_mode), 0o600)
            loaded = load_project_config(Path("/tmp/alias"), data_home=temp_home)
            self.assertEqual(loaded.name, "example")
            for directory in (paths.project_dir, paths.receipts_dir, paths.exports_dir):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_project_config_identity_matches_project_paths_and_canonicalizes_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            alias = Path(tmp) / "alias"
            root.mkdir()
            alias.mkdir()
            config = ProjectConfig.create("example", root / ".." / root.name, [alias / ".." / alias.name])
            paths = ProjectPaths.for_root(root)
            self.assertEqual(config.project_id, paths.project_id)
            self.assertEqual(config.root, root.resolve())
            self.assertEqual(config.aliases, (alias.resolve(),))

    def test_records_are_immutable(self):
        from project_memory.models import KnowledgeCandidate, NormalizedMessage

        message = NormalizedMessage(
            "hermes", "s", "m", "p", "user", "now", "text", None, "hash", {}
        )
        candidate = KnowledgeCandidate(
            "idea", "text", "provisional", 0.5, "m", "text", 0, 4, True
        )
        with self.assertRaises(Exception):
            message.content = "changed"
        with self.assertRaises(Exception):
            candidate.statement = "changed"


if __name__ == "__main__":
    unittest.main()
