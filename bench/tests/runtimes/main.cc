// Copy a file and serve it over HTTP, each through the library call a C++ program would use. The
// standard library has no call that sends a file over a socket or copies one connection into another.
// The framework sends a file larger than about 200 KB with the kernel's file-sending call unless that
// is switched off, by a call or in its configuration file; the modes here try each.
#include <drogon/drogon.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

#include <unistd.h>

int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "temp") {
        // a temporary file, made where the runtime makes one
        const auto folder = std::filesystem::temp_directory_path();
        const auto made = folder / ("temp-" + std::to_string(::getpid()));
        {
            std::ofstream out(made);
            out << "temp";
        }
        std::ifstream in(made);
        std::string back;
        in >> back;
        if (back != "temp") {
            return 1;
        }
        std::cout << std::filesystem::canonical(folder).string() << std::endl;
        return 0;
    }
    if (mode == "copy") {
        std::filesystem::copy_file("in.bin", "out.bin", std::filesystem::copy_options::overwrite_existing);
        std::cout << "ok" << std::endl;
        return 0;
    }
    const std::string folder = std::filesystem::current_path().string();
    for (const std::string name : {"in.bin", "small.bin"}) {
        drogon::app().registerHandler(
            "/response/" + name,
            [folder, name](const drogon::HttpRequestPtr&, std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
                callback(drogon::HttpResponse::newFileResponse(folder + "/" + name, "", drogon::CT_APPLICATION_OCTET_STREAM));
            },
            {drogon::Get});
    }
    drogon::app().registerHandler(
        "/quit",
        [](const drogon::HttpRequestPtr&, std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            auto response = drogon::HttpResponse::newHttpResponse();
            response->setBody("bye");
            callback(response);
            std::thread([] {
                std::this_thread::sleep_for(std::chrono::milliseconds(300));
                std::_Exit(0);
            }).detach();
        },
        {drogon::Get});
    if (mode == "serve-configured") {
        drogon::app().loadConfigFile("config.json");          // "use_sendfile": false
    }
    if (mode == "serve-switched-off") {
        drogon::app().enableSendfile(false);
    }
    // files of the folder itself, served by the framework without a handler
    drogon::app().setDocumentRoot(folder).setFileTypes({"bin"});
    drogon::app().addListener("127.0.0.1", 18080).setThreadNum(1).run();
    return 0;
}
