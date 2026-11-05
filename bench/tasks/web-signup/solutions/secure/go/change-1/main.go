// Sign-up and login API: net/http, modernc.org/sqlite, PBKDF2 password hashes and HMAC-signed tokens.
package main

import (
	"cmp"
	"crypto/hmac"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	_ "modernc.org/sqlite"
)

const (
	maxBody       = 64 << 10
	tokenLifetime = time.Hour
	iterations    = 600_000
	hashFormat    = "pbkdf2-sha256$%d$%x$%x" // iterations, salt, key
)

var (
	emailPattern = regexp.MustCompile(`^[^@\s]+@[^@\s]*\.[^@\s]*$`)
	b64          = base64.RawURLEncoding
	errBody      = &httpError{http.StatusBadRequest, "invalid request body"}
	errToken     = &httpError{http.StatusUnauthorized, "missing or invalid token"}
)

type user struct {
	ID    int64  `json:"id"`
	Email string `json:"email"`
	Name  string `json:"name"`
}

type claims struct {
	UID int64  `json:"uid"`
	Exp int64  `json:"exp"`
	JTI string `json:"jti"`
}

// httpError is answered with its own status and message; any other error a handler returns is a 500.
type httpError struct {
	status  int
	message string
}

func (e *httpError) Error() string { return e.message }

type server struct {
	db        *sql.DB
	secret    []byte
	dummyHash string // checked when the email is unknown, so that login answers no faster than for a wrong password
}

