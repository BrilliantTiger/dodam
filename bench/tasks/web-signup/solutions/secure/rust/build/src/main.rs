//! Sign-up and login API: axum, rusqlite, Argon2 password hashes and HMAC-signed tokens.
use std::env;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{SystemTime, UNIX_EPOCH};

use argon2::{Argon2, PasswordHasher, PasswordVerifier};
use axum::extract::rejection::PathRejection;
use axum::extract::{DefaultBodyLimit, FromRequest, FromRequestParts, Path, Request, State};
use axum::http::{StatusCode, header::AUTHORIZATION, request::Parts};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, KeyInit, Mac};
use rusqlite::{Connection, OptionalExtension, ffi, params};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Map, Value, json};
use sha2::Sha256;
use tokio::sync::Semaphore;

const MAX_BODY: usize = 64 * 1024;
const TOKEN_SECONDS: i64 = 3600;
const INVALID_BODY: ApiError = ApiError(StatusCode::BAD_REQUEST, "invalid request body");
const UNAUTHORIZED: ApiError = ApiError(StatusCode::UNAUTHORIZED, "missing or invalid token");

/// Argon2 takes 19 MiB for each hash, so only a few hashes run at once, off the async threads.
static HASHING: Semaphore = Semaphore::const_new(4);

type HmacSha256 = Hmac<Sha256>;

struct App {
    db: Mutex<Connection>,
    signer: HmacSha256,
    unknown_user_hash: String,
}

#[derive(Serialize)]
struct User {
    id: i64,
    email: String,
    name: String,
}

#[derive(Deserialize)]
struct Signup {
    email: String,
    password: String,
    name: String,
}

#[derive(Deserialize)]
struct Credentials {
    email: String,
    password: String,
}

#[derive(Deserialize)]
struct Claims {
    uid: i64,
    exp: i64,
}

/// An error answer: the status with the body {"error": message}.
struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1 }))).into_response()
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(err: rusqlite::Error) -> Self {
        internal(err)
    }
}

fn internal(err: impl std::fmt::Display) -> ApiError {
    eprintln!("internal error: {err}");
    ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
}

/// A JSON object body read into `T`; any other body is refused with a JSON error.
struct JsonBody<T>(T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for JsonBody<T> {
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, ApiError> {
        let Json(object) = Json::<Map<String, Value>>::from_request(req, state)
            .await
            .map_err(|rejection| match rejection.status() {
                StatusCode::PAYLOAD_TOO_LARGE => {
                    ApiError(StatusCode::PAYLOAD_TOO_LARGE, "request body too large")
                }
                _ => INVALID_BODY,
            })?;
        serde_json::from_value(Value::Object(object))
            .map(JsonBody)
            .map_err(|_| INVALID_BODY)
    }
}

/// The account behind a valid bearer token; any other request is refused with 401.
struct CurrentUser(User);

impl FromRequestParts<Arc<App>> for CurrentUser {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, app: &Arc<App>) -> Result<Self, ApiError> {
        let claims = parts
            .headers
            .get(AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.strip_prefix("Bearer "))
            .and_then(|token| app.read_token(token))
            .ok_or(UNAUTHORIZED)?;
        let user = app
            .db()
            .query_row(
                "SELECT id, email, name FROM users WHERE id = ?1",
                [claims.uid],
                |row| {
                    Ok(User {
                        id: row.get(0)?,
                        email: row.get(1)?,
                        name: row.get(2)?,
                    })
                },
            )
            .optional()?;
        user.map(CurrentUser).ok_or(UNAUTHORIZED)
    }
}

impl App {
    fn db(&self) -> MutexGuard<'_, Connection> {
        // a panic while the lock was held leaves the connection itself usable
        self.db.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn sign(&self, payload: &str) -> HmacSha256 {
        let mut mac = self.signer.clone();
        mac.update(payload.as_bytes());
        mac
    }

    fn issue_token(&self, uid: i64) -> String {
        let jti: [u8; 16] = rand::random();
        let claims =
            json!({ "uid": uid, "exp": now() + TOKEN_SECONDS, "jti": URL_SAFE_NO_PAD.encode(jti) });
        let payload = URL_SAFE_NO_PAD.encode(claims.to_string());
        let signature = URL_SAFE_NO_PAD.encode(self.sign(&payload).finalize().into_bytes());
        format!("{payload}.{signature}")
    }

    fn read_token(&self, token: &str) -> Option<Claims> {
        let (payload, signature) = token.split_once('.')?;
        let signature = URL_SAFE_NO_PAD.decode(signature).ok()?;
        // verify_slice compares in constant time
        self.sign(payload).verify_slice(&signature).ok()?;
        let claims: Claims = serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).ok()?).ok()?;
        (claims.exp >= now()).then_some(claims)
    }
}

fn now() -> i64 {
    let since_epoch = SystemTime::now().duration_since(UNIX_EPOCH);
    since_epoch.map_or(0, |elapsed| elapsed.as_secs() as i64)
}

