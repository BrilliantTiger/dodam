# Dodam benchmark image for Go: the standard library net/http and modernc.org/sqlite, plus the judge.
# Build from bench/images: docker build -f go/Dockerfile -t dodam-bench/go:dev .
FROM golang:1.27-trixie@sha256:d9fa6e7ba43678ee1e5d1617007db9a134612032a74e2d86b44a9f582f1564cc

# Builds use this toolchain and never fetch another.
ENV GOTOOLCHAIN=local

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

COPY go/app/ /app/
RUN /opt/dodam/setup.sh agent

# The modules go to the module cache, checked against go.sum, and the SQLite driver is compiled once
# into agent's build cache, so a judged build compiles only the program. Without network a module
# that go.mod does not already require cannot be fetched.
USER agent
RUN cd /app && go mod download \
 && printf 'package main\n\nimport _ "modernc.org/sqlite"\n\nfunc main() {}\n' > warm.go \
 && go build -o /tmp/warm . && rm -f warm.go /tmp/warm
USER root
WORKDIR /app
