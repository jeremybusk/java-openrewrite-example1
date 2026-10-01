ARG JAVA_VARIANT=21-trixie
ARG JAVA_IMAGE_VERSION=3
FROM mcr.microsoft.com/devcontainers/java:${JAVA_IMAGE_VERSION}-${JAVA_VARIANT}

ARG MAVEN_VERSION=3.9.11
ARG GRADLE_VERSION=8.14.3

USER root
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends python3 python3-pip git git-lfs curl unzip ca-certificates \
    && curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" -o /tmp/maven.tar.gz \
    && tar -xzf /tmp/maven.tar.gz -C /opt \
    && ln -s "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn \
    && curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && ln -s "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle \
    && rm -f /tmp/maven.tar.gz /tmp/gradle.zip \
    && rm -rf /var/lib/apt/lists/*

USER vscode
WORKDIR /workspace
