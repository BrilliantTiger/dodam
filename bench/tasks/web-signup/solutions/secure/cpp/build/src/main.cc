// Sign-up and login API: Drogon, SQLite, PBKDF2 password hashes and HMAC-signed tokens.
#include <drogon/drogon.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <sqlite3.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <format>
#include <functional>
#include <iostream>
#include <locale>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

using namespace drogon;

namespace {

using Callback = std::function<void(const HttpResponsePtr &)>;

constexpr std::size_t MAX_BODY = 64 * 1024;
constexpr std::int64_t TOKEN_SECONDS = 3600;
constexpr int PBKDF2_ITERATIONS = 600000;
constexpr int SALT_BYTES = 16;
constexpr int KEY_BYTES = 32;

struct User {
  std::int64_t id;
  std::string email;
  std::string name;
};

struct Claims {
  std::int64_t uid;
  std::int64_t exp;
};

// A refusal, answered with its status and {"error": message}.
struct HttpError : std::runtime_error {
  HttpError(HttpStatusCode status, const std::string &message) : std::runtime_error(message), status(status) {}
  HttpStatusCode status;
};

// A failed SQLite call, with its extended result code.
struct DatabaseError : std::runtime_error {
  DatabaseError(int code, const std::string &message) : std::runtime_error(message), code(code) {}
  int code;
};

// Text arrives as UTF-8; lengths and rules count Unicode code points.
constexpr char32_t INVALID = 0x110000;

// Reads the code point at text[i] and moves i past it; malformed UTF-8 gives INVALID and moves one byte.
char32_t decode(std::string_view text, std::size_t &i) {
  const auto byte = [&](std::size_t at) { return at < text.size() ? static_cast<unsigned char>(text[at]) : 0U; };
  const unsigned first = byte(i);
  int extra = first < 0x80 ? 0 : first < 0xC2 ? -1 : first < 0xE0 ? 1 : first < 0xF0 ? 2 : first < 0xF5 ? 3 : -1;
  char32_t code = extra <= 0 ? first : first & (0x3F >> extra);
  for (int k = 1; k <= extra; ++k) {
    const unsigned next = byte(i + k);
    if ((next & 0xC0) != 0x80) {
      extra = -1;
      break;
    }
    code = code << 6 | (next & 0x3F);
  }
  constexpr char32_t smallest[] = {0, 0x80, 0x800, 0x10000};
  if (extra < 0 || code < smallest[extra] || code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) {
    ++i;
    return INVALID;
  }
  i += extra + 1;
  return code;
}

// Appends the code point to out as UTF-8.
void append(std::string &out, char32_t code) {
  if (code < 0x80) {
    out += static_cast<char>(code);
  } else if (code < 0x800) {
    out += static_cast<char>(0xC0 | code >> 6);
    out += static_cast<char>(0x80 | (code & 0x3F));
  } else if (code < 0x10000) {
    out += static_cast<char>(0xE0 | code >> 12);
    out += static_cast<char>(0x80 | (code >> 6 & 0x3F));
    out += static_cast<char>(0x80 | (code & 0x3F));
  } else {
    out += static_cast<char>(0xF0 | code >> 18);
    out += static_cast<char>(0x80 | (code >> 12 & 0x3F));
    out += static_cast<char>(0x80 | (code >> 6 & 0x3F));
    out += static_cast<char>(0x80 | (code & 0x3F));
  }
}

bool validUtf8(std::string_view text) {
  for (std::size_t i = 0; i < text.size();) {
    if (decode(text, i) == INVALID) {
      return false;
    }
  }
  return true;
}

// The length in code points of valid UTF-8: every byte that does not continue a sequence starts one.
std::size_t length(std::string_view text) {
  return std::ranges::count_if(text, [](char c) { return (static_cast<unsigned char>(c) & 0xC0) != 0x80; });
}

// What JavaScript's trim() removes and its \s matches: ASCII and Unicode spaces and line breaks.
bool isSpace(char32_t c) {
  return (c >= 0x09 && c <= 0x0D) || c == 0x20 || c == 0xA0 || c == 0x1680 || (c >= 0x2000 && c <= 0x200A) ||
         c == 0x2028 || c == 0x2029 || c == 0x202F || c == 0x205F || c == 0x3000 || c == 0xFEFF;
}

std::string trim(std::string_view text) {
  std::size_t begin = text.size(), end = 0;
  for (std::size_t i = 0; i < text.size();) {
    const std::size_t at = i;
    if (!isSpace(decode(text, i))) {
      begin = std::min(begin, at);
      end = i;
    }
  }
  return begin < end ? std::string(text.substr(begin, end - begin)) : std::string();
}

// Lower-cases every letter with the Unicode case mapping of the C.UTF-8 locale.
std::string lower(std::string_view text) {
  static const std::locale utf8("C.UTF-8");
  const auto &ctype = std::use_facet<std::ctype<wchar_t>>(utf8);
  std::string out;
  for (std::size_t i = 0; i < text.size();) {
    append(out, static_cast<char32_t>(ctype.tolower(static_cast<wchar_t>(decode(text, i)))));
  }
  return out;
}

// The TypeScript rule /^[^@\s]+@[^@\s]*\.[^@\s]*$/: one @ after some text, a dot after the @, no spaces.
bool validEmail(std::string_view email) {
  const std::size_t at = email.find('@');
  if (at == 0 || at == std::string_view::npos || email.find('@', at + 1) != std::string_view::npos ||
      email.find('.', at + 1) == std::string_view::npos) {
    return false;
  }
  for (std::size_t i = 0; i < email.size();) {
    if (isSpace(decode(email, i))) {
      return false;
    }
  }
  return true;
}

// The request body as a JSON object, or a 400 error.
const Json::Value &jsonObject(const HttpRequestPtr &req) {
  const auto &body = req->getJsonObject();
  if (!body || !body->isObject()) {
    throw HttpError(k400BadRequest, "invalid request body");
  }
  return *body;
}

// A field of the body that must be a string of valid UTF-8, or a 400 error.
std::string field(const Json::Value &body, const char *name) {
  const Json::Value &value = body[name];
  if (!value.isString() || !validUtf8(value.asString())) {
    throw HttpError(k400BadRequest, "invalid request body");
  }
  return value.asString();
}

std::int64_t now() {
  return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string randomBytes(int count) {
  std::string bytes(count, '\0');
  if (RAND_bytes(reinterpret_cast<unsigned char *>(bytes.data()), count) != 1) {
    throw std::runtime_error("RAND_bytes failed");
  }
  return bytes;
}

std::string base64url(std::string_view data) { return utils::base64EncodeUnpadded(data, true); }

std::string hex(std::string_view bytes) {
  return utils::binaryStringToHex(reinterpret_cast<const unsigned char *>(bytes.data()), bytes.size(), true);
}

std::string pbkdf2(std::string_view password, std::string_view salt, int iterations) {
  std::string key(KEY_BYTES, '\0');
  if (PKCS5_PBKDF2_HMAC(password.data(), static_cast<int>(password.size()),
                        reinterpret_cast<const unsigned char *>(salt.data()), static_cast<int>(salt.size()), iterations,
                        EVP_sha256(), KEY_BYTES, reinterpret_cast<unsigned char *>(key.data())) != 1) {
    throw std::runtime_error("PBKDF2 failed");
  }
  return key;
}

// Stored as pbkdf2-sha256$<iterations>$<salt>$<key>, with salt and key in hex.
std::string hashPassword(std::string_view password) {
  const std::string salt = randomBytes(SALT_BYTES);
  return std::format("pbkdf2-sha256${}${}${}", PBKDF2_ITERATIONS, hex(salt),
                     hex(pbkdf2(password, salt, PBKDF2_ITERATIONS)));
}

bool passwordMatches(std::string_view password, const std::string &stored) {
  const std::vector<std::string> parts = utils::splitString(stored, "$");
  if (parts.size() != 4 || parts[0] != "pbkdf2-sha256") {
    throw std::runtime_error("unknown password hash format");
  }
  const std::string salt = utils::hexToBinaryString(parts[2].data(), parts[2].size());
  const std::string expected = utils::hexToBinaryString(parts[3].data(), parts[3].size());
  const std::string actual = pbkdf2(password, salt, std::stoi(parts[1]));
  return actual.size() == expected.size() && CRYPTO_memcmp(actual.data(), expected.data(), actual.size()) == 0;
}

std::string compactJson(const Json::Value &value) {
  Json::StreamWriterBuilder writer;
  writer["indentation"] = "";
  return Json::writeString(writer, value);
}

// Tokens are base64url(JSON {uid, exp, jti}) "." base64url(HMAC-SHA256(secret, that payload)).
class Tokens {
public:
  explicit Tokens(std::string secret) : secret_(std::move(secret)) {}

  std::string issue(std::int64_t userId) const {
    Json::Value claims;
    claims["uid"] = Json::Int64{userId};
    claims["exp"] = Json::Int64{now() + TOKEN_SECONDS};
    claims["jti"] = base64url(randomBytes(16));
    const std::string payload = base64url(compactJson(claims));
    return payload + "." + signature(payload);
  }

  std::optional<Claims> read(std::string_view token) const {
    const std::size_t dot = token.find('.');
    if (dot == std::string_view::npos) {
      return std::nullopt;
    }
    const std::string_view payload = token.substr(0, dot);
    const std::string_view given = token.substr(dot + 1);
    const std::string expected = signature(payload);
    if (given.size() != expected.size() || CRYPTO_memcmp(given.data(), expected.data(), given.size()) != 0) {
      return std::nullopt;
    }
    const std::string text = utils::base64Decode(payload);
    Json::Value claims;
    const std::unique_ptr<Json::CharReader> reader(Json::CharReaderBuilder().newCharReader());
    if (!reader->parse(text.data(), text.data() + text.size(), &claims, nullptr) || !claims.isObject() ||
        !claims["uid"].isInt64() || !claims["exp"].isInt64() || claims["exp"].asInt64() < now()) {
      return std::nullopt;
    }
    return Claims{claims["uid"].asInt64(), claims["exp"].asInt64()};
  }

private:
  std::string signature(std::string_view payload) const {
    unsigned char mac[EVP_MAX_MD_SIZE];
    unsigned int size = 0;
    if (HMAC(EVP_sha256(), secret_.data(), static_cast<int>(secret_.size()),
             reinterpret_cast<const unsigned char *>(payload.data()), payload.size(), mac, &size) == nullptr) {
      throw std::runtime_error("HMAC failed");
    }
    return utils::base64EncodeUnpadded(mac, size, true);
  }

  std::string secret_;
};

using Parameter = std::variant<std::int64_t, std::string>;

// A prepared statement with its parameters bound; step() moves to the next row.
class Statement {
public:
  Statement(sqlite3 *db, const std::string &sql, std::initializer_list<Parameter> parameters) : db_(db) {
    sqlite3_stmt *statement = nullptr;
    check(sqlite3_prepare_v2(db, sql.c_str(), -1, &statement, nullptr));
    statement_.reset(statement);
    int index = 0;
    for (const Parameter &parameter : parameters) {
      ++index;
      if (const auto *number = std::get_if<std::int64_t>(&parameter)) {
        check(sqlite3_bind_int64(statement, index, *number));
      } else {
        const std::string &text = std::get<std::string>(parameter);
        check(sqlite3_bind_text(statement, index, text.data(), static_cast<int>(text.size()), SQLITE_TRANSIENT));
      }
    }
  }

  bool step() {
    const int result = sqlite3_step(statement_.get());
    if (result != SQLITE_ROW) {
      check(result == SQLITE_DONE ? SQLITE_OK : result);
    }
    return result == SQLITE_ROW;
  }

  std::int64_t integer(int column) const { return sqlite3_column_int64(statement_.get(), column); }

  std::string text(int column) const {
    const auto *data = reinterpret_cast<const char *>(sqlite3_column_text(statement_.get(), column));
    return data == nullptr ? std::string() : std::string(data, sqlite3_column_bytes(statement_.get(), column));
  }

private:
  void check(int result) const {
    if (result != SQLITE_OK) {
      throw DatabaseError(sqlite3_extended_errcode(db_), sqlite3_errmsg(db_));
    }
  }

  sqlite3 *db_;
  std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement_{nullptr, &sqlite3_finalize};
};

// One SQLite connection for the whole program, used by one request at a time.
class Database {
public:
  explicit Database(const std::string &path) {
    sqlite3 *db = nullptr;
    const int opened =
        sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr);
    db_.reset(db);
    if (opened != SQLITE_OK) {
      throw DatabaseError(opened, db == nullptr ? "out of memory" : sqlite3_errmsg(db));
    }
    sqlite3_busy_timeout(db, 5000);
    Statement(db,
              "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
              "name TEXT NOT NULL, password_hash TEXT NOT NULL)",
              {})
        .step();
  }

  // The new user's id, or nothing when the email is already registered.
  std::optional<std::int64_t> addUser(const std::string &email, const std::string &name,
                                      const std::string &passwordHash) {
    std::lock_guard lock(mutex_);
    try {
      Statement insert(db_.get(), "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
                       {email, name, passwordHash});
      insert.step();
    } catch (const DatabaseError &error) {
      if (error.code == SQLITE_CONSTRAINT_UNIQUE) {
        return std::nullopt;
      }
      throw;
    }
    return sqlite3_last_insert_rowid(db_.get());
  }

  // The id and stored password hash of the user with this email.
  std::optional<std::pair<std::int64_t, std::string>> findLogin(const std::string &email) {
    std::lock_guard lock(mutex_);
    Statement select(db_.get(), "SELECT id, password_hash FROM users WHERE email = ?", {email});
    if (!select.step()) {
      return std::nullopt;
    }
    return std::pair{select.integer(0), select.text(1)};
  }

  std::optional<User> findUser(std::int64_t id) {
    std::lock_guard lock(mutex_);
    Statement select(db_.get(), "SELECT id, email, name FROM users WHERE id = ?", {id});
    if (!select.step()) {
      return std::nullopt;
    }
    return User{select.integer(0), select.text(1), select.text(2)};
  }

private:
  std::mutex mutex_;
  std::unique_ptr<sqlite3, decltype(&sqlite3_close)> db_{nullptr, &sqlite3_close};
};

HttpResponsePtr json(const Json::Value &body, HttpStatusCode status = k200OK) {
  auto response = HttpResponse::newHttpJsonResponse(body);
  response->setStatusCode(status);
  return response;
}

HttpResponsePtr errorResponse(HttpStatusCode status, const std::string &message) {
  Json::Value body;
  body["error"] = message;
  return json(body, status);
}

Json::Value userJson(const User &user) {
  Json::Value body;
  body["id"] = Json::Int64{user.id};
  body["email"] = user.email;
  body["name"] = user.name;
  return body;
}

} // namespace

