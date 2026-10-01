#!/usr/bin/env python3
"""Batch Java repository modernization with OpenRewrite."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import dataclasses
import datetime as dt
import fnmatch
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
from typing import Any, Sequence


MAVEN_CENTRAL_VERSIONS = {
    "maven_plugin": "6.46.1",
    # 7.40+ depends on rewrite-bom 8.91+, which is Code Genome-only.
    "gradle_plugin": "7.39.0",
    "migrate_java": "3.42.1",
    "static_analysis": "2.41.1",
    "java_dependencies": "1.60.2",
    "testing_frameworks": "3.44.0",
}
CODE_GENOME_VERSIONS = {
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
DEFAULT_EXCLUSIONS = (
    "**/generated/**",
    "**/generated-sources/**",
    "**/target/generated-sources/**",
    "**/build/generated/**",
    "**/node_modules/**",
    "**/vendor/**",
)
PROFILE_DEFAULTS = {
    "conservative": {
        "cleanup": False,
        "testing_modernization": "junit",
        "dependency_strategy": "none",
        "build_best_practices": False,
        "post_checks": "jdk",
    },
    "standard": {
        "cleanup": True,
        "testing_modernization": "standard",
        "dependency_strategy": "patch",
        "build_best_practices": False,
        "post_checks": "jdk",
    },
    "aggressive": {
        "cleanup": True,
        "testing_modernization": "aggressive",
        "dependency_strategy": "latest",
        "build_best_practices": True,
        "post_checks": "all",
    },
    "report-only": {
        "cleanup": False,
        "testing_modernization": "none",
        "dependency_strategy": "none",
        "build_best_practices": False,
        "post_checks": "none",
    },
}


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


@dataclasses.dataclass(frozen=True, order=True)
class Dependency:
    group: str
    artifact: str
    version: str
    configuration: str = ""

    @property
    def coordinate(self) -> str:
        return f"{self.group}:{self.artifact}"


@dataclasses.dataclass
class ProjectAnalysis:
    features: list[str] = dataclasses.field(default_factory=list)
    dependencies: list[Dependency] = dataclasses.field(default_factory=list)
    findings: list[str] = dataclasses.field(default_factory=list)
    excluded_paths: list[str] = dataclasses.field(default_factory=list)
    external_configuration: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class MigrationPhase:
    name: str
    recipes: tuple[str, ...]


@dataclasses.dataclass
class PhaseResult:
    name: str
    status: str
    changed: bool = False
    recipes: list[str] = dataclasses.field(default_factory=list)
    error: str = ""


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str
    command: list[str] = dataclasses.field(default_factory=list)
    returncode: int | None = None
    output: str = ""


@dataclasses.dataclass
class ProjectResult:
    path: str
    build_tool: str
    status: str = "failed"
    changed: bool = False
    error: str = ""
    manual_review: list[str] = dataclasses.field(default_factory=list)
    analysis: ProjectAnalysis = dataclasses.field(default_factory=ProjectAnalysis)
    phases: list[PhaseResult] = dataclasses.field(default_factory=list)
    checks: list[CheckResult] = dataclasses.field(default_factory=list)


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


def load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    path = path.expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"policy file does not exist: {path}")
    try:
        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(content)
        else:
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError as exc:
                raise MigrationError(
                    "YAML policies require PyYAML (install python3-yaml), or use a JSON policy"
                ) from exc
            data = yaml.safe_load(content)
    except (OSError, ValueError, TypeError) as exc:
        raise MigrationError(f"invalid policy file {path}: {exc}") from exc
    except Exception as exc:
        # PyYAML's parser errors do not inherit from ValueError.
        raise MigrationError(f"invalid policy file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MigrationError("policy root must be a mapping/object")
    if data.get("version", 1) != 1:
        raise MigrationError("unsupported policy version; expected version: 1")
    return data


def policy_list(policy: dict[str, Any], *keys: str) -> list[str]:
    value: Any = policy
    for key in keys:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    if value in ({}, None):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MigrationError(f"policy {'.'.join(keys)} must be a list of strings")
    return list(value)


def policy_mapping(policy: dict[str, Any], *keys: str) -> dict[str, str]:
    value: Any = policy
    for key in keys:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    if value in ({}, None):
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in value.items()):
        raise MigrationError(f"policy {'.'.join(keys)} must be a string-to-string mapping")
    return dict(value)


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


@contextlib.contextmanager
def isolate_from_parent_git(root: Path):
    """Stop build plugins from applying an ancestor repository's ignore rules."""
    marker = root / ".git"
    ancestor_has_git = any((parent / ".git").exists() for parent in root.parents)
    created = not marker.exists() and ancestor_has_git
    if created:
        # OpenRewrite stops searching for a repository at this marker. Because it
        # is intentionally not a valid repository, the copied project is parsed
        # without treating an ignored artifacts/ parent as an exclusion.
        marker.write_text("temporary java-migrator repository boundary\n", encoding="utf-8")
    try:
        yield
    finally:
        if created:
            marker.unlink(missing_ok=True)


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


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def maven_dependencies(path: Path) -> list[Dependency]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    properties: dict[str, str] = {}
    for node in root.iter():
        if local_name(node) == "properties":
            properties.update({local_name(child): (child.text or "").strip() for child in node})

    result: list[Dependency] = []
    for dependency in root.iter():
        if local_name(dependency) != "dependency":
            continue
        ancestor = parents.get(dependency)
        managed = False
        while ancestor is not None:
            if local_name(ancestor) == "dependencyManagement":
                managed = True
                break
            ancestor = parents.get(ancestor)
        if managed:
            continue
        values = {local_name(child): (child.text or "").strip() for child in dependency}
        group, artifact, version = values.get("groupId", ""), values.get("artifactId", ""), values.get("version", "")
        if not group or not artifact or not version:
            continue
        property_match = re.fullmatch(r"\$\{([^}]+)}", version)
        if property_match:
            version = properties.get(property_match.group(1), "")
        if version and not version.startswith("${"):
            result.append(Dependency(group, artifact, version, values.get("scope", "compile")))
    return result


