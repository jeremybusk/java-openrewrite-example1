import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

import java_migrator as jm


class MigratorTests(unittest.TestCase):
    def test_sanitized_url_removes_embedded_credentials(self):
        self.assertEqual(
            "https://github.com/acme/app.git",
            jm.sanitized_url("https://user:secret@github.com/acme/app.git"),
        )

    def test_current_directory_has_a_destination_name(self):
        self.assertEqual(Path.cwd().name, jm.destination_name("."))

    def test_manifest_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = root / "repos.txt"
            text.write_text("# comment\nhttps://one/repo.git main\nhttps://two/repo.git\n")
            self.assertEqual("main", jm.read_manifest(text)[0].ref)
            data = root / "repos.json"
            data.write_text(json.dumps([
                "https://one/a.git",
                {"url": "https://two/b.git", "ref": "dev"},
            ]))
            self.assertEqual("dev", jm.read_manifest(data)[1].ref)

    def test_discovers_roots_but_not_nested_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "maven" / "module").mkdir(parents=True)
            (root / "maven" / "pom.xml").write_text("<project/>")
            (root / "maven" / "module" / "pom.xml").write_text("<project/>")
            (root / "gradle").mkdir()
            (root / "gradle" / "settings.gradle").write_text("")
            builds = jm.discover_builds(root, "auto", 4)
            self.assertEqual(
                {(root / "maven", "maven"), (root / "gradle", "gradle")},
                {(item.path, item.tool) for item in builds},
            )

    def test_generated_recipe_contains_target_and_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rewrite.yml"
            args = argparse.Namespace(
                target_java=21, cleanup=True, junit5=True,
                build_best_practices=False, dependency_strategy="patch", recipe=[],
            )
            jm.write_recipe(path, "maven", args)
            content = path.read_text()
            self.assertIn("UpgradeToJava21", content)
            self.assertIn("JUnit4to5Migration", content)
            self.assertIn('newVersion: "latest.patch"', content)

    def test_local_input_is_copied_then_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "pom.xml").write_text("<project/>")
            output = root / "artifacts"
            workspace = root / "state"
            output.mkdir()
            workspace.mkdir()
            args = argparse.Namespace(
                output=output, workspace=workspace, force=False,
                shallow=True, timeout=10,
            )
            log = workspace / "log"
            copied, cloned = jm.prepare_repo(
                jm.RepoSpec(str(source)), args, os.environ.copy(), log, root / "askpass",
            )
            self.assertFalse(cloned)
            self.assertTrue((copied / "pom.xml").is_file())
            with self.assertRaises(jm.SkipMigration):
                jm.prepare_repo(
                    jm.RepoSpec(str(source)), args, os.environ.copy(), log, root / "askpass",
                )

    def test_old_gradle_wrapper_uses_container_gradle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "gradlew"
            wrapper.write_text("#!/bin/sh\n")
            properties = root / "gradle" / "wrapper"
            properties.mkdir(parents=True)
            (properties / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10.3-bin.zip\n"
            )
            self.assertEqual("gradle", jm.executable(jm.BuildRoot(root, "gradle")))


if __name__ == "__main__":
    unittest.main()
