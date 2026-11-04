# API Reference: Authentication & Sessions

Endpoints for user login, OAuth authorization code exchange, and session lifecycle management.

---

## Google OAuth Flow

### 1. Initiate Login

```http
GET /auth/login
```

Generates a CSRF-protected state parameter and redirects the user's browser to the Google OAuth consent URI.

#### Query Parameters
None.

---

### 2. OAuth Callback Handler

```http
GET /auth/callback
```

Handles the authorization code return from Google, trades the code for OAuth tokens, stores credentials securely, and initializes the user session.

#### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `code` | `String` | **Yes** | Google OAuth authorization code. |
| `state` | `String` | **Yes** | CSRF state verification token. |

#### Response (`200 OK`)

```json
{
  "message": "Authentication successful",
  "user": {
    "id": 4821,
    "email": "user@company.com",
    "name": "Jane Doe"
  }
}
```

---

### 3. Logout Session

```http
POST /auth/logout
```

Clears active session cookies and invalidates tenant authorization states.

#### Response (`200 OK`)

```json
{
  "message": "Successfully logged out"
}
```
