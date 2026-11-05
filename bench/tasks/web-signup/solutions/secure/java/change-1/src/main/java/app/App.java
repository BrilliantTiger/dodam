// Sign-up and login API: Spring Boot, SQLite JDBC, PBKDF2 password hashes and HMAC-signed tokens.
package app;

import static java.nio.charset.StandardCharsets.UTF_8;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ReadListener;
import jakarta.servlet.RequestDispatcher;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Instant;
import java.util.Base64;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.webmvc.error.ErrorController;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.stereotype.Component;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.config.annotation.ContentNegotiationConfigurer;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

@SpringBootApplication(proxyBeanMethods = false)
@RestController
public class App {
  private static final int MAX_BODY = 64 * 1024;
  private static final long TOKEN_SECONDS = 3600;
  private static final int PBKDF2_ITERATIONS = 600_000;
  private static final Pattern EMAIL =
      Pattern.compile("[^@\\s]+@[^@\\s]*\\.[^@\\s]*", Pattern.UNICODE_CHARACTER_CLASS);
  private static final SecureRandom RANDOM = new SecureRandom();
  private static final Base64.Encoder B64 = Base64.getUrlEncoder().withoutPadding();
  private static final HexFormat HEX = HexFormat.of();
  private static final System.Logger LOG = System.getLogger(App.class.getName());

  // An unknown email is checked against this hash, so that it takes as long as a known one.
  private static final String UNKNOWN_USER_HASH = hashPassword(HEX.formatHex(randomBytes(16)));

  private final Connection db;
  private final byte[] secret;
  private final JsonMapper json;

  public static void main(String[] args) {
    SpringApplication app = new SpringApplication(App.class);
    String port = System.getenv().getOrDefault("PORT", "8080");
    app.setDefaultProperties(Map.of("server.address", "0.0.0.0", "server.port", port));
    app.run(args);
  }

  App(JsonMapper json) throws SQLException {
    this.json = json;
    secret = env("APP_SECRET").getBytes(UTF_8);
    db = DriverManager.getConnection("jdbc:sqlite:" + env("DB_PATH"));
    try (Statement statement = db.createStatement()) {
      statement.execute(
          "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
              + " name TEXT NOT NULL, password_hash TEXT NOT NULL)");
      statement.execute(
          "CREATE TABLE IF NOT EXISTS revoked_tokens"
              + " (jti TEXT PRIMARY KEY, expires INTEGER NOT NULL)");
    }
  }

  record User(long id, String email, String name) {
    static User from(ResultSet row) throws SQLException {
      return new User(row.getLong("id"), row.getString("email"), row.getString("name"));
    }
  }

  record Credentials(long id, String passwordHash) {}

  record Claims(long uid, long exp, String jti) {}

  record Session(User user, String jti, long exp) {}

  /** A refused request: its status and the short reason sent as {"error": reason}. */
  static final class HttpError extends RuntimeException {
    final HttpStatus status;

    HttpError(HttpStatus status, String reason) {
      super(reason);
      this.status = status;
    }
  }

  /** Reads one row of a query result. */
  interface Row<T> {
    T read(ResultSet row) throws SQLException;
  }

  private static String env(String name) {
    String value = System.getenv(name);
    if (value == null || value.isEmpty()) {
      throw new IllegalStateException(name + " must be set");
    }
    return value;
  }

  private static int length(String text) {
    return text.codePointCount(0, text.length());
  }

  private static boolean validName(String name) {
    return length(name) >= 1 && length(name) <= 40;
  }

  private static byte[] randomBytes(int count) {
    byte[] bytes = new byte[count];
    RANDOM.nextBytes(bytes);
    return bytes;
  }

