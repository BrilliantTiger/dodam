// Sign-up and login API: ASP.NET Core minimal APIs, Microsoft.Data.Sqlite, PBKDF2 password hashes and HMAC-signed tokens.
using System.Buffers.Text;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.WebUtilities;
using Microsoft.Data.Sqlite;

const int Pbkdf2Iterations = 600_000;
const long TokenSeconds = 3600;

var secret = Environment.GetEnvironmentVariable("APP_SECRET") is { Length: > 0 } configured
    ? Encoding.UTF8.GetBytes(configured) : throw new InvalidOperationException("APP_SECRET is not set");
var port = Environment.GetEnvironmentVariable("PORT") ?? "8080";
var dbPath = Environment.GetEnvironmentVariable("DB_PATH") ?? "app.db";
var database = new SqliteConnectionStringBuilder { DataSource = dbPath }.ToString();
var emailPattern = new Regex(@"^[^@\s]+@[^@\s]*\.[^@\s]*\z");

Query("""
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, password_hash TEXT NOT NULL)
    """);
var unknownUserHash = HashPassword(Convert.ToHexString(RandomNumberGenerator.GetBytes(16)));

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(kestrel => kestrel.Limits.MaxRequestBodySize = 64 * 1024);
var app = builder.Build();

// Errors the framework answers without a body, such as an unknown route or method, get a JSON one.
app.UseStatusCodePages(async context =>
{
    var response = context.HttpContext.Response;
    await response.WriteAsJsonAsync(new { error = ReasonPhrases.GetReasonPhrase(response.StatusCode).ToLowerInvariant() });
});

// Errors thrown while handling a request, including a body over the limit, are answered as JSON.
app.Use(async (context, next) =>
{
    try
    {
        await next(context);
    }
    catch (Exception error) when (!context.Response.HasStarted)
    {
        var (status, message) = error switch
        {
            HttpError e => (e.Status, e.Message),
            BadHttpRequestException { StatusCode: 413 } => (413, "request body too large"),
            BadHttpRequestException e => (e.StatusCode, "invalid request body"),
            _ => (500, "internal error"),
        };
        if (status == 500)
        {
            app.Logger.LogError(error, "request failed");
        }
        context.Response.Clear();
        context.Response.StatusCode = status;
        await context.Response.WriteAsJsonAsync(new { error = message });
    }
});

app.MapGet("/health", () => new { status = "ok" });

app.MapPost("/register", async (HttpRequest request) =>
{
    var body = await Strings(request, "email", "password", "name");
    var email = body["email"].Trim().ToLowerInvariant();
    var password = body["password"];
    var name = body["name"].Trim();
    if (Length(email) > 254 || !emailPattern.IsMatch(email) || Length(password) is < 8 or > 128
        || Length(name) is < 1 or > 40)
    {
        throw new HttpError(400, "invalid email, password or name");
    }
    var passwordHash = HashPassword(password);
    try
    {
        var id = (long)Query("INSERT INTO users (email, name, password_hash) VALUES ($email, $name, $hash) RETURNING id",
            ("$email", email), ("$name", name), ("$hash", passwordHash))![0];
        return Results.Created($"/users/{id}", new { id });
    }
    catch (SqliteException e) when (e.SqliteExtendedErrorCode == SQLitePCL.raw.SQLITE_CONSTRAINT_UNIQUE)
    {
        throw new HttpError(409, "email already registered");
    }
});

app.MapPost("/login", async (HttpRequest request) =>
{
    var body = await Strings(request, "email", "password");
    var email = body["email"].Trim().ToLowerInvariant();
    var row = Query("SELECT id, password_hash FROM users WHERE email = $email", ("$email", email));
    var matches = PasswordMatches(body["password"], row is null ? unknownUserHash : (string)row[1]);
    if (row is null || !matches)
    {
        throw new HttpError(401, "wrong email or password");
    }
    return new { token = IssueToken((long)row[0]) };
});

app.MapGet("/me", (HttpRequest request) => CurrentUser(request));

app.MapGet("/users/{id}", (string id, HttpRequest request) =>
{
    var user = CurrentUser(request);
    if (id != user.Id.ToString(CultureInfo.InvariantCulture))
    {
        throw new HttpError(403, "not your account");
    }
    return user;
});

app.Run($"http://0.0.0.0:{port}");

int Length(string text) => text.EnumerateRunes().Count();

