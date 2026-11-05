cmake_minimum_required(VERSION 3.28)
project(app LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# The packages a program may use, all installed in the image; the build fetches nothing.
# SQLite3 and OpenSSL come before Drogon, whose package adds find modules of its own.
find_package(SQLite3 REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Drogon 1.9.14 EXACT CONFIG REQUIRED)

file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS src/*.cc src/*.cpp src/*.cxx)
add_executable(app ${APP_SOURCES})
target_link_libraries(app PRIVATE Drogon::Drogon SQLite::SQLite3 OpenSSL::SSL OpenSSL::Crypto)
