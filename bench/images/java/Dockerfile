# Dodam benchmark image for Java: Spring Boot built with Maven and the SQLite JDBC driver, plus the judge.
# Build from bench/images: docker build -f java/Dockerfile -t dodam-bench/java:dev .
FROM maven:3.10.0-eclipse-temurin-25-resolute@sha256:047a82fc82c613266cfb1b66fd920be9c8ba51fd67edc68245e4542768f45e36

# The Maven image's entrypoint only prepares root's ~/.m2, which nothing here uses.
ENTRYPOINT []
CMD ["bash"]

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

# The pinned packages, and the Maven plugins that build with them, live in a repository in /opt/deps
# owned by root (see java/settings.xml). Building a throwaway program with the starting pom.xml fetches
# them: its own dependencies first, so that the repository records them as the program's, then the
# whole build. Maven checks every file it fetches against its SHA-256 in java/checksums.sha256 (to
# refresh that list after a version change, run the same builds with trustedChecksums.record=true and
# copy the file out). The program is then built offline once, as every later build is: a package the
# language does not get fails the build instead of being fetched.
COPY java/settings.xml /usr/share/maven/conf/settings.xml
COPY java/checksums.sha256 /opt/dodam/maven-checksums/checksums.sha256
COPY java/app/pom.xml /tmp/prefetch/pom.xml
COPY java/prefetch/ /tmp/prefetch/src/main/java/prefetch/
RUN cd /tmp/prefetch \
 && checked="-B -ntp -Daether.artifactResolver.postProcessor.trustedChecksums=true \
      -Daether.artifactResolver.postProcessor.trustedChecksums.checksumAlgorithms=SHA-256 \
      -Daether.artifactResolver.postProcessor.trustedChecksums.failIfMissing=true \
      -Daether.trustedChecksumsSource.summaryFile=true \
      -Daether.trustedChecksumsSource.summaryFile.basedir=/opt/dodam/maven-checksums \
      -Daether.trustedChecksumsSource.summaryFile.originAware=false" \
 && mvn $checked compile \
 && mvn $checked -DskipTests clean package \
 && mvn -o -q compile && mvn -o -q -DskipTests clean package \
 && cd / && rm -rf /tmp/prefetch /root/.m2 /opt/deps/maven-repository/.locks /opt/deps/maven-repository/.meta
ENV MAVEN_ARGS=--offline

COPY java/app/ /app/
RUN /opt/dodam/setup.sh agent
WORKDIR /app
