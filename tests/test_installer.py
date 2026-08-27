import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


class InstallerTests(unittest.TestCase):
    def run_installer(self, *args, env=None):
        return subprocess.run(
            ["/bin/bash", str(INSTALLER), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
        )

    def fake_environment(self, target, *, registered=False, broken_help=False, uv_bin_dir=None):
        fake_bin = target / "fake-bin"
        fake_bin.mkdir()
        if uv_bin_dir is None:
            uv_bin_dir = fake_bin
        else:
            uv_bin_dir.mkdir()
        log = target / "calls.log"
        state = target / "registered"
        if registered:
            state.write_text("yes")
        pmem = fake_bin / "pmem"
        pmem.write_text("".join([
            "#!/bin/bash\n",
            "set -u\n",
            "printf '%s\\n' \"pmem $*\" >> \"$CALL_LOG\"\n",
            "printf '%s\\n' \"pmem-exec $0\" >> \"$CALL_LOG\"\n",
            ("if [[ \"${1-}\" == --help ]]; then printf 'fatal: /private/user/secret\\n' >&2; exit 7; fi\n" if broken_help else ""),
            "case \"${1-}\" in\n",
            "  status) [[ -f \"$REGISTERED_STATE\" ]] || exit 1 ;;\n",
            "  init) printf 'PMEM_EMBEDDING_MODEL=%s\\n' \"${PMEM_EMBEDDING_MODEL-}\" >> \"$CALL_LOG\"; touch \"$REGISTERED_STATE\" ;;\n",
            "esac\n",
        ]))
        pmem.chmod(0o755)
        if uv_bin_dir != fake_bin:
            (uv_bin_dir / "pmem").write_bytes(pmem.read_bytes())
            (uv_bin_dir / "pmem").chmod(0o755)
        uv = fake_bin / "uv"
        uv.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            "case \"${1-} ${2-} ${3-}\" in\n"
            "  'tool dir --bin') printf '%s\\n' \"$UV_BIN_DIR\" ;;\n"
            "  *) printf '%s\\n' \"uv $*\" >> \"$CALL_LOG\" ;;\n"
            "esac\n"
        )
        uv.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": str(fake_bin) + ":/usr/bin:/bin",
            "FAKE_BIN": str(fake_bin),
            "UV_BIN_DIR": str(uv_bin_dir),
            "CALL_LOG": str(log),
            "REGISTERED_STATE": str(state),
        })
        return environment, log

    def test_missing_project_root_is_rejected(self):
        result = self.run_installer("--skip-tool-install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--project-root", result.stderr)

    def test_new_project_initializes_checks_status_and_installs_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target with spaces"
            target.mkdir()
            env, log = self.fake_environment(target)
            result = self.run_installer(
                "--project-root", str(target), "--name", "A name", "--embedding-model", "nomic-test",
                "--skip-tool-install",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [line for line in log.read_text().splitlines() if line.startswith("pmem ")]
            self.assertEqual(calls[0].split()[0:2], ["pmem", "--help"])
            self.assertEqual(calls[1].split()[0:2], ["pmem", "migrate-local"])
            self.assertTrue(any(call.startswith("pmem init") for call in calls))
            self.assertTrue(any(call.startswith("pmem install-rules") for call in calls))
            self.assertEqual(sum(call.startswith("pmem status") for call in calls), 2)
            self.assertIn("PMEM_EMBEDDING_MODEL=nomic-test", log.read_text())
            self.assertIn("pmem sync", result.stdout)
            self.assertIn("Nomic", result.stdout)

    def test_existing_registration_is_preserved_and_no_init(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.mkdir()
            env, log = self.fake_environment(target, registered=True)
            result = self.run_installer("--project-root", str(target), "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertFalse(any(" init" in call for call in calls))
            self.assertEqual(sum(" status" in call for call in calls), 2)

    def test_no_rules_skips_rules_command(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            env, log = self.fake_environment(target)
            result = self.run_installer("--project-root", str(target), "--no-rules", "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("install-rules", log.read_text())

    def test_installer_ignores_local_memory_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            (target / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            env, _ = self.fake_environment(target)
            result = self.run_installer("--project-root", str(target), "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/.project-memory/", (target / ".gitignore").read_text())

    def test_normal_mode_installs_editable_tool_with_selected_python(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            env, log = self.fake_environment(target)
            result = self.run_installer(
                "--project-root", str(target), "--python", "3.12", env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("uv tool install --force --editable", log.read_text())
            self.assertIn("--python 3.12", log.read_text())

    def test_broken_pmem_help_exits_before_status_init_or_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            env, log = self.fake_environment(target, broken_help=True)
            result = self.run_installer("--project-root", str(target), "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("pmem --help failed", result.stderr)
            self.assertIn("reinstall", result.stderr)
            self.assertNotIn("/private/user/secret", result.stderr)
            calls = [line for line in log.read_text().splitlines() if line.startswith("pmem ")]
            self.assertEqual(calls, ["pmem --help"])

    def test_warns_when_uv_bin_dir_is_not_on_path_and_uses_absolute_pmem(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            env, log = self.fake_environment(target, uv_bin_dir=Path(directory) / "uv-tools-bin")
            result = self.run_installer("--project-root", str(target), "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: uv tool bin directory is not on PATH", result.stderr)
            self.assertIn("export PATH=" + str(Path(directory) / "uv-tools-bin") + ":$PATH", result.stderr)
            self.assertNotIn("\\$PATH", result.stderr)
            self.assertIn("pmem-exec " + str(Path(directory) / "uv-tools-bin" / "pmem"), log.read_text())

    def test_installer_never_requests_sync_extract_or_model_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            env, log = self.fake_environment(target)
            result = self.run_installer("--project-root", str(target), "--skip-tool-install", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text()
            self.assertNotIn("pmem sync", calls)
            self.assertNotIn("pmem extract", calls)
            self.assertNotIn("model load", calls)

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(["/bin/bash", "-n", str(INSTALLER)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