  /** The named fields of a JSON object body, each of which must be a string. */
  private static Map<String, String> strings(JsonNode body, String... names) {
    if (body == null || !body.isObject()) {
      throw new HttpError(HttpStatus.BAD_REQUEST, "invalid request body");
    }
    Map<String, String> fields = new HashMap<>();
    for (String name : names) {
      JsonNode value = body.get(name);
      // a lone surrogate, which only a JSON escape can produce, cannot be stored as it was sent
      if (value == null
          || !value.isString()
          || !UTF_8.newEncoder().canEncode(value.stringValue())) {
        throw new HttpError(HttpStatus.BAD_REQUEST, "invalid request body");
      }
      fields.put(name, value.stringValue());
    }
    return fields;
  }

  /** A salted PBKDF2-HMAC-SHA256 hash naming its parameters: pbkdf2-sha256$600000$salt$hash. */
  private static String hashPassword(String password) {
    byte[] salt = randomBytes(16);
    byte[] hash = pbkdf2(password, salt, PBKDF2_ITERATIONS);
    return "pbkdf2-sha256$%d$%s$%s"
        .formatted(PBKDF2_ITERATIONS, HEX.formatHex(salt), HEX.formatHex(hash));
  }

  private static boolean passwordMatches(String password, String stored) {
    String[] parts = stored.split("\\$");
    byte[] hash = pbkdf2(password, HEX.parseHex(parts[2]), Integer.parseInt(parts[1]));
    return MessageDigest.isEqual(hash, HEX.parseHex(parts[3]));
  }

