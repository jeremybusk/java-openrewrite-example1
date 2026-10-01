#!/usr/bin/env python3
"""Batch Java repository modernization with OpenRewrite (stdlib only)."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence


VERSIONS = {
    "maven_plugin": "6.49.0",
    "gradle_plugin": "7.41.0",
    "migrate_java": "3.45.0",
    "static_analysis": "2.44.0",
    "java_dependencies": "1.63.0",
    "testing_frameworks": "3.47.0",
}
TARGETS = (11, 17, 21, 25)
CODE_GENOME_URL = "https://artifacts.codegenomeproject.org/maven"
PRINT_LOCK = threading.Lock()


class MigrationError(RuntimeError):
    pass


class SkipMigration(MigrationError):
    pass


@dataclasses.dataclass(frozen=True)
class RepoSpec:
    source: str
    ref: str | None = None


@dataclasses.dataclass(frozen=True)
class BuildRoot:
    path: Path
    tool: str


@dataclasses.dataclass
class ProjectResult:
    path: str
    build_tool: str
    status: str = "failed"
    changed: bool = False
    error: str = ""
    manual_review: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Result:
    source: str
    path: str = ""
    status: str = "failed"
    changed: bool = False
    branch: str = ""
    commit: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    log: str = ""
    diff_stat: str = ""
    projects: list[ProjectResult] = dataclasses.field(default_factory=list)


def say(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def slug(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    value = parsed.path if parsed.scheme or "@" in source else source
    name = Path(value.rstrip("/")).name.removesuffix(".git") or "repository"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "repository"
    return f"{name}-{hashlib.sha256(source.encode()).hexdigest()[:8]}"


def sanitized_url(source: str) -> str:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in ("http", "https") or "@" not in parsed.netloc:
        return source
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[1], parsed.path, parsed.query, parsed.fragment)
    )


def is_remote(source: str) -> bool:
    return bool(re.match(r"^(https?://|ssh://|git://|file://|[^/@\s]+@[^:\s]+:)", source))


def destination_name(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    value = parsed.path if is_remote(source) else str(Path(source).expanduser().resolve())
    name = Path(value.rstrip("/")).name.removesuffix(".git")
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not clean:
        raise MigrationError(f"cannot derive a destination name from: {source}")
    return clean


def run(
    command: Sequence[str], *, cwd: Path, env: dict[str, str], log: Path,
    timeout: int, display: str | None = None,
) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    shown = display or shlex.join(command)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"\n$ {shown}\n")
        stream.flush()
        try:
            completed = subprocess.run(
                list(command), cwd=cwd, env=env, stdout=stream,
                stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MigrationError(f"timed out after {timeout}s: {shown}") from exc
    if completed.returncode:
        raise MigrationError(f"command failed ({completed.returncode}): {shown}; see {log}")


def capture(command: Sequence[str], cwd: Path) -> str:
    try:
        return subprocess.run(
            list(command), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=60, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MigrationError(f"command failed: {shlex.join(command)}") from exc


def read_manifest(path: Path) -> list[RepoSpec]:
    if not path.is_file():
        raise MigrationError(f"manifest does not exist: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise MigrationError("JSON manifest must be a list")
        result = []
        for item in data:
            if isinstance(item, str):
                result.append(RepoSpec(item))
            elif isinstance(item, dict) and item.get("url"):
                result.append(RepoSpec(str(item["url"]), item.get("ref") or item.get("branch")))
            else:
                raise MigrationError("JSON entries must be URLs or objects with a url field")
        return result
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        if rows and "url" not in rows[0]:
            raise MigrationError("CSV manifest requires a url column; ref is optional")
        return [RepoSpec(row["url"].strip(), (row.get("ref") or row.get("branch") or "").strip() or None)
                for row in rows if row.get("url", "").strip()]
    result = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        result.append(RepoSpec(parts[0], parts[1] if len(parts) == 2 else None))
    return result


def specs_from_args(args: argparse.Namespace) -> list[RepoSpec]:
    specs = [RepoSpec(item) for item in args.sources]
    if args.repo_path:
        specs.append(RepoSpec(args.repo_path))
    if args.manifest:
        specs.extend(read_manifest(args.manifest))
    unique = {(spec.source, spec.ref): spec for spec in specs}
    if not unique:
        raise MigrationError("provide a repository URL/path or --manifest")
    result = list(unique.values())
    destinations: dict[str, str] = {}
    for spec in result:
        name = destination_name(sanitized_url(spec.source))
        if name in destinations and destinations[name] != spec.source:
            raise MigrationError(
                f"destination name collision for '{name}': {destinations[name]} and {spec.source}; "
                "use separate --output directories"
            )
        destinations[name] = spec.source
    return result


def make_askpass(directory: Path) -> Path:
    path = directory / "git-askpass.sh"
    path.write_text(
        "#!/bin/sh\ncase \"$1\" in\n"
        "*sername*) printf '%s\\n' \"${MIGRATOR_GIT_USERNAME:-x-access-token}\" ;;\n"
        "*) printf '%s\\n' \"${MIGRATOR_GIT_TOKEN:-}\" ;;\nesac\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def prepare_repo(
    spec: RepoSpec, args: argparse.Namespace, env: dict[str, str], log: Path, askpass: Path,
) -> tuple[Path, bool]:
    source = sanitized_url(spec.source)
    path = args.output / destination_name(source)
    if path.exists() or path.is_symlink():
        if not args.force:
            raise SkipMigration(f"destination exists (use --force to replace it): {path}")
        resolved_output = args.output.resolve()
        resolved_path = path.resolve()
        if resolved_path.parent != resolved_output or resolved_path == resolved_output:
            raise MigrationError(f"refusing to replace unsafe destination: {resolved_path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not is_remote(source):
        local = Path(source).expanduser().resolve()
        if not local.is_dir():
            raise MigrationError(f"local directory does not exist: {local}")
        if path == local or local in path.parents and args.output == local:
            raise MigrationError("output directory must not resolve to the input directory")

        excluded_top_level = set()
        for generated in (args.output, args.workspace):
            try:
                relative = generated.relative_to(local)
                if relative.parts:
                    excluded_top_level.add(relative.parts[0])
            except ValueError:
                pass

        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in {"target", "build", ".gradle", "__pycache__"}}
            if Path(directory).resolve() == local:
                ignored.update(name for name in names if name in excluded_top_level)
            return ignored

        shutil.copytree(local, path, symlinks=True, ignore=ignore)
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    git_env = dict(env)
    git_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass)})
    command = ["git", "clone", "--no-tags"]
    if args.shallow:
        command += ["--depth", "1"]
    if args.submodules:
        command.append("--recurse-submodules")
        if args.shallow:
            command.append("--shallow-submodules")
    command += [source, str(path)]
    safe_command = command[:-2] + [source, str(path)]
    run(command, cwd=args.workspace, env=git_env, log=log, timeout=args.timeout,
        display=shlex.join(safe_command))
    if spec.ref:
        # A separate fetch supports branch names, tags, and raw commit SHAs.
        run(["git", "fetch", "--depth", "1", "origin", spec.ref], cwd=path,
            env=git_env, log=log, timeout=args.timeout)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=path,
            env=git_env, log=log, timeout=args.timeout)
        if args.submodules:
            run(["git", "submodule", "update", "--init", "--recursive"], cwd=path,
                env=git_env, log=log, timeout=args.timeout)
    return path, True


def check_clean(path: Path, allow_dirty: bool) -> None:
    if (path / ".git").exists() and capture(["git", "status", "--porcelain"], path) and not allow_dirty:
        raise MigrationError("repository has uncommitted changes; commit/stash them or pass --allow-dirty")


def discover_builds(root: Path, requested: str, max_depth: int) -> list[BuildRoot]:
    ignored = {".git", ".gradle", ".idea", ".migration-work", "build", "target", "node_modules"}
    candidates: list[BuildRoot] = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        dirs[:] = [] if depth >= max_depth else [name for name in dirs if name not in ignored and not name.startswith(".")]
        tools = []
        if "pom.xml" in files:
            tools.append("maven")
        if set(files) & {"settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"}:
            tools.append("gradle")
        for tool in tools:
            if requested == "auto" or requested == tool:
                candidates.append(BuildRoot(here, tool))
    # Nested POMs/build.gradle files are normally modules of the nearest same-tool root.
    roots = []
    for candidate in sorted(candidates, key=lambda item: len(item.path.parts)):
        if not any(existing.tool == candidate.tool and existing.path in candidate.path.parents for existing in roots):
            roots.append(candidate)
    if not roots:
        raise MigrationError(f"no {requested if requested != 'auto' else 'Maven or Gradle'} build found within depth {max_depth}")
    return roots


def tree_digest(root: Path) -> str:
    """Hash meaningful project files so non-Git directories get accurate change status."""
    digest = hashlib.sha256()
    ignored = {".git", ".gradle", ".idea", ".migration-work", "artifacts", "build", "target", "node_modules", "__pycache__"}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in ignored)
        here = Path(current)
        for name in sorted(files):
            path = here / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root)
            digest.update(str(relative).encode())
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
    return digest.hexdigest()


def artifacts(args: argparse.Namespace) -> list[str]:
    if args.artifact:
        return args.artifact
    result = [f"org.openrewrite.recipe:rewrite-migrate-java:{args.migrate_java_version}"]
    if args.cleanup:
        result.append(f"org.openrewrite.recipe:rewrite-static-analysis:{args.static_analysis_version}")
    if args.junit5:
        result.append(f"org.openrewrite.recipe:rewrite-testing-frameworks:{args.testing_frameworks_version}")
    if args.dependency_strategy != "none":
        result.append(f"org.openrewrite.recipe:rewrite-java-dependencies:{args.java_dependencies_version}")
    return result


def write_recipe(path: Path, tool: str, args: argparse.Namespace) -> str:
    name = "com.acme.migration.ModernizeJava"
    items = [
        f"org.openrewrite.java.migrate.UpgradeToJava{args.target_java}",
        "org.openrewrite.java.migrate.UpgradeDockerImageVersion:\n"
        f"      version: {args.target_java}",
    ]
    if args.cleanup:
        items.append("org.openrewrite.staticanalysis.CommonStaticAnalysis")
    if args.junit5:
        items.append("org.openrewrite.java.testing.junit5.JUnit4to5Migration")
    if args.build_best_practices:
        items.append("org.openrewrite.maven.BestPractices" if tool == "maven" else "org.openrewrite.gradle.GradleBestPractices")
    if args.dependency_strategy != "none":
        version = {"patch": "latest.patch", "latest": "latest.release"}[args.dependency_strategy]
        items.append(
            "org.openrewrite.java.dependencies.UpgradeDependencyVersion:\n"
            "      groupId: \"*\"\n      artifactId: \"*\"\n"
            f"      newVersion: {json.dumps(version)}"
        )
    items.extend(args.recipe)
    recipe_list = "\n".join(f"  - {item}" for item in items)
    path.write_text(
        "---\ntype: specs.openrewrite.org/v1beta/recipe\n"
        f"name: {name}\ndisplayName: Managed Java modernization\n"
        "description: Repeatable Java migration generated by java-migrator.\n"
        f"recipeList:\n{recipe_list}\n",
        encoding="utf-8",
    )
    return name


def write_gradle_init(path: Path, args: argparse.Namespace, env: dict[str, str]) -> None:
    dependencies = "\n".join(f'        rewrite("{item}")' for item in artifacts(args))
    credentials = ""
    if env.get("CODE_GENOME_USERNAME") and env.get("CODE_GENOME_TOKEN"):
        credentials = ' credentials { username = System.getenv("CODE_GENOME_USERNAME"); password = System.getenv("CODE_GENOME_TOKEN") }'
    path.write_text(
        "initscript {\n    repositories {\n"
        f'        maven {{ url = uri("{args.artifact_repository}");{credentials} }}\n'
        '        mavenCentral()\n        maven { url = uri("https://plugins.gradle.org/m2") }\n'
        f'    }}\n    dependencies {{ classpath("org.openrewrite:plugin:{args.gradle_plugin_version}") }}\n}}\n'
        "rootProject {\n    plugins.apply(org.openrewrite.gradle.RewritePlugin)\n    dependencies {\n"
        f"{dependencies}\n    }}\n    afterEvaluate {{\n"
        "        if (repositories.isEmpty()) { repositories { mavenCentral() } }\n"
        "        repositories {\n"
        f'            maven {{ url = uri("{args.artifact_repository}");{credentials} }}\n'
        "        }\n    }\n}\n",
        encoding="utf-8",
    )


def xml_name(parent: ET.Element, name: str) -> str:
    return f"{{{parent.tag.split('}', 1)[0][1:]}}}{name}" if parent.tag.startswith("{") else name


def xml_find(parent: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in parent if node.tag.rsplit("}", 1)[-1] == name), None)


def xml_get_or_add(parent: ET.Element, name: str) -> ET.Element:
    found = xml_find(parent, name)
    return found if found is not None else ET.SubElement(parent, xml_name(parent, name))


def xml_add(parent: ET.Element, name: str, text: str) -> ET.Element:
    node = ET.SubElement(parent, xml_name(parent, name))
    node.text = text
    return node


def write_maven_settings(path: Path, args: argparse.Namespace, env: dict[str, str]) -> None:
    source = args.maven_settings or Path.home() / ".m2" / "settings.xml"
    try:
        root = ET.parse(source).getroot() if source.is_file() else ET.Element("settings")
    except ET.ParseError as exc:
        raise MigrationError(f"invalid Maven settings {source}: {exc}") from exc
    username, token = env.get("CODE_GENOME_USERNAME"), env.get("CODE_GENOME_TOKEN")
    if username and token:
        servers = xml_get_or_add(root, "servers")
        for server in list(servers):
            identity = xml_find(server, "id")
            if identity is not None and identity.text == "codegenome":
                servers.remove(server)
        server = ET.SubElement(servers, xml_name(root, "server"))
        xml_add(server, "id", "codegenome")
        xml_add(server, "username", username)
        xml_add(server, "password", token)
    profiles = xml_get_or_add(root, "profiles")
    profile = ET.SubElement(profiles, xml_name(root, "profile"))
    xml_add(profile, "id", "java-migrator-codegenome")
    for collection_name, item_name in (("repositories", "repository"), ("pluginRepositories", "pluginRepository")):
        collection = ET.SubElement(profile, xml_name(root, collection_name))
        item = ET.SubElement(collection, xml_name(root, item_name))
        xml_add(item, "id", "codegenome")
        xml_add(item, "url", args.artifact_repository)
    active = xml_get_or_add(root, "activeProfiles")
    xml_add(active, "activeProfile", "java-migrator-codegenome")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def executable(build: BuildRoot) -> str:
    wrapper = build.path / ("mvnw" if build.tool == "maven" else "gradlew")
    if build.tool == "gradle" and wrapper.is_file():
        properties = build.path / "gradle" / "wrapper" / "gradle-wrapper.properties"
        if properties.is_file():
            match = re.search(r"gradle-(\d+)(?:\.\d+)*-", properties.read_text(encoding="utf-8", errors="ignore"))
            if match and int(match.group(1)) < 7:
                return "gradle"
    if wrapper.is_file():
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return str(wrapper)
    return "mvn" if build.tool == "maven" else "gradle"


def rewrite_command(
    build: BuildRoot, recipe: str, recipe_file: Path, settings: Path,
    init_script: Path, args: argparse.Namespace,
) -> list[str]:
    exe = executable(build)
    if build.tool == "maven":
        return [
            exe, "--batch-mode", "--no-transfer-progress", "-U", "-s", str(settings),
            f"org.openrewrite.maven:rewrite-maven-plugin:{args.maven_plugin_version}:run",
            f"-Drewrite.recipeArtifactCoordinates={','.join(artifacts(args))}",
            f"-Drewrite.activeRecipes={recipe}", f"-Drewrite.configLocation={recipe_file}",
            "-Drewrite.exportDatatables=true",
        ]
    return [
        exe, "--no-daemon", "--stacktrace", "--init-script", str(init_script), "rewriteRun",
        f"-Drewrite.activeRecipe={recipe}", f"-Drewrite.configLocation={recipe_file}",
        "-Drewrite.exportDatatables=true",
    ]


def verify_command(build: BuildRoot, level: str, settings: Path) -> list[str] | None:
    if level == "none":
        return None
    exe = executable(build)
    if build.tool == "maven":
        return [exe, "--batch-mode", "--no-transfer-progress", "-s", str(settings),
                "test" if level == "test" else "test-compile"]
    return [exe, "--no-daemon", "test" if level == "test" else "classes"]


def manual_review_files(root: Path) -> list[str]:
    """Surface likely stale, organization-specific material without deleting it."""
    name_pattern = re.compile(r"(?:java[-_. ]?8|jdk[-_. ]?8|obsolete|\.old\b|old[-_.])", re.IGNORECASE)
    content_pattern = re.compile(r"(?:\bJava\s*8\b|\bjdk1?\.?8\b|openjdk:8|sourceCompatibility\s*=\s*1\.8)", re.IGNORECASE)
    text_config_suffixes = {"", ".gradle", ".kts", ".md", ".properties", ".txt", ".xml", ".yaml", ".yml"}
    ignored = {".git", ".gradle", "build", "target", "node_modules"}
    found: set[str] = set()
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ignored]
        here = Path(current)
        for name in files:
            path = here / name
            relative = str(path.relative_to(root))
            if name_pattern.search(name):
                found.add(relative)
                continue
            try:
                if (path.suffix.lower() in text_config_suffixes and path.stat().st_size <= 512_000
                        and content_pattern.search(path.read_text(encoding="utf-8", errors="ignore"))):
                    found.add(relative)
            except OSError:
                pass
            if len(found) >= 100:
                return sorted(found)
    return sorted(found)


def checkout_branch(repo: Path, args: argparse.Namespace, env: dict[str, str], log: Path) -> str:
    if not (repo / ".git").exists() or not args.branch:
        return ""
    branch = args.branch.format(java=args.target_java)
    if capture(["git", "branch", "--show-current"], repo) == branch:
        return branch
    exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo).returncode == 0
    run(["git", "switch", branch] if exists else ["git", "switch", "-c", branch],
        cwd=repo, env=env, log=log, timeout=args.timeout)
    return branch


def migrate_project(
    build: BuildRoot, args: argparse.Namespace, env: dict[str, str], log: Path, temp: Path,
) -> ProjectResult:
    result = ProjectResult(str(build.path), build.tool)
    git_managed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=build.path,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    before = capture(["git", "status", "--porcelain"], build.path) if git_managed else tree_digest(build.path)
    try:
        recipe_file = temp / f"rewrite-{build.tool}.yml"
        settings = temp / "settings.xml"
        init_script = temp / "init.gradle"
        recipe = write_recipe(recipe_file, build.tool, args)
        write_maven_settings(settings, args, env)
        write_gradle_init(init_script, args, env)
        command = rewrite_command(build, recipe, recipe_file, settings, init_script, args)
        if args.dry_run:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"Would run in {build.path}: {shlex.join(command)}\n")
            result.status = "planned"
        else:
            run(command, cwd=build.path, env=env, log=log, timeout=args.timeout)
            verify = verify_command(build, args.verify, settings)
            if verify:
                run(verify, cwd=build.path, env=env, log=log, timeout=args.timeout)
            after = capture(["git", "status", "--porcelain"], build.path) if git_managed else tree_digest(build.path)
            result.changed = after != before
            result.status = "changed" if result.changed else "unchanged"
    except Exception as exc:
        result.error = str(exc)
    result.manual_review = manual_review_files(build.path)
    return result


def migrate_one(spec: RepoSpec, args: argparse.Namespace, env: dict[str, str], askpass: Path) -> Result:
    started = time.monotonic()
    source = sanitized_url(spec.source)
    name = slug(source)
    log = args.workspace / "logs" / f"{name}.log"
    result = Result(source=source, log=str(log))
    result.path = str(args.output / destination_name(source))
    try:
        say(f"[{name}] preparing {source}")
        repo, cloned = prepare_repo(spec, args, env, log, askpass)
        result.path = str(repo)
        check_clean(repo, args.allow_dirty)
        builds = discover_builds(repo, args.build_tool, args.max_depth)
        result.branch = checkout_branch(repo, args, env, log)
        state = args.workspace / ".state"
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=state) as temp_name:
            for index, build in enumerate(builds):
                say(f"[{name}] {build.tool}: {build.path.relative_to(repo)}")
                project_temp = Path(temp_name) / str(index)
                project_temp.mkdir()
                project = migrate_project(build, args, env, log, project_temp)
                result.projects.append(project)
                if project.status == "failed" and not args.continue_projects:
                    break
        failures = [project for project in result.projects if project.status == "failed"]
        if failures:
            raise MigrationError("; ".join(f"{item.path}: {item.error}" for item in failures))
        result.changed = any(project.changed for project in result.projects)
        if (repo / ".git").exists():
            result.diff_stat = capture(["git", "diff", "--stat", "HEAD"], repo)
            if result.changed and args.commit:
                run(["git", "add", "--all"], cwd=repo, env=env, log=log, timeout=args.timeout)
                run(["git", "commit", "-m", args.commit_message.format(java=args.target_java)],
                    cwd=repo, env=env, log=log, timeout=args.timeout)
                result.commit = capture(["git", "rev-parse", "HEAD"], repo)
                if args.push:
                    if not cloned:
                        say(f"[{name}] warning: pushing from a local input directory")
                    git_env = dict(env)
                    git_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass)})
                    run(["git", "push", "--set-upstream", "origin", result.branch],
                        cwd=repo, env=git_env, log=log, timeout=args.timeout)
        result.status = "planned" if args.dry_run else ("changed" if result.changed else "unchanged")
    except SkipMigration as exc:
        result.status = "skipped"
        result.error = str(exc)
    except Exception as exc:
        result.error = str(exc)
    finally:
        result.duration_seconds = round(time.monotonic() - started, 2)
        report = args.workspace / "reports" / f"{name}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(dataclasses.asdict(result), indent=2) + "\n", encoding="utf-8")
        say(f"[{name}] {result.status} ({result.duration_seconds}s){': ' + result.error if result.error else ''}")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone and modernize Maven/Gradle Java repositories with OpenRewrite.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("sources", nargs="*", help="Git URLs or local directories")
    parser.add_argument("--repo-path", help="alias for one local directory")
    parser.add_argument("--manifest", type=Path, help="TXT, CSV (url,ref), or JSON repository list")
    parser.add_argument("--output", type=Path, default=Path("artifacts"),
                        help="updated repository/directory copies")
    parser.add_argument("--workspace", type=Path, default=Path(".migration-work"),
                        help="logs, reports, and temporary state")
    parser.add_argument("--target-java", type=int, choices=TARGETS, default=21)
    parser.add_argument("--build-tool", choices=("auto", "maven", "gradle"), default="auto")
    parser.add_argument("--max-depth", type=int, default=4, help="maximum build-root discovery depth")
    parser.add_argument("--jobs", type=int, default=1, help="repositories migrated concurrently")
    parser.add_argument("--continue-projects", action="store_true", help="continue other builds after one fails")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per external command")
    parser.add_argument("--verify", choices=("none", "compile", "test"), default="test")
    parser.add_argument("--dependency-strategy", choices=("none", "patch", "latest"), default="patch")
    parser.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--junit5", action=argparse.BooleanOptionalAction, default=True,
                        help="migrate JUnit 4 tests and build dependencies to JUnit 5")
    parser.add_argument("--build-best-practices", action=argparse.BooleanOptionalAction, default=False,
                        help="can make major build-tool changes (for example Gradle 9)")
    parser.add_argument("--recipe", action="append", default=[], help="extra recipe; repeatable")
    parser.add_argument("--artifact", action="append", default=[], help="override G:A:V recipe artifacts")
    parser.add_argument("--branch", default="automation/java-{java}", help="empty disables branch creation")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--commit-message", default="Migrate to Java {java}")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--force", action="store_true", help="replace an existing output destination")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--shallow", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--submodules", action=argparse.BooleanOptionalAction, default=True,
                        help="clone private/public Git submodules as well")
    parser.add_argument("--dry-run", action="store_true", help="clone, discover, and print commands only")
    parser.add_argument("--git-token-env", default="GIT_TOKEN")
    parser.add_argument("--git-username", default="x-access-token")
    parser.add_argument("--maven-settings", type=Path)
    parser.add_argument("--artifact-repository", default=CODE_GENOME_URL,
                        help="Code Genome endpoint or an organization Maven mirror")
    parser.add_argument("--maven-plugin-version", default=VERSIONS["maven_plugin"])
    parser.add_argument("--gradle-plugin-version", default=VERSIONS["gradle_plugin"])
    parser.add_argument("--migrate-java-version", default=VERSIONS["migrate_java"])
    parser.add_argument("--static-analysis-version", default=VERSIONS["static_analysis"])
    parser.add_argument("--java-dependencies-version", default=VERSIONS["java_dependencies"])
    parser.add_argument("--testing-frameworks-version", default=VERSIONS["testing_frameworks"])
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout < 1 or args.max_depth < 0:
        parser.error("--jobs and --timeout must be positive; --max-depth cannot be negative")
    if args.push and (not args.commit or not args.branch):
        parser.error("--push requires --commit and a non-empty --branch")
    args.workspace = args.workspace.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        specs = specs_from_args(args)
        args.workspace.mkdir(parents=True, exist_ok=True)
        args.output.mkdir(parents=True, exist_ok=True)
        state = args.workspace / ".state"
        state.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["MIGRATOR_GIT_TOKEN"] = env.get(args.git_token_env, "")
        env["MIGRATOR_GIT_USERNAME"] = args.git_username
        with tempfile.TemporaryDirectory(prefix="java-migrator-", dir=state) as temp:
            askpass = make_askpass(Path(temp))
            say(f"Migrating {len(specs)} repository(s) to Java {args.target_java} with {args.jobs} worker(s)")
            if args.jobs == 1:
                results = [migrate_one(spec, args, env, askpass) for spec in specs]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    results = list(pool.map(lambda spec: migrate_one(spec, args, env, askpass), specs))
        summary = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "target_java": args.target_java,
            "counts": {status: sum(item.status == status for item in results)
                       for status in ("changed", "unchanged", "planned", "skipped", "failed")},
            "results": [dataclasses.asdict(item) for item in results],
        }
        output = args.workspace / "reports" / "summary.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        say(f"Summary: {output} ({summary['counts']['failed']} failed)")
        return 1 if summary["counts"]["failed"] else 0
    except (MigrationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
