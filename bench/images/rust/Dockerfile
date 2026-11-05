# Dodam benchmark image for Rust: axum on tokio and rusqlite with its bundled SQLite, plus the judge.
# Build from bench/images: docker build -f rust/Dockerfile -t dodam-bench/rust:dev .
# --build-arg CARGO_BUILD_JOBS=<n> limits the parallel jobs of the dependency build below.
FROM rust:1.99.1-slim-trixie@sha256:59f72d5265cfcf281fcfa4d990e75f09c53f911def9913c47a4a89dca690a887

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge
RUN /opt/dodam/setup.sh agent && install -d -o agent -g agent /opt/cargo-build

# Cargo runs offline, so a build can use only the crates of /app/Cargo.lock, which the user agent
# fetches here into the toolchain's registry; any other crate fails the build instead of downloading.
# Cargo keeps its intermediate files in a build directory outside /app: the dependencies are
# compiled there once, from a placeholder main that is cleaned away afterwards, so a build of /app
# compiles only the app and writes ./target/release/app. rusqlite compiles its bundled SQLite with
# the C compiler of the base image.
ENV CARGO_NET_OFFLINE=true CARGO_BUILD_BUILD_DIR=/opt/cargo-build
COPY --chown=agent:agent rust/app/ /app/
USER agent
WORKDIR /app
ARG CARGO_BUILD_JOBS
RUN mkdir src && echo 'fn main() {}' > src/main.rs \
 && CARGO_NET_OFFLINE=false cargo fetch --locked --target "$(rustc --print host-tuple)" \
 && cargo build --release --locked \
 && cargo clean --release --package app && rm -rf src target
USER root
