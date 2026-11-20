#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <set>
#include <string>
#include <system_error>
#include <vector>

namespace fs = std::filesystem;

constexpr size_t MAX_INPUT = 1024 * 1024;

struct Refused {
    int code;
    std::string message;
};

using Record = std::vector<std::string>;

bool inside(const fs::path &folder, const fs::path &place) {
    const std::string f = folder.string(), p = place.string();
    return p == f || p.compare(0, f.size() + 1, f + "/") == 0;
}

fs::path real_place(const fs::path &name) {
    std::error_code error;
    fs::path place = fs::canonical(name, error);
    if (error) throw Refused{2, name.string() + " does not exist"};
    return place;
}

fs::path checked(const fs::path &folder, const fs::path &name) {
    fs::path place = real_place(name);
    if (!inside(folder, place)) {
        throw Refused{3, name.string() + " is outside the working folder"};
    }
    return place;
}

std::string read_limited(int fd) {
    std::string data(MAX_INPUT + 1, '\0');
    size_t size = 0;
    while (size < data.size()) {
        ssize_t got = ::read(fd, data.data() + size, data.size() - size);
        if (got < 0) throw Refused{2, "the input cannot be read"};
        if (got == 0) break;
        size += static_cast<size_t>(got);
    }
    data.resize(size);
    return data;
}

std::string read_input(const fs::path &folder, const std::string &name) {
    std::string data;
    if (name == "-") {
        data = read_limited(0);
    } else {
        fs::path place = checked(folder, name);
        std::error_code error;
        if (!fs::is_regular_file(place, error)) throw Refused{2, name + " is not a file"};
        int fd = ::open(place.c_str(), O_RDONLY);
        if (fd < 0) throw Refused{2, name + " cannot be read"};
        try {
            data = read_limited(fd);
        } catch (...) {
            ::close(fd);
            throw;
        }
        ::close(fd);
    }
    if (data.size() > MAX_INPUT) {
        throw Refused{2, "the input is larger than 1048576 bytes"};
    }
    return data;
}

std::vector<Record> parse(const std::string &text) {
    std::vector<Record> records;
    Record record;
    std::string field;
    bool quoted = false, after_quote = false, at_start = true;
    const size_t n = text.size();
    size_t i = 0;
    while (i < n) {
        const char c = text[i];
        if (quoted) {
            if (c == '"') {
                if (i + 1 < n && text[i + 1] == '"') {
                    field.push_back('"');
                    i += 2;
                    continue;
                }
                quoted = false;
                after_quote = true;
            } else {
                field.push_back(c);
            }
            i += 1;
            continue;
        }
        const size_t line_end = c == '\n' ? 1 : (c == '\r' && i + 1 < n && text[i + 1] == '\n') ? 2 : 0;
        if (c == ',' || line_end > 0) {
            record.push_back(field);
            field.clear();
            at_start = true;
            after_quote = false;
            if (line_end > 0) {
                records.push_back(record);
                record.clear();
            }
            i += line_end > 0 ? line_end : 1;
            continue;
        }
        if (c == '"') {
            if (!at_start) throw Refused{2, "a double quote where none is allowed"};
            quoted = true;
            at_start = false;
        } else if (after_quote) {
            throw Refused{2, "text after a closing quote"};
        } else {
            field.push_back(c);
            at_start = false;
        }
        i += 1;
    }
    if (quoted) throw Refused{2, "a quoted field is not closed"};
    if (!record.empty() || !at_start) {
        record.push_back(field);
        records.push_back(record);
    }
    return records;
}

std::vector<Record> table(const std::string &data) {
    std::vector<Record> records = parse(data);
    if (records.empty()) throw Refused{2, "the input has no header"};
    const Record &header = records[0];
    std::set<std::string> names(header.begin(), header.end());
    if (names.count("") > 0 || names.size() != header.size()) {
        throw Refused{2, "header names must be non-empty and differ"};
    }
    for (const Record &row : records) {
        if (row.size() != header.size()) {
            throw Refused{2, "a record has a different number of fields than the header"};
        }
    }
    return records;
}

void quote(std::string &out, const std::string &text) {
    out.push_back('"');
    for (const char c : text) {
        const unsigned char u = static_cast<unsigned char>(c);
        if (c == '"' || c == '\\') {
            out.push_back('\\');
            out.push_back(c);
        } else if (u < 0x20) {
            char escaped[8];
            std::snprintf(escaped, sizeof escaped, "\\u%04x", u);
            out += escaped;
        } else {
            out.push_back(c);
        }
    }
    out.push_back('"');
}

