Yes. For this repository, I’d use Java 25 with the aggressive profile. Java 25 is the latest LTS; Java 27 is newer but non-LTS and the tool currently supports up to Java 25. [Oracle Java releases](https://www.oracle.com/java/technologies/downloads/)

From the migration-tool repository:

```bash
docker build \
  --build-arg JAVA_VARIANT=25-trixie \
  --build-arg GRADLE_VERSION=9.1.0 \
  -t java-migrator:jdk25 \
  -f .devcontainer/Dockerfile .
```

Then migrate the public GitHub repository:

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  -w /workspace \
  java-migrator:jdk25 \
  python3 migrate.py \
    https://github.com/jeremybusk/java-openrewrite-example1.git \
    --target-java 25 \
    --profile aggressive \
    --output /workspace/artifacts \
    --workspace /workspace/.migration-work \
    --force
```

Because the repository is public, no PAT is required.

Results will be under:

```text
artifacts/java-openrewrite-example1/
```

The summary will be:

```text
.migration-work/reports/summary.json
```

`--force` removes and recreates only that output copy. It does not modify your working repository.

For the more conservative Java 21 target:

```bash
docker build -t java-migrator:jdk21 -f .devcontainer/Dockerfile .

docker run --rm \
  -v "$PWD:/workspace" \
  -w /workspace \
  java-migrator:jdk21 \
  python3 migrate.py \
    https://github.com/jeremybusk/java-openrewrite-example1.git \
    --target-java 21 \
    --profile aggressive \
    --force
```

The aggressive profile already implies:

- Latest dependency releases where generic upgrades are considered safe.
- Aggressive test modernization.
- Source cleanup.
- Maven/Gradle best-practice recipes.
- JDK and dependency diagnostics.

## Selecting Spring Boot

Java is directly selectable:

```text
--target-java 11
--target-java 17
--target-java 21
--target-java 25
```

Spring Boot is not yet a first-class flag. The current low-level mechanism is `--recipe` plus `--artifact`, but supplying `--artifact` replaces the complete default artifact list.

For example, this policy selects the Spring Boot 3.5 migration family:

```yaml
version: 1
profile: aggressive
targetJava: 25

recipes:
  - org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5

artifacts:
  - org.openrewrite.recipe:rewrite-migrate-java:3.42.1
  - org.openrewrite.recipe:rewrite-static-analysis:2.41.1
  - org.openrewrite.recipe:rewrite-java-dependencies:1.60.2
  - org.openrewrite.recipe:rewrite-testing-frameworks:3.44.0
  - org.openrewrite.recipe:rewrite-spring:6.37.1

verification:
  build: test
  postChecks: all
  strict: false
  commands: []
```

Run it with:

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  -w /workspace \
  java-migrator:jdk25 \
  python3 migrate.py \
    https://github.com/example/spring-application.git \
    --policy /workspace/spring-boot-35.yml \
    --force
```

That OpenRewrite recipe targets the latest `3.5.x`, not one exact patch. The official recipe upgrades the Spring Boot parent, dependencies, Maven/Gradle plugins, Spring Security and Spring Cloud together. [OpenRewrite Spring Boot 3.5 recipe](https://docs.openrewrite.org/recipes/java/spring/boot3/upgradespringboot_3_5-community-edition)

As of today, Spring lists these stable lines:

- Spring Boot 4.1.1: latest overall stable
- Spring Boot 4.0.8
- Spring Boot 3.5.16
- Spring Boot 3.4.13

[Official Spring Boot versions](https://docs.spring.io/spring-boot/reference/index.html)

I would add first-class options next:

```text
--spring-boot none|3.5|4.0
--spring-boot-version 3.5.16
```

That would automatically select compatible recipe artifacts and update Spring’s parent/BOM/plugins coherently. Using `--dependency-pin` alone is insufficient for Spring Boot because it does not update the parent POM, BOM, build plugin, configuration properties, or related framework migrations.