def gradle_dependencies(path: Path) -> list[Dependency]:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    pattern = re.compile(
        r"(?m)^\s*([A-Za-z][\w]*)\s*(?:\(|\s)\s*['\"]"
        r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^'\"$\s]+)['\"]"
    )
    return [Dependency(group, artifact, version, configuration)
            for configuration, group, artifact, version in pattern.findall(content)]


def path_is_excluded(relative: str, exclusions: Sequence[str]) -> bool:
    unix = relative.replace(os.sep, "/")
    return any(fnmatch.fnmatch(unix, pattern) or fnmatch.fnmatch(f"/{unix}", pattern)
               for pattern in exclusions)


def external_configuration_files(root: Path, exclusions: Sequence[str]) -> list[str]:
    """Find Java/build controls that recipes may not own or fully understand."""
    exact_names = {
        ".java-version", ".sdkmanrc", ".tool-versions", ".gitlab-ci.yml",
        "azure-pipelines.yml", "buildspec.yml", "jenkinsfile", "maven.config",
        "jvm.config", "gradle.properties", "renovate.json",
    }
    ignored = {".git", ".gradle", "build", "target", "node_modules", "vendor"}
    found: set[str] = set()
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in files:
            path = here / name
            relative = str(path.relative_to(root)).replace(os.sep, "/")
            lowered = name.lower()
            is_candidate = (
                lowered in exact_names
                or lowered.startswith("dockerfile")
                or relative.startswith(".github/workflows/")
                or relative.startswith(".circleci/")
                or relative.startswith(".mvn/")
                or relative.startswith(".devcontainer/")
            )
            if is_candidate and not path_is_excluded(relative, exclusions):
                found.add(relative)
    return sorted(found)


def analyze_project(build: BuildRoot, args: argparse.Namespace) -> ProjectAnalysis:
    dependencies: set[Dependency] = set()
    build_files: list[Path] = []
    if build.tool == "maven":
        build_files = list(build.path.rglob("pom.xml"))
        for path in build_files:
            dependencies.update(maven_dependencies(path))
    else:
        build_files = [path for pattern in ("build.gradle", "build.gradle.kts")
                       for path in build.path.rglob(pattern)]
        for path in build_files:
            dependencies.update(gradle_dependencies(path))

    feature_coordinates = {
        "spring": ("org.springframework",),
        "spring-boot": ("org.springframework.boot",),
        "javaee": ("javax", "javax.servlet", "javax.persistence", "javax.validation"),
        "jakarta": ("jakarta", "jakarta.platform"),
        "lombok": ("org.projectlombok:lombok",),
        "mapstruct": ("org.mapstruct",),
        "mockito": ("org.mockito",),
        "junit": ("junit:junit", "org.junit"),
        "powermock": ("org.powermock",),
        "jmockit": ("org.jmockit",),
        "testng": ("org.testng",),
        "log4j-1": ("log4j:log4j",),
        "guava": ("com.google.guava:guava",),
        "android": ("com.android",),
        "kotlin": ("org.jetbrains.kotlin",),
        "scala": ("org.scala-lang",),
    }
    features: set[str] = set()
    for dependency in dependencies:
        coordinate = dependency.coordinate
        for feature, prefixes in feature_coordinates.items():
            if any(coordinate == prefix or coordinate.startswith(prefix + ":")
                   or dependency.group == prefix or dependency.group.startswith(prefix + ".")
                   for prefix in prefixes):
                features.add(feature)

    source_patterns = {
        "javaee": re.compile(r"\bjavax\.(?:activation|annotation|ejb|enterprise|inject|jms|mail|persistence|servlet|transaction|validation|ws\.rs|xml\.bind|xml\.ws)\b"),
        "removed-jdk-modules": re.compile(r"\b(?:javax\.xml\.(?:bind|ws)|javax\.activation|org\.omg\.|jdk\.nashorn\.)"),
        "jdk-internals": re.compile(r"\b(?:sun\.|com\.sun\.|jdk\.internal\.)"),
        "security-manager": re.compile(r"\b(?:SecurityManager|System\.getSecurityManager|AccessController)\b"),
        "finalization": re.compile(r"\b(?:System\.runFinalization|Runtime\.runFinalizersOnExit|void\s+finalize\s*\()"),
        "powermock": re.compile(r"\borg\.powermock\b"),
        "mockito": re.compile(r"\borg\.mockito\b"),
        "junit": re.compile(r"\borg\.junit\b"),
        "lombok": re.compile(r"\blombok\."),
        "spring": re.compile(r"\borg\.springframework\."),
    }
    ignored_dirs = {".git", ".gradle", "build", "target", "node_modules", "vendor", "__pycache__"}
    excluded_paths: set[str] = set()
    for current, dirs, files in os.walk(build.path):
        here = Path(current)
        kept = []
        for name in dirs:
            child = here / name
            relative = str(child.relative_to(build.path)).replace(os.sep, "/")
            if name in ignored_dirs or path_is_excluded(relative + "/", args.exclusions):
                if name not in ignored_dirs:
                    excluded_paths.add(relative)
            else:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            path = here / name
            if path.suffix.lower() not in {".java", ".kt", ".scala", ".gradle", ".kts", ".xml", ".properties", ".yml", ".yaml"}:
                continue
            relative = str(path.relative_to(build.path)).replace(os.sep, "/")
            if path_is_excluded(relative, args.exclusions):
                excluded_paths.add(relative)
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for feature, pattern in source_patterns.items():
                if pattern.search(content):
                    features.add(feature)
            if path.suffix == ".kt":
                features.add("kotlin")
            elif path.suffix == ".scala":
                features.add("scala")

    findings: list[str] = []
    finding_messages = {
        "removed-jdk-modules": "Uses APIs removed from the JDK (for example JAXB/JAX-WS/CORBA/Nashorn); verify replacement dependencies and runtime behavior.",
        "jdk-internals": "Uses internal JDK APIs; replace them or document narrowly scoped --add-opens/--add-exports flags.",
        "security-manager": "Uses SecurityManager/AccessController APIs whose behavior changed after Java 8; review security assumptions.",
        "finalization": "Uses finalization APIs; migrate resource cleanup to AutoCloseable/Cleaner and verify lifecycle behavior.",
        "spring": "Spring detected; choose an explicit Spring/Spring Boot target before enabling framework recipes.",
        "javaee": "Java EE javax APIs detected; Jakarta namespace migration is intentionally opt-in.",
        "powermock": "PowerMock detected; aggressive Mockito conversion requires manual review of static/constructor/private mocking.",
        "jmockit": "JMockit detected; verify Java agent flags and consider the JMockit-to-Mockito recipe.",
        "testng": "TestNG detected; the JUnit migration pack does not convert TestNG suites, listeners, or XML configuration.",
        "log4j-1": "Log4j 1.x detected; plan a logging migration rather than a blind version update.",
        "android": "Android build detected; do not assume the normal Java 21/Gradle migration policy is compatible.",
        "kotlin": "Kotlin sources/plugins detected; align Kotlin jvmTarget and plugin compatibility with the target JDK.",
        "scala": "Scala detected; verify the Scala compiler and binary version support the target JDK.",
    }
    for feature in sorted(features):
        if feature in finding_messages:
            findings.append(finding_messages[feature])
    if "lombok" in features and "mapstruct" in features:
        findings.append("Lombok and MapStruct detected; enable annotation-processor binding during compatibility migration.")
    external_configuration = external_configuration_files(build.path, args.exclusions)
    if external_configuration:
        findings.append(
            "External CI/toolchain configuration requires review: "
            + ", ".join(external_configuration[:12])
            + (" ..." if len(external_configuration) > 12 else "")
        )
    return ProjectAnalysis(
        features=sorted(features),
        dependencies=sorted(dependencies),
        findings=findings,
        excluded_paths=sorted(excluded_paths),
        external_configuration=external_configuration,
    )


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
    if args.profile == "report-only":
        return []
    if args.artifact:
        return args.artifact
    result = [f"org.openrewrite.recipe:rewrite-migrate-java:{args.migrate_java_version}"]
    if args.cleanup:
        result.append(f"org.openrewrite.recipe:rewrite-static-analysis:{args.static_analysis_version}")
    if args.testing_modernization != "none":
        result.append(f"org.openrewrite.recipe:rewrite-testing-frameworks:{args.testing_frameworks_version}")
    if args.dependency_strategy != "none":
        result.append(f"org.openrewrite.recipe:rewrite-java-dependencies:{args.java_dependencies_version}")
    return result


