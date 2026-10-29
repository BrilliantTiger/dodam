// Sign-up and login API: Express, better-sqlite3, scrypt password hashes and HMAC-signed tokens.
import { createHmac, randomBytes, scrypt as scryptCallback, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";
import Database from "better-sqlite3";
import express, { NextFunction, Request, Response } from "express";

const scrypt = promisify(scryptCallback) as (password: string, salt: Buffer, keylen: number, options: object) => Promise<Buffer>;
const SECRET = process.env.APP_SECRET ?? "";
const TOKEN_SECONDS = 3600;
const SCRYPT = { N: 16384, r: 8, p: 1 };
const EMAIL = /^[^@\s]+@[^@\s]*\.[^@\s]*$/;

const db = new Database(process.env.DB_PATH ?? "app.db");
db.exec(`CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
         name TEXT NOT NULL, password_hash TEXT NOT NULL)`);

type User = { id: number; email: string; name: string };

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const length = (text: string) => [...text].length;

function strings(body: unknown, fields: string[]): Record<string, string> {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new HttpError(400, "invalid request body");
  }
  const values: Record<string, string> = {};
  for (const field of fields) {
    const value = (body as Record<string, unknown>)[field];
    if (typeof value !== "string") {
      throw new HttpError(400, "invalid request body");
    }
    values[field] = value;
  }
  return values;
}

async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const digest = await scrypt(password, salt, 64, SCRYPT);
  return `scrypt$${salt.toString("hex")}$${digest.toString("hex")}`;
}

async function passwordMatches(password: string, stored: string): Promise<boolean> {
  const [, salt, digest] = stored.split("$");
  const candidate = await scrypt(password, Buffer.from(salt, "hex"), 64, SCRYPT);
  return timingSafeEqual(candidate, Buffer.from(digest, "hex"));
}

const unknownUserHash = hashPassword(randomBytes(16).toString("hex"));

const b64 = (data: Buffer | string) => Buffer.from(data).toString("base64url");
const signature = (payload: string) => b64(createHmac("sha256", SECRET).update(payload).digest());

function issueToken(userId: number): string {
  const claims = { uid: userId, exp: Math.floor(Date.now() / 1000) + TOKEN_SECONDS, jti: randomBytes(16).toString("base64url") };
  const payload = b64(JSON.stringify(claims));
  return `${payload}.${signature(payload)}`;
}

function readToken(token: string): { uid: number; exp: number } | null {
  const dot = token.indexOf(".");
  if (dot < 0) {
    return null;
  }
  const payload = token.slice(0, dot);
  const given = Buffer.from(token.slice(dot + 1));
  const expected = Buffer.from(signature(payload));
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return null;
  }
  try {
    const claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (Number.isInteger(claims?.uid) && Number.isInteger(claims?.exp) && claims.exp >= Date.now() / 1000) {
      return claims;
    }
  } catch {
    // a payload we signed always parses; anything else is refused below
  }
  return null;
}

function currentUser(req: Request): User {
  const [scheme, token] = (req.get("authorization") ?? "").split(" ", 2);
  const claims = scheme === "Bearer" && token ? readToken(token) : null;
  const user = claims ? (db.prepare("SELECT id, email, name FROM users WHERE id = ?").get(claims.uid) as User | undefined) : undefined;
  if (!user) {
    throw new HttpError(401, "missing or invalid token");
  }
  return user;
}

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: 64 * 1024 }));

app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

app.post("/register", async (req, res) => {
  const { email: rawEmail, password, name: rawName } = strings(req.body, ["email", "password", "name"]);
  const email = rawEmail.trim().toLowerCase();
  const name = rawName.trim();
  if (length(email) > 254 || !EMAIL.test(email) || length(password) < 8 || length(password) > 128
      || length(name) < 1 || length(name) > 40) {
    throw new HttpError(400, "invalid email, password or name");
  }
  const passwordHash = await hashPassword(password);
  try {
    const result = db.prepare("INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)").run(email, name, passwordHash);
    res.status(201).json({ id: Number(result.lastInsertRowid) });
  } catch (err) {
    if ((err as { code?: string }).code === "SQLITE_CONSTRAINT_UNIQUE") {
      throw new HttpError(409, "email already registered");
    }
    throw err;
  }
});

app.post("/login", async (req, res) => {
  const { email, password } = strings(req.body, ["email", "password"]);
  const row = db.prepare("SELECT id, password_hash FROM users WHERE email = ?").get(email.trim().toLowerCase()) as
    { id: number; password_hash: string } | undefined;
  const matches = await passwordMatches(password, row ? row.password_hash : await unknownUserHash);
  if (!row || !matches) {
    throw new HttpError(401, "wrong email or password");
  }
  res.json({ token: issueToken(row.id) });
});

app.get("/me", (req, res) => {
  res.json(currentUser(req));
});

app.get("/users/:id", (req, res) => {
  const user = currentUser(req);
  if (req.params.id !== String(user.id)) {
    throw new HttpError(403, "not your account");
  }
  res.json(user);
});

app.use((_req, res) => {
  res.status(404).json({ error: "not found" });
});

app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  const status = (err as { status?: number }).status ?? 500;
  const message = err instanceof HttpError ? err.message
    : status === 413 ? "request body too large"
    : status < 500 ? "invalid request body" : "internal error";
  res.status(status).json({ error: message });
});

app.listen(Number(process.env.PORT ?? 8080), "0.0.0.0");