int main() {
  const char *secret = std::getenv("APP_SECRET");
  if (secret == nullptr || *secret == '\0') {
    std::cerr << "APP_SECRET must be set\n";
    return 1;
  }
  const char *dbPath = std::getenv("DB_PATH");
  const char *port = std::getenv("PORT");
  Database db(dbPath == nullptr ? "app.db" : dbPath);
  const Tokens tokens(secret);
  const std::string unknownUserHash = hashPassword(base64url(randomBytes(16)));

  // The user signed in with "Authorization: Bearer <token>", or a 401 error.
  const auto currentUser = [&](const HttpRequestPtr &req) {
    const std::string &header = req->getHeader("authorization");
    const auto claims = header.starts_with("Bearer ") ? tokens.read(std::string_view(header).substr(7)) : std::nullopt;
    const auto user = claims ? db.findUser(claims->uid) : std::nullopt;
    if (!user) {
      throw HttpError(k401Unauthorized, "missing or invalid token");
    }
    return *user;
  };

  app().registerHandler("/health",
                        [](const HttpRequestPtr &, Callback &&callback) {
                          Json::Value body;
                          body["status"] = "ok";
                          callback(json(body));
                        },
                        {Get});

  app().registerHandler("/register",
                        [&](const HttpRequestPtr &req, Callback &&callback) {
                          const Json::Value &body = jsonObject(req);
                          const std::string email = lower(trim(field(body, "email")));
                          const std::string password = field(body, "password");
                          const std::string name = trim(field(body, "name"));
                          if (length(email) > 254 || !validEmail(email) || length(password) < 8 ||
                              length(password) > 128 || length(name) < 1 || length(name) > 40) {
                            throw HttpError(k400BadRequest, "invalid email, password or name");
                          }
                          const auto id = db.addUser(email, name, hashPassword(password));
                          if (!id) {
                            throw HttpError(k409Conflict, "email already registered");
                          }
                          Json::Value created;
                          created["id"] = Json::Int64{*id};
                          callback(json(created, k201Created));
                        },
                        {Post});

  app().registerHandler("/login",
                        [&](const HttpRequestPtr &req, Callback &&callback) {
                          const Json::Value &body = jsonObject(req);
                          const std::string email = lower(trim(field(body, "email")));
                          const std::string password = field(body, "password");
                          const auto login = db.findLogin(email);
                          const bool matches = passwordMatches(password, login ? login->second : unknownUserHash);
                          if (!login || !matches) {
                            throw HttpError(k401Unauthorized, "wrong email or password");
                          }
                          Json::Value issued;
                          issued["token"] = tokens.issue(login->first);
                          callback(json(issued));
                        },
                        {Post});

  app().registerHandler(
      "/me", [&](const HttpRequestPtr &req, Callback &&callback) { callback(json(userJson(currentUser(req)))); },
      {Get});

  app().registerHandler("/users/{id}",
                        [&](const HttpRequestPtr &req, Callback &&callback, const std::string &id) {
                          const User user = currentUser(req);
                          if (id != std::to_string(user.id)) {
                            throw HttpError(k403Forbidden, "not your account");
                          }
                          callback(json(userJson(user)));
                        },
                        {Get});

  app()
      .setClientMaxBodySize(MAX_BODY) // a larger body is refused with 413 before it is read
      .setThreadNum(0)                // one IO thread per core, so a slow password hash holds up few requests
      .setFileTypes({})               // an API only: serve no files from the working folder
      .enableServerHeader(false)
      .setCustomErrorHandler([](HttpStatusCode status, const HttpRequestPtr &) {
        // errors the framework answers itself, such as an unknown route or method
        std::string reason(statusCodeToString(status));
        std::ranges::transform(reason, reason.begin(), [](unsigned char c) { return std::tolower(c); });
        return errorResponse(status, reason);
      })
      .setExceptionHandler([](const std::exception &e, const HttpRequestPtr &, Callback &&callback) {
        if (const auto *refusal = dynamic_cast<const HttpError *>(&e)) {
          callback(errorResponse(refusal->status, refusal->what()));
        } else {
          LOG_ERROR << e.what();
          callback(errorResponse(k500InternalServerError, "internal error"));
        }
      })
      .addListener("0.0.0.0", static_cast<std::uint16_t>(std::stoi(port == nullptr ? "8080" : port)))
      .run();
}