// The named string fields of a JSON object body; any other body is a 400.
async Task<Dictionary<string, string>> Strings(HttpRequest request, params string[] fields)
{
    try
    {
        using var body = await JsonDocument.ParseAsync(request.Body);
        var root = body.RootElement;
        if (request.HasJsonContentType() && root.ValueKind == JsonValueKind.Object
            && fields.All(field => root.TryGetProperty(field, out var value) && value.ValueKind == JsonValueKind.String))
        {
            return fields.ToDictionary(field => field, field => root.GetProperty(field).GetString()!);
        }
    }
    catch (Exception e) when (e is JsonException or InvalidOperationException)
    {
        // not JSON, or a string that is not valid Unicode
    }
    throw new HttpError(400, "invalid request body");
}

string HashPassword(string password)
{
    var salt = RandomNumberGenerator.GetBytes(16);
    var digest = Rfc2898DeriveBytes.Pbkdf2(password, salt, Pbkdf2Iterations, HashAlgorithmName.SHA256, 32);
    return $"pbkdf2_sha256${Pbkdf2Iterations}${Convert.ToHexStringLower(salt)}${Convert.ToHexStringLower(digest)}";
}

bool PasswordMatches(string password, string stored)
{
    var parts = stored.Split('$');
    var digest = Convert.FromHexString(parts[3]);
    var candidate = Rfc2898DeriveBytes.Pbkdf2(password, Convert.FromHexString(parts[2]),
        int.Parse(parts[1], CultureInfo.InvariantCulture), HashAlgorithmName.SHA256, digest.Length);
    return CryptographicOperations.FixedTimeEquals(candidate, digest);
}

string Signature(string payload) => Base64Url.EncodeToString(HMACSHA256.HashData(secret, Encoding.UTF8.GetBytes(payload)));

string IssueToken(long userId)
{
    var claims = new Claims(userId, DateTimeOffset.UtcNow.ToUnixTimeSeconds() + TokenSeconds,
        Base64Url.EncodeToString(RandomNumberGenerator.GetBytes(16)));
    var payload = Base64Url.EncodeToString(JsonSerializer.SerializeToUtf8Bytes(claims, JsonSerializerOptions.Web));
    return $"{payload}.{Signature(payload)}";
}

Claims? ReadToken(string token)
{
    var dot = token.IndexOf('.');
    if (dot < 0)
    {
        return null;
    }
    var payload = token[..dot];
    var given = Encoding.UTF8.GetBytes(token[(dot + 1)..]);
    var expected = Encoding.UTF8.GetBytes(Signature(payload));
    if (!CryptographicOperations.FixedTimeEquals(given, expected))
    {
        return null;
    }
    try
    {
        var claims = JsonSerializer.Deserialize<Claims>(Base64Url.DecodeFromChars(payload), JsonSerializerOptions.Web);
        return claims is not null && claims.Exp > DateTimeOffset.UtcNow.ToUnixTimeSeconds() ? claims : null;
    }
    catch (Exception e) when (e is FormatException or JsonException)
    {
        // a payload we signed always reads; anything else is refused
        return null;
    }
}

User CurrentUser(HttpRequest request)
{
    var header = request.Headers.Authorization.ToString();
    var claims = header.StartsWith("Bearer ", StringComparison.Ordinal) ? ReadToken(header["Bearer ".Length..]) : null;
    if (claims is not null && Query("SELECT id, email, name FROM users WHERE id = $id", ("$id", claims.Uid)) is { } row)
    {
        return new User((long)row[0], (string)row[1], (string)row[2]);
    }
    throw new HttpError(401, "missing or invalid token");
}

// Runs one statement, with every value bound as a parameter, on a pooled connection of its own,
// and returns the first row it gives, if any. Locks held by other connections are waited for.
object[]? Query(string sql, params (string Name, object Value)[] values)
{
    using var connection = new SqliteConnection(database);
    connection.Open();
    using var command = connection.CreateCommand();
    command.CommandText = "PRAGMA busy_timeout = 10000";
    command.ExecuteNonQuery();
    command.CommandText = sql;
    foreach (var (name, value) in values)
    {
        command.Parameters.AddWithValue(name, value);
    }
    using var reader = command.ExecuteReader();
    if (!reader.Read())
    {
        return null;
    }
    var row = new object[reader.FieldCount];
    reader.GetValues(row);
    return row;
}

record User(long Id, string Email, string Name);

record Claims(long Uid, long Exp, string Jti);

sealed class HttpError(int status, string message) : Exception(message)
{
    public int Status { get; } = status;
}
