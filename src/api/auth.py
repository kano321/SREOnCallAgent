"""
Authentication module: register / login / logout / password recovery
via a user-defined security question and answer.

Security notes (production best practices applied here):
- Passwords are NEVER stored in plaintext. They are hashed with PBKDF2-SHA256
  (100k iterations) plus a per-user random salt (stdlib `hashlib`, no deps).
- Security answers are hashed the same way, compared case-insensitively.
- Sessions are random bearer tokens (32 bytes hex), stored server-side,
  with a TTL. Tokens are revoked on logout or password reset.
"""
import hashlib
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from src.api import db

TOKEN_TTL = 7 * 24 * 3600  # 7 days

router = APIRouter(prefix="/auth", tags=["auth"])


def _init_db() -> None:
    db.init_db()


_init_db()


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256, salted)
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 100_000
    ).hex()


def _verify_password(password: str, salt_hex: str, expected_hash: str) -> bool:
    return secrets.compare_digest(
        _hash_password(password, bytes.fromhex(salt_hex)), expected_hash
    )


def _extract_token(request: Request) -> str:
    """Bearer token from the Authorization header, or ?token= query param
    (the query param form exists for EventSource, which cannot set headers)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token", "")


def get_current_user(request: Request) -> str:
    """FastAPI dependency: returns the authenticated username or 401."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    rows = db.query(
        "SELECT username, created_at FROM tokens WHERE token = %s", (token,)
    )
    if not rows:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    row = rows[0]
    if int(time.time()) - row["created_at"] > TOKEN_TTL:
        db.execute("DELETE FROM tokens WHERE token = %s", (token,))
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return row["username"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    question: str = Field(min_length=2, max_length=200)
    answer: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class UsernameRequest(BaseModel):
    username: str


class ForgotResetRequest(BaseModel):
    username: str
    answer: str
    new_password: str = Field(min_length=6, max_length=64)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/register")
def register(req: RegisterRequest):
    """Create a new account with a recovery question/answer."""
    username = req.username.strip()
    if db.query("SELECT 1 FROM users WHERE username = %s", (username,)):
        raise HTTPException(status_code=400, detail="用户名已存在")
    salt = secrets.token_bytes(16)
    db.execute(
        "INSERT INTO users "
        "(username, password_hash, salt, question, answer_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            username,
            _hash_password(req.password, salt),
            salt.hex(),
            req.question.strip(),
            _hash_password(req.answer.lower().strip(), salt),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return {"ok": True}


@router.post("/login")
def login(req: LoginRequest):
    """Verify credentials and issue a bearer token."""
    rows = db.query(
        "SELECT * FROM users WHERE username = %s", (req.username.strip(),)
    )
    row = rows[0] if rows else None
    if not row or not _verify_password(req.password, row["salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = secrets.token_hex(32)
    db.execute(
        "INSERT INTO tokens (token, username, created_at) VALUES (%s, %s, %s)",
        (token, row["username"], int(time.time())),
    )
    return {"token": token, "username": row["username"]}


@router.post("/logout")
def logout(request: Request):
    """Revoke the current token."""
    token = _extract_token(request)
    if token:
        db.execute("DELETE FROM tokens WHERE token = %s", (token,))
    return {"ok": True}


@router.get("/me")
def me(user: str = Depends(get_current_user)):
    """Return the authenticated username (used to validate a token)."""
    return {"username": user}


@router.post("/forgot/question")
def forgot_question(req: UsernameRequest):
    """Step 1 of recovery: reveal the security question for a username."""
    rows = db.query(
        "SELECT question FROM users WHERE username = %s", (req.username.strip(),)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"question": rows[0]["question"]}


@router.post("/forgot/reset")
def forgot_reset(req: ForgotResetRequest):
    """Step 2 of recovery: verify the security answer and set a new password."""
    username = req.username.strip()
    rows = db.query(
        "SELECT salt, answer_hash FROM users WHERE username = %s", (username,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    row = rows[0]
    if not _verify_password(req.answer.lower().strip(), row["salt"], row["answer_hash"]):
        raise HTTPException(status_code=400, detail="安全问题答案错误")
    salt = secrets.token_bytes(16)
    db.execute(
        "UPDATE users SET password_hash = %s, salt = %s WHERE username = %s",
        (_hash_password(req.new_password, salt), salt.hex(), username),
    )
    db.execute("DELETE FROM tokens WHERE username = %s", (username,))
    return {"ok": True}