def dependency_recipes(analysis: ProjectAnalysis, args: argparse.Namespace) -> list[str]:
    if args.dependency_strategy == "none":
        return []
    default_version = {"patch": "latest.patch", "latest": "latest.release"}[args.dependency_strategy]
    coordinated_groups = (
        "org.springframework", "org.hibernate", "io.quarkus", "io.micronaut",
        "com.fasterxml.jackson", "junit", "org.junit", "org.mockito", "net.bytebuddy",
        "org.powermock", "org.jmockit", "log4j",
    )
    recipes: list[str] = []
    seen: set[str] = set()
    for dependency in analysis.dependencies:
        coordinate = dependency.coordinate
        if coordinate in seen:
            continue
        seen.add(coordinate)
        if any(fnmatch.fnmatch(coordinate, pattern) for pattern in args.dependency_deny):
            continue
        pinned = next((version for pattern, version in args.dependency_pin.items()
                       if fnmatch.fnmatch(coordinate, pattern)), None)
        if pinned is None and dependency.group.startswith(coordinated_groups):
            continue
        new_version = pinned or default_version
        recipes.append(
            "org.openrewrite.java.dependencies.UpgradeDependencyVersion:\n"
            f"      groupId: {json.dumps(dependency.group)}\n"
            f"      artifactId: {json.dumps(dependency.artifact)}\n"
            f"      newVersion: {json.dumps(new_version)}"
        )
    return recipes


def junit_launcher_recipe() -> str:
    return (
        "org.openrewrite.java.dependencies.AddDependency:\n"
        "      groupId: org.junit.platform\n"
        "      artifactId: junit-platform-launcher\n"
        "      version: 1.x\n"
        "      configuration: testRuntimeOnly\n"
        "      onlyIfUsing: org.junit.jupiter.api.*\n"
        "      acceptTransitive: false"
    )


