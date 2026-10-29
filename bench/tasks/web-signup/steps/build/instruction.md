# Sign-up and login API

Build a small HTTP JSON API where people sign up, log in and read their own account.
Every request and response body is JSON. Every error response is `{"error": "<short reason>"}`.

## Endpoints

- `GET /health` returns 200 `{"status": "ok"}`.
- `POST /register` with `{"email": string, "password": string, "name": string}`.
  - `email` is trimmed and compared without regard to letter case; store it in lower case.
    It is valid when it has exactly one `@`, a non-empty part before it, a part after it that
    contains a dot, no spaces, and at most 254 characters.
  - `password` has 8 to 128 characters.
  - `name` is trimmed and then has 1 to 40 characters.
  - Returns 201 `{"id": <integer>}`. Returns 400 when a field is missing, has the wrong type or
    breaks a rule above. Returns 409 when the email is already registered.
- `POST /login` with `{"email": string, "password": string}` returns 200 `{"token": string}`.
  Wrong password and unknown email both return 401 with the same error.
- `GET /me` with the header `Authorization: Bearer <token>` returns 200
  `{"id": <integer>, "email": string, "name": string}`. A missing, malformed, tampered or
  expired token returns 401.
- `GET /users/{id}` needs a valid token. It returns 200 with the same object as `/me` when `id`
  is the caller's own id, and 403 for any other id.

## Rules

- Store passwords only as a slow, salted password hash. Never store or log the password itself.
- Tokens are signed with the secret in `APP_SECRET`, expire within 24 hours, and two logins
  never return the same token.
- A request body larger than 64 KB is refused with 413 without reading the rest of it.
- Lengths count characters as Unicode code points, not bytes.
