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
            self.assertNotIn("junit-platform-launcher", content)

            jm.write_recipe(path, "gradle", args)
            self.assertIn("junit-platform-launcher", path.read_text())

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

    def test_maven_central_is_the_default_repository(self):
        args = jm.parse_args(["example"])
        self.assertEqual("maven-central", args.recipe_repository)
        self.assertEqual("6.46.1", args.maven_plugin_version)
        self.assertEqual("7.39.0", args.gradle_plugin_version)
        self.assertEqual("3.42.1", args.migrate_java_version)
        self.assertIsNone(args.artifact_repository)

    def test_repository_modes_generate_isolated_gradle_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            central = jm.parse_args(["example"])
            central_path = root / "central.gradle"
            jm.write_gradle_init(central_path, central, "example.Recipe", root / "rewrite.yml")
            central_text = central_path.read_text()
            self.assertIn("mavenCentral()", central_text)
            self.assertIn('activeRecipe("example.Recipe")', central_text)
            self.assertIn("configFile = file(", central_text)
            self.assertNotIn("codegenomeproject.org", central_text)
            self.assertNotIn("mavenLocal()", central_text)

            local = jm.parse_args(["example", "--recipe-repository", "maven-local"])
            local_path = root / "local.gradle"
            jm.write_gradle_init(local_path, local, "example.Recipe", root / "rewrite.yml")
            self.assertIn("mavenLocal()", local_path.read_text())

            codegenome = jm.parse_args(["example", "--recipe-repository", "codegenome"])
            codegenome_path = root / "codegenome.gradle"
            jm.write_gradle_init(
                codegenome_path, codegenome, "example.Recipe", root / "rewrite.yml"
            )
            codegenome_text = codegenome_path.read_text()
            self.assertIn(jm.CODE_GENOME_URL, codegenome_text)
            self.assertIn('System.getenv("CODE_GENOME_TOKEN")', codegenome_text)
            self.assertEqual("3.45.0", codegenome.migrate_java_version)

    def test_central_maven_settings_do_not_add_code_genome(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.xml"
            args = jm.parse_args(["example"])
            args.maven_settings = Path(directory) / "missing-settings.xml"
            jm.write_maven_settings(path, args, {})
            content = path.read_text()
            self.assertIn("<settings", content)
            self.assertNotIn("codegenome", content)

    def test_parent_git_boundary_is_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / ".git").mkdir()
            copied_project = parent / "artifacts" / "app"
            copied_project.mkdir(parents=True)
            marker = copied_project / ".git"
            with jm.isolate_from_parent_git(copied_project):
                self.assertTrue(marker.is_file())
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
