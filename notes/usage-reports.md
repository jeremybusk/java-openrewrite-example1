docker build \
  --build-arg JAVA_VARIANT=25-trixie \
  --build-arg GRADLE_VERSION=9.1.0 \
  -t java-migrator:jdk25 \
  -f .devcontainer/Dockerfile \
  .

docker run --rm \
  -v "$PWD:/workspace" \
  -w /workspace \
  java-migrator:jdk25 \
  python3 migrate.py \
    https://github.com/jeremybusk/java-openrewrite-example1.git \
    --target-java 25 \
    --profile aggressive \
    --dry-run \
    --verify none \
    --output /workspace/artifacts/preplan \
    --workspace /workspace/.migration-work/preplan \
    --force
