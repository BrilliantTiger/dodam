# Dodam benchmark image for C++: Drogon with SQLite3 and OpenSSL, built by CMake and g++, plus the judge.
# Build from bench/images: docker build -f cpp/Dockerfile -t dodam-bench/cpp:dev .
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4 AS toolchain

# g++ 14 and CMake 3.31 with the libraries programs link against, at exact Debian 13 versions:
# SQLite3 and OpenSSL for programs, jsoncpp, uuid and zlib for Drogon, and Python for the judge.
# Once a Debian update replaces one of these versions, this step fails until the pin is raised.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      g++=4:14.2.0-1 g++-14=14.2.0-19 cmake=3.31.6-2 make=4.4.1-2 \
      libsqlite3-dev=3.46.1-7+deb13u2 libssl-dev=3.5.7-1~deb13u3 libjsoncpp-dev=1.9.6-3 \
      uuid-dev=2.41.5-0+deb13u1 zlib1g-dev=1:1.3.dfsg+really1.3.1-1+b1 \
      python3=3.13.5-1 python3-venv=3.13.5-1 \
 && rm -rf /var/lib/apt/lists/*

# Drogon v1.9.14 (commit 3aae769c) with the trantor v1.5.29 it pins as its submodule (commit 78e480f5),
# from source archives checked by hash. Debian 13 ships Drogon 1.9.0, so it is built here: static
# libraries for C++20 with the SQLite3 database client, installed under /usr/local in the final image.
FROM toolchain AS drogon
ADD --checksum=sha256:b0d856d203531de2909ff132c7e3d240575c40a67f34523cc70fc605f0be70a1 \
    https://github.com/drogonframework/drogon/archive/3aae769cc405cf5abb9d477675c6430924f02c7c.tar.gz /src/drogon.tar.gz
ADD --checksum=sha256:60487a296530a330d753fe3c5e492a885d043a5b8d7ffa4d9bdc96b2813a394b \
    https://github.com/an-tao/trantor/archive/78e480f5e138acdc05f76c55a2716718cd846364.tar.gz /src/trantor.tar.gz
RUN mkdir /src/drogon \
 && tar -xzf /src/drogon.tar.gz -C /src/drogon --strip-components=1 \
 && tar -xzf /src/trantor.tar.gz -C /src/drogon/trantor --strip-components=1 \
 && cmake -S /src/drogon -B /src/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=20 \
      -DBUILD_CTL=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_POSTGRESQL=OFF -DBUILD_MYSQL=OFF \
      -DBUILD_REDIS=OFF -DBUILD_SQLITE=ON -DBUILD_BROTLI=OFF -DBUILD_YAML_CONFIG=OFF -DBUILD_C-ARES=OFF \
 && cmake --build /src/build -j 3 \
 && DESTDIR=/out cmake --install /src/build

FROM toolchain
COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

COPY --from=drogon /out/usr/local/ /usr/local/

COPY cpp/app/ /app/
RUN /opt/dodam/setup.sh agent
WORKDIR /app
