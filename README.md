# Java OpenRewrite migration engine

A batch migration CLI for cloning public or private Git repositories and moving
Maven and Gradle applications to Java 11, 17, 21, or 25. Java 21 is the default.
It runs in a Microsoft dev container, uses a repository's build wrapper when
available, isolates failures, verifies migrated builds, and emits JSON and
Markdown reports.

The tool never modifies a local input directory. It copies local inputs or clones
remote inputs to `artifacts/<name>` and migrates that copy. If the destination
already exists it is reported as `skipped`; pass `--force` to replace that one
destination. `artifacts/` and `.migration-work/` are gitignored.

## Default migration policy

Recipe artifacts resolve from Maven Central by default, with no Code Genome
account or credentials required. The default versions are pinned to the newest
releases verified in Maven Central so a later Code Genome-only release cannot
silently break a run.

- Analyze each build before rewriting. The report records direct dependencies,
  detected frameworks/languages, removed JDK APIs, internal JDK usage, generated
  paths, and external CI/toolchain files.
- Run separate Java, compatibility, test migration, test cleanup, dependency,
  source cleanup, and custom-recipe phases. Every phase and its recipes are
  recorded independently, so a failure has a useful boundary.
- Run `UpgradeToJava<target>` to update sources, build settings, plugins, CI, and
  known incompatible APIs. Java EE/Jakarta changes remain explicit because a
  namespace change often crosses application-server and contract boundaries.
- Migrate JUnit 4 to JUnit 5 when JUnit is detected. The standard pack also
  migrates Mockito 4 to 5 and applies JUnit 5 best practices; Gradle projects get
  an explicit JUnit Platform launcher to keep engine and launcher versions
  aligned.
- Upgrade only direct, literal-version dependencies. Coordinated ecosystems such
  as Spring, Hibernate, Jackson, JUnit, and Mockito are skipped unless pinned;
  deny rules and organization-approved pins can further constrain updates.
- Apply common static-analysis cleanup after compatibility work.
- Discover independent Maven and Gradle build roots in monorepos without running
  ordinary nested modules twice.
- Clone submodules by default, support Git LFS, and use the container Gradle when
  an old wrapper cannot start on the modern migration JVM.
- Run tests after rewriting, then run `jdeps --jdk-internals` and
  `jdeprscan --for-removal`; retain detailed diagnostics in JSON and summarize
  them in Markdown.
- List likely stale Java 8 runbooks/configuration in each project's
  `manual_review` report field instead of deleting organization-specific files.

`--build-best-practices` is opt-in because the current Gradle composite can make
a Gradle major-version upgrade. Framework migrations such as Spring Boot and
Quarkus should be added deliberately with `--recipe` and a matching `--artifact`;
there is no universally safe framework target.

## Profiles and policy as code

Profiles set risk-appropriate defaults; any individual option can override them.

| Profile | Cleanup | Tests | Dependencies | Post-checks |
| --- | --- | --- | --- | --- |
| `conservative` | off | JUnit migration | none | JDK diagnostics |
| `standard` (default) | common cleanup | JUnit + Mockito | patch | JDK diagnostics |
| `aggressive` | cleanup + build best practices | deeper Mockito cleanup | latest | JDK + dependency report |
| `report-only` | no rewrite | no rewrite | no rewrite | analysis only |

Start by inventorying a portfolio without downloading recipe artifacts:

```bash
python3 migrate.py --manifest repositories.txt --profile report-only --jobs 4
```

For repeatable fleet migrations, copy
[`migration-policy.example.yml`](migration-policy.example.yml), review its deny
rules and pins, then run:

```bash
python3 migrate.py --manifest repositories.txt \
  --policy migration-policy.yml --jobs 4
```

The policy supports `targetJava`, `buildTool`, profiles, test/Jakarta/Lombok
packs, dependency strategy/deny/pin rules, exclusion globs, extra recipes and
artifacts, build verification, JDK/dependency diagnostics, strict diagnostics,
custom verification commands, and report format. CLI options take precedence.
YAML support is included in the devcontainer; a JSON policy works with a stock
Python install.

Useful targeted packs and safety controls:

```bash
# Explicit Java EE namespace/application-server target.
python3 migrate.py ./legacy-ee --jakarta 10

# Lombok cleanup is opt-in; Lombok + MapStruct binding is detected automatically.
python3 migrate.py ./service --lombok-best-practices

# Protect or approve individual dependency families.
python3 migrate.py ./service \
  --dependency-deny 'com.mycompany:*' \
  --dependency-pin 'org.apache.commons:commons-lang3=3.17.0'

# Treat jdeps/jdeprscan/custom check failures as migration failures.
python3 migrate.py ./service --post-checks all --strict-post-checks \
  --verify-command './scripts/integration-test.sh'
```

Generated and vendored trees are excluded by default. Repeat `--exclude` for
project-specific globs or use `--no-default-exclusions` when generated sources
are intentionally migration input.

## Credentials

Git credentials and recipe-repository credentials are independent:

1. `GIT_TOKEN` is a GitHub/GitLab/Bitbucket PAT used only by a temporary
   `GIT_ASKPASS` helper. It is never put in clone URLs or command logs. Public
   HTTPS repositories need no PAT. Override the username with `--git-username`
   (GitHub defaults to `x-access-token`; GitLab commonly uses `oauth2`). SSH URLs
   use your normal SSH configuration instead.