/// Runs Argon2 work on a blocking thread and keeps its permit until the work ends.
async fn hashing<T: Send + 'static>(
    work: impl FnOnce() -> T + Send + 'static,
) -> Result<T, ApiError> {
    let permit = HASHING.acquire().await.map_err(internal)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        work()
    })
    .await
    .map_err(internal)
}

/// Argon2id with its default parameters and a random salt, stored as a PHC string.
async fn hash_password(password: String) -> Result<String, ApiError> {
    hashing(move || {
        Argon2::default()
            .hash_password(password.as_bytes())
            .map(|hash| hash.to_string())
    })
    .await?
    .map_err(internal)
}

/// Rehashes with the salt and parameters of the stored hash and compares in constant time.
async fn password_matches(password: String, stored: String) -> Result<bool, ApiError> {
    hashing(move || {
        Argon2::default()
            .verify_password(password.as_bytes(), stored.as_str())
            .is_ok()
    })
    .await
}

/// Length in Unicode code points.
fn length(text: &str) -> usize {
    text.chars().count()
}

/// One @ with something before it and a dot after it, no white space, at most 254 characters.
fn valid_email(email: &str) -> bool {
    let Some((local, domain)) = email.split_once('@') else {
        return false;
    };
    length(email) <= 254
        && !local.is_empty()
        && domain.contains('.')
        && !domain.contains('@')
        && !email.contains(char::is_whitespace)
}

fn valid_name(name: &str) -> bool {
    (1..=40).contains(&length(name))
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn register(
    State(app): State<Arc<App>>,
    JsonBody(signup): JsonBody<Signup>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let email = signup.email.trim().to_lowercase();
    let name = signup.name.trim();
    if !valid_email(&email) || !(8..=128).contains(&length(&signup.password)) || !valid_name(name) {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "invalid email, password or name",
        ));
    }
    let password_hash = hash_password(signup.password).await?;
    let db = app.db();
    match db.execute(
        "INSERT INTO users (email, name, password_hash) VALUES (?1, ?2, ?3)",
        params![email, name, password_hash],
    ) {
        Ok(_) => Ok((
            StatusCode::CREATED,
            Json(json!({ "id": db.last_insert_rowid() })),
        )),
        Err(rusqlite::Error::SqliteFailure(
            ffi::Error {
                extended_code: ffi::SQLITE_CONSTRAINT_UNIQUE,
                ..
            },
            _,
        )) => Err(ApiError(StatusCode::CONFLICT, "email already registered")),
        Err(err) => Err(err.into()),
    }
}

async fn login(
    State(app): State<Arc<App>>,
    JsonBody(credentials): JsonBody<Credentials>,
) -> Result<Json<Value>, ApiError> {
    let email = credentials.email.trim().to_lowercase();
    let account: Option<(i64, String)> = app
        .db()
        .query_row(
            "SELECT id, password_hash FROM users WHERE email = ?1",
            [&email],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    // an unknown email costs one hash too, so the answer time does not tell which emails exist
    let stored = account
        .as_ref()
        .map_or(&app.unknown_user_hash, |(_, hash)| hash)
        .clone();
    let matches = password_matches(credentials.password, stored).await?;
    match account {
        Some((id, _)) if matches => Ok(Json(json!({ "token": app.issue_token(id) }))),
        _ => Err(ApiError(
            StatusCode::UNAUTHORIZED,
            "wrong email or password",
        )),
    }
}

async fn me(CurrentUser(user): CurrentUser) -> Json<User> {
    Json(user)
}

async fn user_by_id(
    CurrentUser(user): CurrentUser,
    id: Result<Path<String>, PathRejection>,
) -> Result<Json<User>, ApiError> {
    // an id that cannot be read as text is not the caller's either
    match id {
        Ok(Path(id)) if id == user.id.to_string() => Ok(Json(user)),
        _ => Err(ApiError(StatusCode::FORBIDDEN, "not your account")),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db = Connection::open(env::var("DB_PATH")?)?;
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
         name TEXT NOT NULL, password_hash TEXT NOT NULL)",
    )?;
    let app = Arc::new(App {
        db: Mutex::new(db),
        signer: HmacSha256::new_from_slice(env::var("APP_SECRET")?.as_bytes())?,
        unknown_user_hash: Argon2::default()
            .hash_password(&rand::random::<[u8; 16]>())?
            .to_string(),
    });
    let router = Router::new()
        .route("/health", get(health))
        .route("/register", post(register))
        .route("/login", post(login))
        .route("/me", get(me))
        .route("/users/{id}", get(user_by_id))
        .method_not_allowed_fallback(|| async {
            ApiError(StatusCode::METHOD_NOT_ALLOWED, "method not allowed")
        })
        .fallback(|| async { ApiError(StatusCode::NOT_FOUND, "not found") })
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .with_state(app);
    let port: u16 = env::var("PORT").map_or(Ok(8080), |port| port.parse())?;
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    axum::serve(listener, router).await?;
    Ok(())
}
