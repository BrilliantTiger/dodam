# Dodam benchmark image for Python: FastAPI served by uvicorn, plus the judge.
# Build from bench/images: docker build -f python/Dockerfile -t dodam-bench/python:dev .
FROM python:3.14-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

# The packages Python programs get, pinned with hashes, in the system Python owned by root.
COPY python/requirements.txt /opt/dodam/app-requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /opt/dodam/app-requirements.txt

RUN /opt/dodam/setup.sh agent
WORKDIR /app
