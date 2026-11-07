import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PUBLISH = Path(__file__).parents[1] / "scripts/publish_news.sh"


class PublishTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              text=True, capture_output=True).stdout.strip()

    def test_publish_preserves_concurrent_changes(self):
        for change in ("none", "unrelated", "conflict", "no-news"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote, writer, runner = (root / name for name in ("remote.git", "writer", "runner"))
                self.git(root, "init", "--bare", str(remote))
                self.git(root, "clone", str(remote), str(writer))
                self.git(writer, "checkout", "-b", "main")
                self.git(writer, "config", "user.name", "Test")
                self.git(writer, "config", "user.email", "test@example.org")
                (writer / "news.json").write_text('["base"]\n')
                (writer / "scripts").mkdir()
                # Stand in for a fresh collection against the concurrent archive.
                (writer / "scripts/update_news.py").write_text(
                    'from pathlib import Path\n'
                    'Path("news.json").write_text(\'["base", "remote", "local"]\\n\')\n')
                self.git(writer, "add", ".")
                self.git(writer, "commit", "-m", "initial")
                self.git(writer, "push", "origin", "main")
                self.git(root, "clone", "--branch", "main", str(remote), str(runner))
                if change in ("unrelated", "conflict"):
                    target = "news.json" if change == "conflict" else "note.txt"
                    (writer / target).write_text('["base", "remote"]\n')
                    self.git(writer, "add", target)
                    self.git(writer, "commit", "-m", "concurrent edit")
                    self.git(writer, "push", "origin", "main")
                if change != "no-news":
                    (runner / "news.json").write_text('["base", "local"]\n')
                bin_dir = root / "bin"
                bin_dir.mkdir()
                (bin_dir / "python").symlink_to(sys.executable)
                env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
                result = subprocess.run(["bash", str(PUBLISH)], cwd=runner, env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.git(writer, "fetch", "origin", "main")
                articles = json.loads(self.git(writer, "show", "origin/main:news.json"))
                self.assertEqual(articles, ["base"] if change == "no-news" else
                                 ["base", "remote", "local"] if change == "conflict" else
                                 ["base", "local"])
                if change == "unrelated":
                    self.assertEqual(self.git(writer, "show", "origin/main:note.txt"), '["base", "remote"]')


if __name__ == "__main__":
    unittest.main()