2. No recipe-repository credentials are needed for the default Maven Central
   mode. `CODE_GENOME_USERNAME` and `CODE_GENOME_TOKEN` are read only when
   `--recipe-repository codegenome` is selected. They are artifact credentials,
   not Git credentials.

```bash
export GIT_TOKEN='your-source-control-pat'
# Only for --recipe-repository codegenome:
export CODE_GENOME_USERNAME='you@example.com'
export CODE_GENOME_TOKEN='your-code-genome-download-token'
```

Public source does not necessarily mean Apache-licensed open source. OpenRewrite
core and many building-block recipes are Apache 2.0, while the comprehensive
Java migration, static-analysis, and testing recipe modules used by the default
policy are Moderne Source Available License software. Maven Central mode is
account-free, but it does not change those artifact licenses. Review the license
before offering migrations as a product or service.

## Recipe repositories

Three explicit modes are supported:

```bash
# Default: account-free releases pinned from Maven Central.
python3 migrate.py ./my-app --recipe-repository maven-central

# Prefer recipes built and installed in ~/.m2/repository, then use Central for
# their transitive dependencies and the OpenRewrite build plugin.
python3 migrate.py ./my-app --recipe-repository maven-local \
  --migrate-java-version YOUR_LOCAL_VERSION

# Opt in to current Code Genome releases.
python3 migrate.py ./my-app --recipe-repository codegenome
```

`maven-local` adds `mavenLocal()` for Gradle; Maven already checks its local
repository first. Use the version flags or repeat `--artifact GROUP:NAME:VERSION`
when locally built coordinates differ from the pinned defaults. To use an
organization repository proxy in Central or Code Genome mode, pass
`--artifact-repository https://repository.example/repository/maven-public`.

## Dev container and Docker

In VS Code, choose **Dev Containers: Reopen in Container**. The image is based on
Microsoft's Java 21 Debian Trixie devcontainer and includes Python, Maven, and
Gradle. Trixie is used instead of Ubuntu to stay on Microsoft's current default
Java devcontainer line with fewer distribution-specific variables.

The same image works directly with Docker:

```bash
docker build -t java-migrator -f .devcontainer/Dockerfile .

docker run --rm \
  -e GIT_TOKEN \
  -v "$PWD:/workspace" -w /workspace \
  java-migrator \
  python3 migrate.py examples --target-java 21 --force
```

Add `-e CODE_GENOME_USERNAME -e CODE_GENOME_TOKEN` and
`--recipe-repository codegenome` only for a Code Genome run.

That migrates both included Java 8 fixtures and writes updated copies beneath
`artifacts/examples`. Add `--dry-run --verify none` to test copying, discovery,
recipe generation, and reporting without downloading artifacts.

For Java 25 build verification, use a Java 25 image:

```bash
docker build --build-arg JAVA_VARIANT=25-trixie --build-arg GRADLE_VERSION=9.1.0 \
  -t java-migrator:jdk25 -f .devcontainer/Dockerfile .
```

Gradle 9.1 is the minimum release that can itself run on Java 25; the default
Gradle 8.14 line is retained for better compatibility while bootstrapping older
projects on the default Java 21 image. For especially old Gradle/Android/Kotlin
builds, migrate and validate Java 21 first, then use that output as the input to
a separate Java 25 run.

## Local and remote repositories

```bash
# The source is untouched; the result is artifacts/java8-maven.
python3 migrate.py ./examples/java8-maven --target-java 21 --force

# Clone and migrate a public or private repository.
python3 migrate.py https://github.com/acme/service.git --target-java 21

# Commit and push the result on automation/java-21.
python3 migrate.py https://github.com/acme/service.git \
  --target-java 21 --commit --push --force
```

Without `--commit`, changes remain uncommitted for review. `--push` requires a
commit and a branch. Logs and reports live in `.migration-work/`. JSON retains
complete machine-readable diagnostics while Markdown provides concise review
pages. Both are generated by default; choose only one with
`--report-format json` or `--report-format markdown`.

## Batch migration

TXT manifests accept `URL` or `URL REF`; CSV accepts `url,ref`; JSON accepts URL
strings or objects like `{"url": "...", "ref": "main"}`.

```bash
python3 migrate.py --manifest repositories.txt \
  --target-java 21 --jobs 4 --continue-projects
```

Keep concurrency conservative: each OpenRewrite JVM can consume substantial CPU
and memory. The command returns nonzero if any repository fails. Re-running skips
destinations already present; `--force` discards and recreates only the matching
output destination.

Git submodules are cloned by default; opt out with `--no-submodules`. Private
dependency repositories continue to use your Maven settings and environment, so
mount `~/.m2`/`~/.gradle` when a Docker run needs organization-specific config.

Useful controls:

```bash
python3 migrate.py ./my-app --verify compile --dependency-strategy none
python3 migrate.py ./my-app --no-cleanup --no-junit5
python3 migrate.py ./mixed-repo --build-tool maven --max-depth 6
```

## Java 8 fixtures

[`examples/`](examples/) contains independent Maven and Gradle apps with Java 8
compiler settings, JUnit 4, stale dependencies/plugins, deprecated APIs,
redundant source patterns, an old Java container, and obsolete runbooks. Prose
and intentionally dead files demonstrate a boundary: a safe general-purpose
tool should report them for human review rather than guess that they can be
deleted.

Run the unit tests with:

```bash
python3 -m unittest discover -s tests -v
```
