# Change request: log out and edit the profile

Keep everything that already works, and add:

- `POST /logout` with a valid token returns 204. From then on that token is refused with 401
  everywhere. Other tokens of the same person, from other logins, keep working.
- `PATCH /me` with a valid token and `{"name": string}` changes the name with the same rule as
  sign-up (trimmed, then 1 to 40 characters) and returns 200 with the updated `/me` object.
  An invalid name returns 400; a missing or refused token returns 401.