std::string to_json(const Record &header, const std::vector<const Record *> &rows) {
    std::string out = "[";
    for (size_t r = 0; r < rows.size(); r++) {
        out += r == 0 ? "{" : ", {";
        for (size_t i = 0; i < header.size(); i++) {
            if (i > 0) out += ", ";
            quote(out, header[i]);
            out += ": ";
            quote(out, (*rows[r])[i]);
        }
        out += "}";
    }
    return out + "]\n";
}

fs::path output_place(const fs::path &folder, const std::string &name) {
    const fs::path given(name);
    const fs::path parent = checked(folder, given.parent_path().empty() ? fs::path(".") : given.parent_path());
    std::error_code error;
    if (!fs::is_directory(parent, error) || given.filename().empty()) {
        throw Refused{2, "the folder of " + name + " does not exist"};
    }
    return parent / given.filename();
}

void write_all(int fd, const std::string &data) {
    size_t sent = 0;
    while (sent < data.size()) {
        ssize_t put = ::write(fd, data.data() + sent, data.size() - sent);
        if (put < 0) throw Refused{2, "cannot write the output"};
        sent += static_cast<size_t>(put);
    }
}

void write_new(const fs::path &place, const std::string &data) {
    int fd = ::open(place.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0) {
        if (errno == EEXIST) throw Refused{3, place.string() + " already exists"};
        throw Refused{2, "cannot write " + place.string()};
    }
    try {
        write_all(fd, data);
    } catch (...) {
        ::close(fd);
        throw;
    }
    ::close(fd);
}

bool file_name(const std::string &value) {
    if (value.empty() || value.size() > 64) return false;
    for (const char c : value) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
        if (!ok) return false;
    }
    return true;
}

void split(const fs::path &folder, const std::vector<std::string> &args) {
    if (args[1] == "-") throw Refused{2, "--split needs a folder to write to"};
    const fs::path place = output_place(folder, args[1]);
    const std::vector<Record> records = table(read_input(folder, args[0]));
    const Record &header = records[0];
    size_t column = header.size();
    for (size_t i = 0; i < header.size(); i++) {
        if (header[i] == args[3]) column = i;
    }
    if (column == header.size()) throw Refused{2, "no column named " + args[3]};
    std::vector<std::string> order;
    std::vector<std::vector<const Record *>> groups;
    for (size_t r = 1; r < records.size(); r++) {
        const std::string &value = records[r][column];
        if (!file_name(value)) throw Refused{2, "a value of the column cannot be a file name"};
        size_t g = 0;
        while (g < order.size() && order[g] != value) g++;
        if (g == order.size()) {
            order.push_back(value);
            groups.emplace_back();
        }
        groups[g].push_back(&records[r]);
    }
    if (::mkdir(place.c_str(), 0755) != 0) {
        if (errno == EEXIST) throw Refused{3, place.string() + " already exists"};
        throw Refused{2, "cannot create " + place.string()};
    }
    for (size_t g = 0; g < order.size(); g++) {
        write_new(place / (order[g] + ".json"), to_json(header, groups[g]));
    }
}

void run(const std::vector<std::string> &args) {
    const fs::path folder = real_place(".");
    if (args.size() == 4 && args[2] == "--split") {
        split(folder, args);
        return;
    }
    if (args.size() != 2) throw Refused{2, "usage: <input> <output> [--split <column>]"};
    const bool to_stdout = args[1] == "-";
    const fs::path place = to_stdout ? fs::path() : output_place(folder, args[1]);
    const std::vector<Record> records = table(read_input(folder, args[0]));
    std::vector<const Record *> rows;
    for (size_t i = 1; i < records.size(); i++) rows.push_back(&records[i]);
    const std::string data = to_json(records[0], rows);
    if (to_stdout) {
        write_all(1, data);
    } else {
        write_new(place, data);
    }
}

int main(int argc, char **argv) {
    try {
        run(std::vector<std::string>(argv + 1, argv + argc));
    } catch (const Refused &refused) {
        std::cerr << refused.message << "\n";
        return refused.code;
    }
    return 0;
}