def migration_phases(build: BuildRoot, analysis: ProjectAnalysis, args: argparse.Namespace) -> list[MigrationPhase]:
    if args.profile == "report-only":
        return []
    phases: list[MigrationPhase] = [MigrationPhase("java", (
        f"org.openrewrite.java.migrate.UpgradeToJava{args.target_java}",
        "org.openrewrite.java.migrate.UpgradeDockerImageVersion:\n"
        f"      version: {args.target_java}",
    ))]

    compatibility: list[str] = []
    if args.jakarta != "none":
        compatibility.append({
            "9": "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
            "10": "org.openrewrite.java.migrate.jakarta.JakartaEE10",
            "11": "org.openrewrite.java.migrate.jakarta.JakartaEE11",
        }[args.jakarta])
    if "lombok" in analysis.features and "mapstruct" in analysis.features:
        compatibility.append("org.openrewrite.java.migrate.AddLombokMapstructBinding")
    if args.lombok_best_practices and "lombok" in analysis.features:
        compatibility.append("org.openrewrite.java.migrate.lombok.LombokBestPractices")
    if compatibility:
        phases.append(MigrationPhase("compatibility", tuple(compatibility)))

    if args.testing_modernization != "none" and ({"junit", "mockito"} & set(analysis.features)):
        test_migration = ["org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
        if args.testing_modernization in {"standard", "aggressive"} and "mockito" in analysis.features:
            test_migration.append("org.openrewrite.java.testing.mockito.Mockito4to5Only")
        phases.append(MigrationPhase("testing-migration", tuple(test_migration)))

        test_cleanup: list[str] = ["org.openrewrite.java.testing.junit5.JUnit5BestPractices"]
        if build.tool == "gradle":
            test_cleanup.append(junit_launcher_recipe())
        if args.testing_modernization == "aggressive" and "mockito" in analysis.features:
            test_cleanup.append("org.openrewrite.java.testing.mockito.MockitoBestPractices")
        phases.append(MigrationPhase("testing-cleanup", tuple(test_cleanup)))

    dependency_changes = dependency_recipes(analysis, args)
    if dependency_changes:
        phases.append(MigrationPhase("dependencies", tuple(dependency_changes)))

    cleanup: list[str] = []
    if args.cleanup:
        cleanup.append("org.openrewrite.staticanalysis.CommonStaticAnalysis")
    if args.build_best_practices:
        cleanup.append("org.openrewrite.maven.BestPractices" if build.tool == "maven"
                       else "org.openrewrite.gradle.GradleBestPractices")
    if args.profile == "aggressive" and args.target_java >= 21 and "guava" in analysis.features:
        cleanup.append("org.openrewrite.java.migrate.guava.NoGuavaJava21")
    if cleanup:
        phases.append(MigrationPhase("cleanup", tuple(cleanup)))
    if args.recipe:
        phases.append(MigrationPhase("custom", tuple(args.recipe)))
    return phases


def remote_recipe_repository(args: argparse.Namespace) -> str | None:
    """Return the explicitly configured recipe repository, if one is needed."""
    if args.artifact_repository:
        return args.artifact_repository
    if args.recipe_repository == "codegenome":
        return CODE_GENOME_URL
    return None


def gradle_repositories(args: argparse.Namespace, indent: str) -> str:
    """Generate repository declarations without writing credential values to disk."""
    repositories: list[str] = []
    if args.recipe_repository == "maven-local":
        repositories.append(f"{indent}mavenLocal()")
    remote = remote_recipe_repository(args)
    if remote:
        credentials = ""
        if args.recipe_repository == "codegenome":
            credentials = (
                ' credentials { username = System.getenv("CODE_GENOME_USERNAME"); '
                'password = System.getenv("CODE_GENOME_TOKEN") }'
            )
        repositories.append(f'{indent}maven {{ url = uri("{remote}");{credentials} }}')
    repositories.append(f"{indent}mavenCentral()")
    return "\n".join(repositories)


def write_recipe(path: Path, phase: MigrationPhase) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]", "", phase.name.title())
    name = f"com.acme.migration.{suffix}"
    recipe_list = "\n".join(f"  - {item}" for item in phase.recipes)
    path.write_text(
        "---\ntype: specs.openrewrite.org/v1beta/recipe\n"
        f"name: {name}\ndisplayName: Managed Java modernization ({phase.name})\n"
        f"description: Generated {phase.name} phase for a repeatable Java migration.\n"
        f"recipeList:\n{recipe_list}\n",
        encoding="utf-8",
    )
    return name


def write_gradle_init(
    path: Path, args: argparse.Namespace, recipe: str, recipe_file: Path,
) -> None:
    dependencies = "\n".join(f'        rewrite("{item}")' for item in artifacts(args))
    init_repositories = gradle_repositories(args, "        ")
    project_repositories = gradle_repositories(args, "            ")
    exclusions = ""
    if args.exclusions:
        values = ", ".join(json.dumps(pattern) for pattern in args.exclusions)
        exclusions = f"        exclusion({values})\n"
    path.write_text(
        "initscript {\n    repositories {\n"
        f"{init_repositories}\n"
        '        maven { url = uri("https://plugins.gradle.org/m2") }\n'
        f'    }}\n    dependencies {{ classpath("org.openrewrite:plugin:{args.gradle_plugin_version}") }}\n}}\n'
        "rootProject {\n    plugins.apply(org.openrewrite.gradle.RewritePlugin)\n    dependencies {\n"
        f"{dependencies}\n    }}\n    rewrite {{\n"
        f"        activeRecipe({json.dumps(recipe)})\n"
        f"        configFile = file({json.dumps(str(recipe_file))})\n"
        f"{exclusions}"
        "        setExportDatatables(true)\n"
        "    }\n    afterEvaluate {\n"
        "        repositories {\n"
        f"{project_repositories}\n"
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
    remote = remote_recipe_repository(args)
    if not remote:
        if source.is_file():
            shutil.copyfile(source, path)
        else:
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<settings/>\n', encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return
    try:
        root = ET.parse(source).getroot() if source.is_file() else ET.Element("settings")
    except ET.ParseError as exc:
        raise MigrationError(f"invalid Maven settings {source}: {exc}") from exc
    repository_id = "codegenome" if args.recipe_repository == "codegenome" else "java-migrator-recipes"
    username, token = env.get("CODE_GENOME_USERNAME"), env.get("CODE_GENOME_TOKEN")
    if args.recipe_repository == "codegenome" and username and token:
        servers = xml_get_or_add(root, "servers")
        for server in list(servers):
            identity = xml_find(server, "id")
            if identity is not None and identity.text == repository_id:
                servers.remove(server)
        server = ET.SubElement(servers, xml_name(root, "server"))
        xml_add(server, "id", repository_id)
        xml_add(server, "username", username)
        xml_add(server, "password", token)
    profiles = xml_get_or_add(root, "profiles")
    profile = ET.SubElement(profiles, xml_name(root, "profile"))
    profile_id = "java-migrator-recipes"
    xml_add(profile, "id", profile_id)
    for collection_name, item_name in (("repositories", "repository"), ("pluginRepositories", "pluginRepository")):
        collection = ET.SubElement(profile, xml_name(root, collection_name))
        item = ET.SubElement(collection, xml_name(root, item_name))
        xml_add(item, "id", repository_id)
        xml_add(item, "url", remote)
    active = xml_get_or_add(root, "activeProfiles")
    xml_add(active, "activeProfile", profile_id)
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
        ] + ([f"-Drewrite.exclusions={','.join(args.exclusions)}"] if args.exclusions else [])
    return [
        exe, "--no-daemon", "--stacktrace", "--init-script", str(init_script), "rewriteRun",
    ]