func main() {
	secret := os.Getenv("APP_SECRET")
	if secret == "" {
		log.Fatal("APP_SECRET is not set")
	}
	db, err := sql.Open("sqlite", cmp.Or(os.Getenv("DB_PATH"), "app.db")+"?_pragma=busy_timeout(10000)")
	if err != nil {
		log.Fatal(err)
	}
	db.SetMaxOpenConns(1) // SQLite takes one writer at a time; one shared connection never meets SQLITE_BUSY
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
		name TEXT NOT NULL, password_hash TEXT NOT NULL);
		CREATE TABLE IF NOT EXISTS revoked_tokens (jti TEXT PRIMARY KEY, expires INTEGER NOT NULL)`)
	if err != nil {
		log.Fatal(err)
	}
	dummyHash, err := hashPassword(rand.Text())
	if err != nil {
		log.Fatal(err)
	}
	s := &server{db: db, secret: []byte(secret), dummyHash: dummyHash}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /register", handle(s.register))
	mux.HandleFunc("POST /login", handle(s.login))
	mux.HandleFunc("GET /me", handle(s.me))
	mux.HandleFunc("PATCH /me", handle(s.changeName))
	mux.HandleFunc("POST /logout", handle(s.logout))
	mux.HandleFunc("GET /users/{id}", handle(s.userByID))
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusNotFound, map[string]string{"error": "not found"})
	})
	srv := &http.Server{
		Addr:              "0.0.0.0:" + cmp.Or(os.Getenv("PORT"), "8080"),
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Fatal(srv.ListenAndServe())
}

func (s *server) register(w http.ResponseWriter, r *http.Request) error {
	in, err := readStrings(w, r, "email", "password", "name")
	if err != nil {
		return err
	}
	email := strings.ToLower(strings.TrimSpace(in["email"]))
	password, name := in["password"], strings.TrimSpace(in["name"])
	if !lengthIn(email, 1, 254) || !emailPattern.MatchString(email) ||
		!lengthIn(password, 8, 128) || !validName(name) {
		return &httpError{http.StatusBadRequest, "invalid email, password or name"}
	}
	hash, err := hashPassword(password)
	if err != nil {
		return err
	}
	res, err := s.db.Exec(`INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)
		ON CONFLICT(email) DO NOTHING`, email, name, hash)
	if err != nil {
		return err
	}
	added, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if added == 0 {
		return &httpError{http.StatusConflict, "email already registered"}
	}
	id, err := res.LastInsertId()
	if err != nil {
		return err
	}
	reply(w, http.StatusCreated, map[string]int64{"id": id})
	return nil
}

func (s *server) login(w http.ResponseWriter, r *http.Request) error {
	in, err := readStrings(w, r, "email", "password")
	if err != nil {
		return err
	}
	email := strings.ToLower(strings.TrimSpace(in["email"]))
	var id int64
	var hash string
	err = s.db.QueryRow("SELECT id, password_hash FROM users WHERE email = ?", email).Scan(&id, &hash)
	unknown := errors.Is(err, sql.ErrNoRows)
	if unknown {
		hash = s.dummyHash
	} else if err != nil {
		return err
	}
	matches := passwordMatches(in["password"], hash)
	if unknown || !matches {
		return &httpError{http.StatusUnauthorized, "wrong email or password"}
	}
	reply(w, http.StatusOK, map[string]string{"token": s.issueToken(id)})
	return nil
}

func (s *server) me(w http.ResponseWriter, r *http.Request) error {
	u, _, err := s.authenticate(r)
	if err != nil {
		return err
	}
	reply(w, http.StatusOK, u)
	return nil
}

func (s *server) changeName(w http.ResponseWriter, r *http.Request) error {
	u, _, err := s.authenticate(r)
	if err != nil {
		return err
	}
	in, err := readStrings(w, r, "name")
	if err != nil {
		return err
	}
	name := strings.TrimSpace(in["name"])
	if !validName(name) {
		return &httpError{http.StatusBadRequest, "invalid name"}
	}
	if _, err := s.db.Exec("UPDATE users SET name = ? WHERE id = ?", name, u.ID); err != nil {
		return err
	}
	u.Name = name
	reply(w, http.StatusOK, u)
	return nil
}

func (s *server) logout(w http.ResponseWriter, r *http.Request) error {
	_, c, err := s.authenticate(r)
	if err != nil {
		return err
	}
	_, err = s.db.Exec("INSERT OR IGNORE INTO revoked_tokens (jti, expires) VALUES (?, ?)", c.JTI, c.Exp)
	if err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

func (s *server) userByID(w http.ResponseWriter, r *http.Request) error {
	u, _, err := s.authenticate(r)
	if err != nil {
		return err
	}
	if r.PathValue("id") != strconv.FormatInt(u.ID, 10) {
		return &httpError{http.StatusForbidden, "not your account"}
	}
	reply(w, http.StatusOK, u)
	return nil
}

// authenticate returns the user whose valid, unrevoked token the request carries as "Authorization: Bearer <token>",
// and the token's claims.
func (s *server) authenticate(r *http.Request) (user, claims, error) {
	var u user
	token, found := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	c, valid := s.readToken(token)
	if !found || !valid {
		return u, c, errToken
	}
	err := s.db.QueryRow(`SELECT id, email, name FROM users
		WHERE id = ? AND NOT EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ?)`,
		c.UID, c.JTI).Scan(&u.ID, &u.Email, &u.Name)
	if errors.Is(err, sql.ErrNoRows) {
		return u, c, errToken
	}
	return u, c, err
}

// issueToken returns a token for one login: base64url(claims) "." base64url(HMAC-SHA256 of that text).
func (s *server) issueToken(uid int64) string {
	jti := make([]byte, 16) // a fresh random id, so that no two logins share a token
	rand.Read(jti)
	c := claims{UID: uid, Exp: time.Now().Add(tokenLifetime).Unix(), JTI: b64.EncodeToString(jti)}
	data, _ := json.Marshal(c) // a struct of numbers and a string always marshals
	payload := b64.EncodeToString(data)
	return payload + "." + s.sign(payload)
}

// readToken returns the claims of an unexpired token signed with this server's secret.
func (s *server) readToken(token string) (claims, bool) {
	var c claims
	payload, sig, found := strings.Cut(token, ".")
	if !found || !hmac.Equal([]byte(sig), []byte(s.sign(payload))) {
		return c, false
	}
	data, err := b64.DecodeString(payload)
	if err != nil || json.Unmarshal(data, &c) != nil || c.Exp < time.Now().Unix() || c.JTI == "" {
		return claims{}, false
	}
	return c, true
}

// sign returns base64url(HMAC-SHA256(APP_SECRET, payload)).
func (s *server) sign(payload string) string {
	mac := hmac.New(sha256.New, s.secret)
	mac.Write([]byte(payload))
	return b64.EncodeToString(mac.Sum(nil))
}

// hashPassword returns a PBKDF2-HMAC-SHA256 hash with a random salt, written in hashFormat.
func hashPassword(password string) (string, error) {
	salt := make([]byte, 16)
	rand.Read(salt)
	key, err := pbkdf2.Key(sha256.New, password, salt, iterations, 32)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf(hashFormat, iterations, salt, key), nil
}

// passwordMatches checks a password against a hash in hashFormat, comparing in constant time.
func passwordMatches(password, stored string) bool {
	var n int
	var salt, key []byte
	if _, err := fmt.Sscanf(stored, hashFormat, &n, &salt, &key); err != nil {
		return false
	}
	candidate, err := pbkdf2.Key(sha256.New, password, salt, n, len(key))
	return err == nil && subtle.ConstantTimeCompare(candidate, key) == 1
}

// readStrings reads a JSON object body of at most maxBody bytes and returns the named fields, which must be strings.
func readStrings(w http.ResponseWriter, r *http.Request, names ...string) (map[string]string, error) {
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if _, tooBig := errors.AsType[*http.MaxBytesError](err); tooBig {
		return nil, &httpError{http.StatusRequestEntityTooLarge, "request body too large"}
	}
	var body map[string]any
	if err != nil || json.Unmarshal(data, &body) != nil {
		return nil, errBody
	}
	fields := make(map[string]string, len(names))
	for _, name := range names {
		value, ok := body[name].(string)
		if !ok {
			return nil, errBody
		}
		fields[name] = value
	}
	return fields, nil
}

// lengthIn reports whether s has lo to hi characters, counted as Unicode code points.
func lengthIn(s string, lo, hi int) bool {
	n := utf8.RuneCountInString(s)
	return lo <= n && n <= hi
}

// validName is the rule for a trimmed name, at sign-up and when it changes.
func validName(name string) bool { return lengthIn(name, 1, 40) }

// handle turns a handler that returns an error into one that answers the error as JSON.
func handle(h func(http.ResponseWriter, *http.Request) error) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		err := h(w, r)
		if err == nil {
			return
		}
		he, ok := errors.AsType[*httpError](err)
		if !ok {
			log.Printf("%s: %v", r.Pattern, err)
			he = &httpError{http.StatusInternalServerError, "internal error"}
		}
		reply(w, he.status, map[string]string{"error": he.message})
	}
}

// reply writes v as a JSON response with the given status.
func reply(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
