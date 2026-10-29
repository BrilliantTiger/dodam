# Contract summary

HTTP JSON API on the port in PORT. Endpoints: GET /health, POST /register, POST /login, GET /me,
GET /users/{id}; after the change request also POST /logout and PATCH /me. Data lives in SQLite at
DB_PATH. Tokens are signed with APP_SECRET.
