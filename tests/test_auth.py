import time

from src.api import auth


def _user_row(password="secret123", age_seconds=0):
    salt = b"0123456789abcdef"
    return {
        "username": "alice",
        "salt": salt.hex(),
        "password_hash": auth._hash_password(password, salt),
        "created_at": int(time.time()) - age_seconds,
    }


def test_password_hash_round_trip():
    salt = b"0123456789abcdef"
    digest = auth._hash_password("secret123", salt)

    assert auth._verify_password("secret123", salt.hex(), digest) is True
    assert auth._verify_password("wrong", salt.hex(), digest) is False


def test_register_and_login(client, monkeypatch):
    users = []
    tokens = []

    def query(sql, args):
        if "SELECT 1 FROM users" in sql:
            return [{"1": 1}] if any(u["username"] == args[0] for u in users) else []
        if "SELECT * FROM users" in sql:
            return [u for u in users if u["username"] == args[0]]
        return []

    def execute(sql, args):
        if "INSERT INTO users" in sql:
            users.append(
                {
                    "username": args[0],
                    "password_hash": args[1],
                    "salt": args[2],
                    "question": args[3],
                    "answer_hash": args[4],
                    "created_at": args[5],
                }
            )
        elif "INSERT INTO tokens" in sql:
            tokens.append(args)
        return 1

    monkeypatch.setattr(auth.db, "query", query)
    monkeypatch.setattr(auth.db, "execute", execute)

    register = client.post(
        "/auth/register",
        json={
            "username": "alice",
            "password": "secret123",
            "question": "security question",
            "answer": "answer",
        },
    )
    login = client.post(
        "/auth/login",
        json={"username": "alice", "password": "secret123"},
    )

    assert register.status_code == 200
    assert login.status_code == 200
    assert len(login.json()["token"]) == 64
    assert tokens[0][1] == "alice"
    assert tokens[0][0] == auth.hash_token(login.json()["token"])
    assert tokens[0][0] != login.json()["token"]
    assert client.cookies.get(auth.SESSION_COOKIE_NAME) == login.json()["token"]
    assert "HttpOnly" in login.headers["set-cookie"]


def test_login_rejects_bad_password(client, monkeypatch):
    row = _user_row()
    monkeypatch.setattr(auth.db, "query", lambda *_args: [row])

    response = client.post(
        "/auth/login",
        json={"username": "alice", "password": "wrong"},
    )

    assert response.status_code == 401


def test_expired_token_is_deleted(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda *_args: [{"username": "alice", "created_at": 0}],
    )
    monkeypatch.setattr(
        auth.db,
        "execute",
        lambda sql, args: deleted.append((sql, args)) or 1,
    )

    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer expired-token"},
    )

    assert response.status_code == 401
    assert deleted


def test_cleanup_expired_tokens(monkeypatch):
    captured = {}

    def execute(sql, args):
        captured["sql"] = sql
        captured["args"] = args
        return 3

    monkeypatch.setattr(auth.db, "execute", execute)

    assert auth.cleanup_expired_tokens() == 3
    assert captured["args"][0] <= int(time.time()) - auth.TOKEN_TTL


def test_legacy_plaintext_token_is_migrated(client, monkeypatch):
    raw_token = "legacy-token"
    updates = []

    def query(sql, args):
        if "is_hashed = 1" in sql:
            return []
        return [{"username": "alice", "created_at": int(time.time())}]

    monkeypatch.setattr(auth.db, "query", query)
    monkeypatch.setattr(
        auth.db,
        "execute",
        lambda sql, args: updates.append((sql, args)) or 1,
    )

    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert updates[0][1] == (auth.hash_token(raw_token), raw_token)


def test_session_cookie_can_authenticate(client, monkeypatch):
    raw_token = "cookie-token"
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda *_args: [{"username": "alice", "created_at": int(time.time())}],
    )
    client.cookies.set(auth.SESSION_COOKIE_NAME, raw_token)

    response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


def test_query_token_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(auth, "ALLOW_LEGACY_QUERY_TOKEN", False)

    response = client.get("/auth/me?token=legacy-query-token")

    assert response.status_code == 401


def test_refresh_session_sets_cookie_from_bearer_token(client, monkeypatch):
    raw_token = "refreshed-token"
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda *_args: [{"username": "alice", "created_at": int(time.time())}],
    )

    response = client.post(
        "/auth/session",
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert client.cookies.get(auth.SESSION_COOKIE_NAME) == raw_token
