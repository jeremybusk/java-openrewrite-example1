```
# Build the Java 21 execution image
docker build \
  --build-arg JAVA_VARIANT=21-trixie \
  --build-arg GRADLE_VERSION=8.14.3 \
  -t java-migrator:jdk21 \
  -f .devcontainer/Dockerfile .
```

Then migrate and test for Java 21:

```
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

Using `jdk25` with `--target-java 21` can still rewrite and usually compile Java 21-targeted code, but its tests execute on Java 25. That may miss runtime behavior specific to Java 21. So:
