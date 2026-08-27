import tempfile
import unittest
from pathlib import Path
import shlex

from project_memory.rules import install_rules, render_rules, START_MARKER, END_MARKER


class RuleInstallerTests(unittest.TestCase):
    def test_rendered_root_is_exactly_one_shell_safe_posix_argument(self):
        root = Path("/tmp/project with 'quote' $HOME `uname`\nnext")
        rendered = render_rules(root)
        argument = shlex.quote(str(root.resolve()))
        self.assertEqual(rendered.count(argument), 2)
        self.assertNotIn('"' + str(root.resolve()) + '"', rendered)
        self.assertNotIn(";", rendered)
        self.assertNotIn("$(", rendered)

    def test_rendered_contract_cannot_execute_root_as_shell_code(self):
        root = Path("/tmp/$(touch SHOULD_NOT_EXIST) `touch ALSO_NOT` $HOME")
        rendered = render_rules(root)
        argument = shlex.quote(str(root.resolve()))
        self.assertEqual(rendered.count(argument), 2)
        command = rendered.split("run: ", 1)[1].split(".", 1)[0]
        self.assertEqual(shlex.split(command)[3], str(root.resolve()))

    def test_rendered_five_rule_contract_syncs_without_optional_extraction(self):
        root = Path("/tmp/project")
        rendered = render_rules(root)
        rules = [line for line in rendered.splitlines() if line.split(". ", 1)[0].isdigit()]
        self.assertEqual(len(rules), 5)
        self.assertIn(
            f"5. After a material decision or at completion, run: pmem sync --root {root.resolve()}.",
            rules,
        )
        self.assertNotIn("--extract", rendered)

    def test_all_agent_input_sent_to_pmem_must_be_english(self):
        rendered = render_rules(Path("/tmp/project"))
        self.assertIn("All text sent to pmem must be in English", rendered)
        self.assertIn("<current user request translated to English>", rendered)
        self.assertIn("Respond to the user in the user's language", rendered)

    def test_installs_exact_five_contract_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "AGENTS.md"
            target.write_text("# Local\n\nKeep this.\n", encoding="utf-8")
            install_rules(root)
            first = target.read_bytes()
            install_rules(root)
            self.assertEqual(target.read_bytes(), first)
            text = target.read_text()
            block = text[text.index(START_MARKER):text.index(END_MARKER) + len(END_MARKER)]
            self.assertEqual(block.count("\n"), 6)
            for contract in ("preflight", "evidence", "Cite", "conflict", "sync"):
                self.assertIn(contract, block)
            self.assertNotIn("--extract", block)

    def test_replaces_only_marked_block_preserving_unrelated_content_and_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "CLAUDE.md"
            target.write_bytes(b"before\r\n<!-- PROJECT-MEMORY:START -->\r\nold\r\n<!-- PROJECT-MEMORY:END -->\r\nafter\r\n")
            install_rules(root)
            data = target.read_bytes()
            self.assertTrue(data.startswith(b"before\r\n"))
            self.assertTrue(data.endswith(b"after\r\n"))
            self.assertNotIn(b"\r\nold\r\n", data)

    def test_skips_absent_files_and_create_agents_only_creates_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_rules(root)
            self.assertFalse((root / "AGENTS.md").exists())
            install_rules(root, create_agents=True)
            self.assertTrue((root / "AGENTS.md").exists())
            self.assertFalse((root / "CLAUDE.md").exists())
            self.assertFalse((root / "GEMINI.md").exists())


if __name__ == "__main__":
    unittest.main()