def verify_command(build: BuildRoot, level: str, settings: Path) -> list[str] | None:
    if level == "none":
        return None
    exe = executable(build)
    if build.tool == "maven":
        return [exe, "--batch-mode", "--no-transfer-progress", "-s", str(settings),
                "test" if level == "test" else "test-compile"]
    return [exe, "--no-daemon", "test" if level == "test" else "classes"]


def diagnostic_command(
    name: str, command: Sequence[str], build: BuildRoot, env: dict[str, str],
    args: argparse.Namespace, *, warn_on_output: bool = False,
) -> CheckResult:
    executable_path = shutil.which(command[0], path=env.get("PATH"))
    if executable_path is None:
        return CheckResult(name, "skipped", list(command), output=f"{command[0]} is not installed")
    try:
        completed = subprocess.run(
            list(command), cwd=build.path, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=min(args.timeout, 600), check=False,
        )
        output = completed.stdout[-12_000:]
        warning = completed.returncode != 0 or (warn_on_output and bool(output.strip()))
        return CheckResult(name, "warning" if warning else "passed", list(command),
                           completed.returncode, output)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return CheckResult(name, "warning", list(command), output=output[-12_000:] + "\nTimed out")


def run_post_checks(
    build: BuildRoot, settings: Path, env: dict[str, str], args: argparse.Namespace,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if args.post_checks in {"jdk", "all"}:
        candidates = ([build.path / "target" / "classes"] if build.tool == "maven" else
                      [build.path / "build" / "classes" / "java" / "main"])
        classes = next((path for path in candidates if path.is_dir()), None)
        if classes is None:
            checks.append(CheckResult("jdk-internals", "skipped", output="compiled classes directory not found"))
            checks.append(CheckResult("deprecated-for-removal", "skipped", output="compiled classes directory not found"))
        else:
            checks.append(diagnostic_command(
                "jdk-internals", ["jdeps", "--recursive", "--jdk-internals", str(classes)],
                build, env, args, warn_on_output=True,
            ))
            checks.append(diagnostic_command(
                "deprecated-for-removal",
                ["jdeprscan", "--release", str(args.target_java), "--for-removal", str(classes)],
                build, env, args, warn_on_output=True,
            ))
    if args.post_checks == "all":
        if build.tool == "maven":
            command = [executable(build), "--batch-mode", "--no-transfer-progress", "-s", str(settings),
                       "dependency:tree"]
        else:
            command = [executable(build), "--no-daemon", "dependencies"]
        checks.append(diagnostic_command("dependency-report", command, build, env, args))
    for index, value in enumerate(args.verify_command, 1):
        try:
            command = shlex.split(value)
        except ValueError as exc:
            checks.append(CheckResult(f"custom-{index}", "warning", output=f"invalid command: {exc}"))
            continue
        if command:
            checks.append(diagnostic_command(f"custom-{index}", command, build, env, args))
    return checks


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


def markdown_code(value: object) -> str:
    text = str(value).replace("`", "\\`").replace("\n", " ")
    return f"`{text}`"


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def render_result_markdown(result: Result) -> str:
    lines = [
        f"# Java migration report: {destination_name(result.source)}",
        "",
        f"- Status: **{result.status}**",
        f"- Source: {markdown_code(result.source)}",
        f"- Output: {markdown_code(result.path)}",
        f"- Duration: {result.duration_seconds:.2f} seconds",
        f"- Changed: {'yes' if result.changed else 'no'}",
    ]
    if result.branch:
        lines.append(f"- Branch: {markdown_code(result.branch)}")
    if result.commit:
        lines.append(f"- Commit: {markdown_code(result.commit)}")
    lines.append(f"- Log: {markdown_code(result.log)}")
    if result.error:
        lines += ["", "## Error", "", result.error]
    if result.diff_stat:
        lines += ["", "## Diff summary", "", "```text", result.diff_stat, "```"]

    if not result.projects:
        lines += ["", "No build projects were processed."]
        return "\n".join(lines) + "\n"

    for index, project in enumerate(result.projects, 1):
        title = Path(project.path).name or project.path
        lines += [
            "", f"## Project {index}: {title}", "",
            f"- Build tool: **{project.build_tool}**",
            f"- Status: **{project.status}**",
            f"- Path: {markdown_code(project.path)}",
        ]
        if project.error:
            lines.append(f"- Error: {project.error}")

        analysis = project.analysis
        lines += ["", "### Analysis", ""]
        lines.append(
            "Detected features: "
            + (", ".join(markdown_code(item) for item in analysis.features) or "None")
        )
        if analysis.findings:
            lines += ["", "Findings:", ""] + [f"- {item}" for item in analysis.findings]
        if analysis.external_configuration:
            lines += ["", "External configuration:", ""] + [
                f"- {markdown_code(item)}" for item in analysis.external_configuration
            ]
        if analysis.excluded_paths:
            lines += ["", "Excluded paths:", ""] + [
                f"- {markdown_code(item)}" for item in analysis.excluded_paths
            ]
        manual_items = [item for item in project.manual_review if item not in analysis.findings]
        if manual_items:
            lines += ["", "Manual review:", ""] + [
                f"- {markdown_code(item)}" for item in manual_items
            ]

        if analysis.dependencies:
            lines += [
                "", "### Direct dependencies", "",
                "| Group | Artifact | Current version | Configuration |",
                "| --- | --- | --- | --- |",
            ]
            lines += [
                f"| {markdown_cell(item.group)} | {markdown_cell(item.artifact)} | "
                f"{markdown_cell(item.version)} | {markdown_cell(item.configuration or '—')} |"
                for item in analysis.dependencies
            ]

        if project.phases:
            lines += [
                "", "### Migration phases", "",
                "| Phase | Status | Changed | Recipes |",
                "| --- | --- | --- | --- |",
            ]
            for phase in project.phases:
                recipes = "<br>".join(
                    markdown_cell(recipe.splitlines()[0].removesuffix(":"))
                    for recipe in phase.recipes
                ) or "—"
                lines.append(
                    f"| {markdown_cell(phase.name)} | {markdown_cell(phase.status)} | "
                    f"{'yes' if phase.changed else 'no'} | {recipes} |"
                )
                if phase.error:
                    lines += ["", f"**{phase.name} error:** {phase.error}"]

        if project.checks:
            lines += [
                "", "### Verification checks", "",
                "| Check | Status | Exit code | Command |",
                "| --- | --- | --- | --- |",
            ]
            for check in project.checks:
                command = shlex.join(check.command) if check.command else "—"
                returncode = "—" if check.returncode is None else str(check.returncode)
                lines.append(
                    f"| {markdown_cell(check.name)} | {markdown_cell(check.status)} | "
                    f"{returncode} | {markdown_cell(command)} |"
                )
    return "\n".join(lines) + "\n"


def render_summary_markdown(summary: dict[str, Any], results: Sequence[Result]) -> str:
    lines = [
        "# Java migration summary", "",
        f"- Generated: {markdown_code(summary['generated_at'])}",
        f"- Target Java: **{summary['target_java']}**",
        f"- Profile: **{summary['profile']}**",
        f"- Recipe repository: {markdown_code(summary['recipe_repository'])}",
        "", "## Results", "",
        "| Status | Count |", "| --- | ---: |",
    ]
    lines += [f"| {status} | {count} |" for status, count in summary["counts"].items()]
    artifacts_used = summary.get("recipe_artifacts", [])
    lines += ["", "## Recipe artifacts", ""]
    lines += ([f"- {markdown_code(item)}" for item in artifacts_used]
              if artifacts_used else ["No recipe artifacts were used."])
    lines += [
        "", "## Repositories", "",
        "| Source | Status | Changed | Duration | Output | Report |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for result in results:
        report_name = f"{slug(result.source)}.md"
        lines.append(
            f"| {markdown_cell(result.source)} | {markdown_cell(result.status)} | "
            f"{'yes' if result.changed else 'no'} | {result.duration_seconds:.2f}s | "
            f"{markdown_cell(result.path)} | [{report_name}]({report_name}) |"
        )
    return "\n".join(lines) + "\n"


def write_result_reports(result: Result, args: argparse.Namespace, name: str) -> list[Path]:
    directory = args.workspace / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    if args.report_format in {"both", "json"}:
        path = directory / f"{name}.json"
        path.write_text(json.dumps(dataclasses.asdict(result), indent=2) + "\n", encoding="utf-8")
        outputs.append(path)
    if args.report_format in {"both", "markdown"}:
        path = directory / f"{name}.md"
        path.write_text(render_result_markdown(result), encoding="utf-8")
        outputs.append(path)
    return outputs


def write_summary_reports(
    summary: dict[str, Any], results: Sequence[Result], args: argparse.Namespace,
) -> list[Path]:
    directory = args.workspace / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    if args.report_format in {"both", "json"}:
        path = directory / "summary.json"
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        outputs.append(path)
    if args.report_format in {"both", "markdown"}:
        path = directory / "summary.md"
        path.write_text(render_summary_markdown(summary, results), encoding="utf-8")
        outputs.append(path)
    return outputs


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
        result.analysis = analyze_project(build, args)
        result.manual_review = list(result.analysis.findings)
        phases = migration_phases(build, result.analysis, args)
        if not phases:
            result.status = "analyzed"
            result.manual_review = sorted(set(result.manual_review + manual_review_files(build.path)))
            return result
        settings = temp / "settings.xml"
        write_maven_settings(settings, args, env)
        for index, phase in enumerate(phases):
            recipe_file = temp / f"rewrite-{index:02d}-{phase.name}.yml"
            init_script = temp / f"init-{index:02d}-{phase.name}.gradle"
            recipe = write_recipe(recipe_file, phase)
            write_gradle_init(init_script, args, recipe, recipe_file)
            command = rewrite_command(build, recipe, recipe_file, settings, init_script, args)
            phase_before = (capture(["git", "status", "--porcelain"], build.path)
                            if git_managed else tree_digest(build.path))
            phase_result = PhaseResult(phase.name, "failed", recipes=list(phase.recipes))
            try:
                if args.dry_run:
                    log.parent.mkdir(parents=True, exist_ok=True)
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write(
                            f"Would run phase {phase.name} in {build.path}: {shlex.join(command)}\n"
                        )
                    phase_result.status = "planned"
                else:
                    run(command, cwd=build.path, env=env, log=log, timeout=args.timeout)
                    phase_after = (capture(["git", "status", "--porcelain"], build.path)
                                   if git_managed else tree_digest(build.path))
                    phase_result.changed = phase_after != phase_before
                    phase_result.status = "changed" if phase_result.changed else "unchanged"
            except Exception as exc:
                phase_result.error = str(exc)
                raise
            finally:
                result.phases.append(phase_result)
        if not args.dry_run:
            verify = verify_command(build, args.verify, settings)
            if verify:
                run(verify, cwd=build.path, env=env, log=log, timeout=args.timeout)
            result.checks = run_post_checks(build, settings, env, args)
            if args.strict_post_checks:
                warnings = [check for check in result.checks if check.status == "warning"]
                if warnings:
                    raise MigrationError("post-check failures: " + ", ".join(check.name for check in warnings))
            after = capture(["git", "status", "--porcelain"], build.path) if git_managed else tree_digest(build.path)
            result.changed = after != before
            result.status = "changed" if result.changed else "unchanged"
        else:
            result.status = "planned"
    except Exception as exc:
        result.error = str(exc)
    result.manual_review = sorted(set(result.manual_review + manual_review_files(build.path)))
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
        with isolate_from_parent_git(repo):
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
        if args.profile == "report-only":
            result.status = "analyzed"
        else:
            result.status = "planned" if args.dry_run else ("changed" if result.changed else "unchanged")
    except SkipMigration as exc:
        result.status = "skipped"
        result.error = str(exc)
    except Exception as exc:
        result.error = str(exc)
    finally:
        result.duration_seconds = round(time.monotonic() - started, 2)
        write_result_reports(result, args, name)
        say(f"[{name}] {result.status} ({result.duration_seconds}s){': ' + result.error if result.error else ''}")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    policy_parser = argparse.ArgumentParser(add_help=False)
    policy_parser.add_argument("--policy", type=Path)
    policy_args, _ = policy_parser.parse_known_args(raw_args)
    policy = load_policy(policy_args.policy)
    packs = policy.get("packs", {})
    dependency_policy = policy.get("dependencies", {})
    verification_policy = policy.get("verification", {})
    reporting_policy = policy.get("reporting", {})
    if not all(isinstance(item, dict) for item in
               (packs, dependency_policy, verification_policy, reporting_policy)):
        raise MigrationError(
            "policy packs, dependencies, verification, and reporting values must be mappings"
        )
    parser = argparse.ArgumentParser(
        description="Clone and modernize Maven/Gradle Java repositories with OpenRewrite.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("sources", nargs="*", help="Git URLs or local directories")
    parser.add_argument("--policy", type=Path, help="versioned YAML or JSON migration policy")
    parser.add_argument(
        "--profile", choices=tuple(PROFILE_DEFAULTS), default=policy.get("profile", "standard"),
        help="migration risk/capability preset",
    )
    parser.add_argument("--repo-path", help="alias for one local directory")
    parser.add_argument("--manifest", type=Path, help="TXT, CSV (url,ref), or JSON repository list")
    parser.add_argument("--output", type=Path, default=Path("artifacts"),
                        help="updated repository/directory copies")
    parser.add_argument("--workspace", type=Path, default=Path(".migration-work"),
                        help="logs, reports, and temporary state")
    parser.add_argument("--report-format", choices=("both", "json", "markdown"),
                        default=reporting_policy.get("format", "both"),
                        help="report file formats to generate")
    parser.add_argument("--target-java", type=int, choices=TARGETS,
                        default=policy.get("targetJava", 21))
    parser.add_argument("--build-tool", choices=("auto", "maven", "gradle"),
                        default=policy.get("buildTool", "auto"))
    parser.add_argument("--max-depth", type=int, default=4, help="maximum build-root discovery depth")
    parser.add_argument("--jobs", type=int, default=1, help="repositories migrated concurrently")
    parser.add_argument("--continue-projects", action="store_true", help="continue other builds after one fails")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per external command")
    parser.add_argument("--verify", choices=("none", "compile", "test"),
                        default=verification_policy.get("build", "test"))
    parser.add_argument(
        "--dependency-strategy", choices=("none", "patch", "latest"),
        default=dependency_policy.get("strategy"),
    )
    parser.add_argument("--dependency-deny", action="append", default=policy_list(policy, "dependencies", "deny"),
                        help="G:A glob excluded from generic upgrades; repeatable")
    parser.add_argument("--dependency-pin", action="append", default=[], metavar="G:A=VERSION",
                        help="pin a dependency glob to a version; repeatable")
    parser.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=policy.get("cleanup"))
    parser.add_argument("--junit5", action=argparse.BooleanOptionalAction, default=None,
                        help="compatibility alias for enabling/disabling JUnit migration")
    parser.add_argument(
        "--testing-modernization", choices=("none", "junit", "standard", "aggressive"),
        default=packs.get("testing"), help="testing migration depth",
    )
    parser.add_argument("--jakarta", choices=("none", "9", "10", "11"),
                        default=str(packs.get("jakarta", "none")),
                        help="explicit Java EE to Jakarta target; never auto-enabled")
    parser.add_argument("--lombok-best-practices", action=argparse.BooleanOptionalAction,
                        default=packs.get("lombokBestPractices"))
    parser.add_argument("--build-best-practices", action=argparse.BooleanOptionalAction,
                        default=policy.get("buildBestPractices"),
                        help="can make major build-tool changes (for example Gradle 9)")
    parser.add_argument("--recipe", action="append", default=policy_list(policy, "recipes"),
                        help="extra recipe; repeatable")
    parser.add_argument("--artifact", action="append", default=policy_list(policy, "artifacts"),
                        help="override G:A:V recipe artifacts")
    parser.add_argument("--exclude", action="append", default=policy_list(policy, "excludePaths"),
                        help="OpenRewrite exclusion glob; repeatable")
    parser.add_argument("--default-exclusions", action=argparse.BooleanOptionalAction, default=True,
                        help="exclude common generated and vendored paths")
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
    parser.add_argument(
        "--post-checks", choices=("none", "jdk", "all"),
        default=verification_policy.get("postChecks"),
        help="run jdeps/jdeprscan and optionally dependency reports after the build",
    )
    parser.add_argument("--verify-command", action="append",
                        default=policy_list(policy, "verification", "commands"),
                        help="additional argv-style verification command; repeatable")
    parser.add_argument("--strict-post-checks", action="store_true",
                        default=bool(verification_policy.get("strict", False)),
                        help="treat diagnostic/custom post-check warnings as failures")
    parser.add_argument(
        "--recipe-repository",
        choices=("maven-central", "maven-local", "codegenome"),
        default="maven-central",
        help="where recipe artifacts are resolved; Code Genome is opt-in",
    )
    parser.add_argument(
        "--artifact-repository",
        help="optional Maven-compatible mirror/remote URL (supplements the selected mode)",
    )
    parser.add_argument("--maven-plugin-version", help="automatic for the selected repository mode")
    parser.add_argument("--gradle-plugin-version", help="automatic for the selected repository mode")
    parser.add_argument("--migrate-java-version", help="automatic for the selected repository mode")
    parser.add_argument("--static-analysis-version", help="automatic for the selected repository mode")
    parser.add_argument("--java-dependencies-version", help="automatic for the selected repository mode")
    parser.add_argument("--testing-frameworks-version", help="automatic for the selected repository mode")
    args = parser.parse_args(raw_args)
    if args.jobs < 1 or args.timeout < 1 or args.max_depth < 0:
        parser.error("--jobs and --timeout must be positive; --max-depth cannot be negative")
    if args.push and (not args.commit or not args.branch):
        parser.error("--push requires --commit and a non-empty --branch")
    if args.recipe_repository == "maven-local" and args.artifact_repository:
        parser.error("--artifact-repository cannot be combined with --recipe-repository maven-local")
    if args.profile not in PROFILE_DEFAULTS:
        parser.error(f"unknown profile in policy: {args.profile}")
    if args.target_java not in TARGETS:
        parser.error(f"unsupported targetJava in policy: {args.target_java}")
    if args.build_tool not in {"auto", "maven", "gradle"}:
        parser.error(f"unsupported buildTool in policy: {args.build_tool}")
    if args.verify not in {"none", "compile", "test"}:
        parser.error(f"unsupported verification.build in policy: {args.verify}")
    if args.report_format not in {"both", "json", "markdown"}:
        parser.error(f"unsupported reporting.format in policy: {args.report_format}")
    profile_defaults = PROFILE_DEFAULTS[args.profile]
    for attribute in ("cleanup", "testing_modernization", "dependency_strategy",
                      "build_best_practices", "post_checks"):
        if getattr(args, attribute) is None:
            setattr(args, attribute, profile_defaults[attribute])
    if args.lombok_best_practices is None:
        args.lombok_best_practices = args.profile == "aggressive"
    if args.testing_modernization not in {"none", "junit", "standard", "aggressive"}:
        parser.error(f"unsupported packs.testing in policy: {args.testing_modernization}")
    if args.dependency_strategy not in {"none", "patch", "latest"}:
        parser.error(f"unsupported dependencies.strategy in policy: {args.dependency_strategy}")
    if args.post_checks not in {"none", "jdk", "all"}:
        parser.error(f"unsupported verification.postChecks in policy: {args.post_checks}")
    if args.jakarta not in {"none", "9", "10", "11"}:
        parser.error(f"unsupported packs.jakarta in policy: {args.jakarta}")
    if args.junit5 is False:
        args.testing_modernization = "none"
    elif args.junit5 is True and args.testing_modernization == "none":
        args.testing_modernization = "junit"
    args.exclusions = list(dict.fromkeys(
        ([] if not args.default_exclusions else list(DEFAULT_EXCLUSIONS)) + args.exclude
    ))
    args.dependency_deny = list(dict.fromkeys(args.dependency_deny))
    cli_dependency_pins = args.dependency_pin
    args.dependency_pin = policy_mapping(policy, "dependencies", "pin")
    for item in cli_dependency_pins:
        pattern, separator, version = item.partition("=")
        if not separator or not pattern or not version:
            parser.error("--dependency-pin must use G:A=VERSION")
        args.dependency_pin[pattern] = version
    args.policy_data = policy
    defaults = CODE_GENOME_VERSIONS if args.recipe_repository == "codegenome" else MAVEN_CENTRAL_VERSIONS
    for name, value in defaults.items():
        attribute = f"{name}_version"
        if getattr(args, attribute) is None:
            setattr(args, attribute, value)
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
            say(
                f"Migrating {len(specs)} repository(s) to Java {args.target_java} with "
                f"{args.jobs} worker(s); profile: {args.profile}; recipes: {args.recipe_repository}"
            )
            if args.jobs == 1:
                results = [migrate_one(spec, args, env, askpass) for spec in specs]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    results = list(pool.map(lambda spec: migrate_one(spec, args, env, askpass), specs))
        summary = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "target_java": args.target_java,
            "recipe_repository": args.recipe_repository,
            "recipe_artifacts": artifacts(args),
            "plugin_versions": {
                "maven": args.maven_plugin_version,
                "gradle": args.gradle_plugin_version,
            },
            "profile": args.profile,
            "report_format": args.report_format,
            "counts": {status: sum(item.status == status for item in results)
                       for status in ("changed", "unchanged", "analyzed", "planned", "skipped", "failed")},
            "results": [dataclasses.asdict(item) for item in results],
        }
        outputs = write_summary_reports(summary, results, args)
        say(f"Summary: {', '.join(str(path) for path in outputs)} "
            f"({summary['counts']['failed']} failed)")
        return 1 if summary["counts"]["failed"] else 0
    except (MigrationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