  private static byte[] pbkdf2(String password, byte[] salt, int iterations) {
    try {
      PBEKeySpec spec = new PBEKeySpec(password.toCharArray(), salt, iterations, 256);
      return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded();
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  private String signature(String payload) {
    try {
      Mac mac = Mac.getInstance("HmacSHA256");
      mac.init(new SecretKeySpec(secret, "HmacSHA256"));
      return B64.encodeToString(mac.doFinal(payload.getBytes(UTF_8)));
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  private String issueToken(long userId) {
    long expires = Instant.now().getEpochSecond() + TOKEN_SECONDS;
    Claims claims = new Claims(userId, expires, B64.encodeToString(randomBytes(16)));
    String payload = B64.encodeToString(json.writeValueAsBytes(claims));
    return payload + "." + signature(payload);
  }

  private Claims readToken(String token) {
    int dot = token.indexOf('.');
    if (dot < 0) {
      return null;
    }
    String payload = token.substring(0, dot);
    byte[] given = token.substring(dot + 1).getBytes(UTF_8);
    byte[] expected = signature(payload).getBytes(UTF_8);
    if (!MessageDigest.isEqual(given, expected)) {
      return null;
    }
    try {
      Claims claims = json.readValue(Base64.getUrlDecoder().decode(payload), Claims.class);
      return claims.exp() >= Instant.now().getEpochSecond() && claims.jti() != null
          ? claims
          : null;
    } catch (IllegalArgumentException | JacksonException e) {
      return null; // a payload we signed always reads; anything else is refused
    }
  }

  private static void bind(PreparedStatement statement, Object... parameters) throws SQLException {
    for (int i = 0; i < parameters.length; i++) {
      statement.setObject(i + 1, parameters[i]);
    }
  }

  /** Runs one statement with its parameters bound and reads its first row, or null if none. */
  private <T> T first(String sql, Row<T> reader, Object... parameters) throws SQLException {
    // every request shares this one connection, so they take turns on it
    synchronized (db) {
      try (PreparedStatement statement = db.prepareStatement(sql)) {
        bind(statement, parameters);
        try (ResultSet row = statement.executeQuery()) {
          return row.next() ? reader.read(row) : null;
        }
      }
    }
  }

  /** Runs one statement that returns no rows, with its parameters bound. */
  private void run(String sql, Object... parameters) throws SQLException {
    synchronized (db) {
      try (PreparedStatement statement = db.prepareStatement(sql)) {
        bind(statement, parameters);
        statement.executeUpdate();
      }
    }
  }

  private Session currentSession(HttpHeaders headers) throws SQLException {
    String authorization = headers.getFirst(HttpHeaders.AUTHORIZATION);
    Claims claims =
        authorization != null && authorization.startsWith("Bearer ")
            ? readToken(authorization.substring("Bearer ".length()))
            : null;
    boolean revoked =
        claims != null
            && first("SELECT 1 FROM revoked_tokens WHERE jti = ?", row -> 1, claims.jti()) != null;
    User user =
        claims == null || revoked
            ? null
            : first("SELECT id, email, name FROM users WHERE id = ?", User::from, claims.uid());
    if (user == null) {
      throw new HttpError(HttpStatus.UNAUTHORIZED, "missing or invalid token");
    }
    return new Session(user, claims.jti(), claims.exp());
  }

  @GetMapping("/health")
  Map<String, String> health() {
    return Map.of("status", "ok");
  }

  @ResponseStatus(HttpStatus.CREATED)
  @PostMapping("/register")
  Map<String, Long> register(@RequestBody(required = false) JsonNode body) throws SQLException {
    Map<String, String> fields = strings(body, "email", "password", "name");
    String email = fields.get("email").strip().toLowerCase(Locale.ROOT);
    String password = fields.get("password");
    String name = fields.get("name").strip();
    if (length(email) > 254
        || !EMAIL.matcher(email).matches()
        || length(password) < 8
        || length(password) > 128
        || !validName(name)) {
      throw new HttpError(HttpStatus.BAD_REQUEST, "invalid email, password or name");
    }
    String passwordHash = hashPassword(password);
    // a taken email inserts nothing, so no id comes back
    Long id =
        first(
            "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)"
                + " ON CONFLICT (email) DO NOTHING RETURNING id",
            row -> row.getLong("id"),
            email,
            name,
            passwordHash);
    if (id == null) {
      throw new HttpError(HttpStatus.CONFLICT, "email already registered");
    }
    return Map.of("id", id);
  }

  @PostMapping("/login")
  Map<String, String> login(@RequestBody(required = false) JsonNode body) throws SQLException {
    Map<String, String> fields = strings(body, "email", "password");
    Credentials account =
        first(
            "SELECT id, password_hash FROM users WHERE email = ?",
            row -> new Credentials(row.getLong("id"), row.getString("password_hash")),
            fields.get("email").strip().toLowerCase(Locale.ROOT));
    String stored = account == null ? UNKNOWN_USER_HASH : account.passwordHash();
    boolean matches = passwordMatches(fields.get("password"), stored);
    if (account == null || !matches) {
      throw new HttpError(HttpStatus.UNAUTHORIZED, "wrong email or password");
    }
    return Map.of("token", issueToken(account.id()));
  }

  @GetMapping("/me")
  User me(@RequestHeader HttpHeaders headers) throws SQLException {
    return currentSession(headers).user();
  }

  @PatchMapping("/me")
  User rename(@RequestBody(required = false) JsonNode body, @RequestHeader HttpHeaders headers)
      throws SQLException {
    User user = currentSession(headers).user();
    String name = strings(body, "name").get("name").strip();
    if (!validName(name)) {
      throw new HttpError(HttpStatus.BAD_REQUEST, "invalid name");
    }
    run("UPDATE users SET name = ? WHERE id = ?", name, user.id());
    return new User(user.id(), user.email(), name);
  }

  @ResponseStatus(HttpStatus.NO_CONTENT)
  @PostMapping("/logout")
  void logout(@RequestHeader HttpHeaders headers) throws SQLException {
    Session session = currentSession(headers);
    run(
        "INSERT OR IGNORE INTO revoked_tokens (jti, expires) VALUES (?, ?)",
        session.jti(),
        session.exp());
  }

  @GetMapping("/users/{id}")
  User userById(@PathVariable String id, @RequestHeader HttpHeaders headers) throws SQLException {
    User user = currentSession(headers).user();
    if (!id.equals(Long.toString(user.id()))) {
      throw new HttpError(HttpStatus.FORBIDDEN, "not your account");
    }
    return user;
  }

  static ResponseEntity<Map<String, String>> error(HttpStatus status, String reason) {
    return ResponseEntity.status(status).body(Map.of("error", reason));
  }

  @ExceptionHandler
  ResponseEntity<Map<String, String>> refused(HttpError refusal) {
    return error(refusal.status, refusal.getMessage());
  }

  @ExceptionHandler({
    HttpMessageNotReadableException.class,
    HttpMediaTypeNotSupportedException.class
  })
  ResponseEntity<Map<String, String>> unreadable() {
    return error(HttpStatus.BAD_REQUEST, "invalid request body");
  }

  @ExceptionHandler
  ResponseEntity<Map<String, String>> failed(Exception exception) {
    LOG.log(System.Logger.Level.ERROR, "request failed", exception);
    return error(HttpStatus.INTERNAL_SERVER_ERROR, "internal error");
  }

  /** Answers errors that never reach a handler above, such as unknown routes and wrong methods. */
  @RestController
  static final class Errors implements ErrorController {
    @RequestMapping("/error")
    ResponseEntity<Map<String, String>> error(HttpServletRequest request) {
      Object code = request.getAttribute(RequestDispatcher.ERROR_STATUS_CODE);
      // a client asking for /error itself gets the answer for any unknown route
      HttpStatus status = HttpStatus.resolve(code instanceof Integer value ? value : 404);
      status = status != null ? status : HttpStatus.INTERNAL_SERVER_ERROR;
      return App.error(status, status.getReasonPhrase().toLowerCase(Locale.ROOT));
    }
  }

  /** Every answer is JSON, whatever the Accept header asks for. */
  @Configuration(proxyBeanMethods = false)
  static final class JsonAnswers implements WebMvcConfigurer {
    @Override
    public void configureContentNegotiation(ContentNegotiationConfigurer configurer) {
      configurer.ignoreAcceptHeader(true).defaultContentType(MediaType.APPLICATION_JSON);
    }
  }

  /**
   * Refuses a request body over 64 KiB with 413: by its declared length before reading any of it,
   * or, for a chunked body that declares none, once more than 64 KiB has arrived.
   */
  @Component
  @Order(Ordered.HIGHEST_PRECEDENCE)
  static final class BodyLimit extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(
        HttpServletRequest request, HttpServletResponse response, FilterChain chain)
        throws ServletException, IOException {
      long declared = request.getContentLengthLong();
      if (declared > MAX_BODY) {
        tooLarge(response);
      } else if (declared >= 0) {
        chain.doFilter(request, response);
      } else {
        byte[] body = request.getInputStream().readNBytes(MAX_BODY + 1);
        if (body.length > MAX_BODY) {
          tooLarge(response);
        } else {
          chain.doFilter(new ReadBody(request, body), response);
        }
      }
    }

    private static void tooLarge(HttpServletResponse response) throws IOException {
      response.setStatus(HttpStatus.CONTENT_TOO_LARGE.value());
      response.setContentType(MediaType.APPLICATION_JSON_VALUE);
      response.getOutputStream().write("{\"error\":\"request body too large\"}".getBytes(UTF_8));
    }
  }

  /** A request whose body has been read into memory already. */
  static final class ReadBody extends HttpServletRequestWrapper {
    private final byte[] body;

    ReadBody(HttpServletRequest request, byte[] body) {
      super(request);
      this.body = body;
    }

    @Override
    public int getContentLength() {
      return body.length;
    }

    @Override
    public long getContentLengthLong() {
      return body.length;
    }

    @Override
    public ServletInputStream getInputStream() {
      ByteArrayInputStream in = new ByteArrayInputStream(body);
      return new ServletInputStream() {
        @Override
        public int read() {
          return in.read();
        }

        @Override
        public int read(byte[] buffer, int offset, int length) {
          return in.read(buffer, offset, length);
        }

        @Override
        public boolean isFinished() {
          return in.available() == 0;
        }

        @Override
        public boolean isReady() {
          return true;
        }

        @Override
        public void setReadListener(ReadListener listener) {
          throw new UnsupportedOperationException("the body has been read already");
        }
      };
    }
  }
}
