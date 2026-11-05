# Dodam benchmark image for TypeScript on Node.js: Express and better-sqlite3, plus the judge.
# Build from bench/images: docker build -f typescript/Dockerfile -t dodam-bench/typescript:dev .
FROM node:26-trixie-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

# The pinned packages live in /opt/deps, owned by root; /app/node_modules links to them and the
# verifier relinks it before every build.
# better-sqlite3 compiles its SQLite binding here; the compilers leave with the build.
COPY typescript/app/package.json typescript/app/package-lock.json /opt/deps/
RUN apt-get update \
 && apt-get install -y --no-install-recommends make g++ \
 && cd /opt/deps && npm ci --no-audit --no-fund && npm cache clean --force \
 && apt-get purge -y --auto-remove make g++ && rm -rf /var/lib/apt/lists/* /root/.cache

COPY typescript/app/ /app/
RUN rm -rf /app/node_modules && ln -s /opt/deps/node_modules /app/node_modules \
 && /opt/dodam/setup.sh agent
WORKDIR /app
