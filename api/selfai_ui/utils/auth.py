import logging
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Dict, Optional, Union

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import WEBUI_SECRET_KEY
from selfai_ui.models.users import Users

log = logging.getLogger(__name__)

SESSION_SECRET = WEBUI_SECRET_KEY
ALGORITHM = "HS256"

##############
# Auth Utils
##############

############################
# JIT Eval Tokens
############################

# In-memory store: {token_str: {user_id, job_id, eval_type, created_at}}
_eval_tokens: Dict[str, dict] = {}
_eval_tokens_lock = threading.Lock()


def create_eval_token(user_id: str, job_id: str, eval_type: str) -> str:
    """Create a short-lived JIT token for an eval job.

    The token authenticates eval container requests as the given user
    and carries job metadata so the UI can identify and log eval traffic.
    """
    token = f"eval-{uuid.uuid4().hex[:16]}"
    with _eval_tokens_lock:
        _eval_tokens[token] = {
            "user_id": user_id,
            "job_id": job_id,
            "eval_type": eval_type,
            "created_at": datetime.now(UTC).isoformat(),
        }
    log.info(f"Created eval token for job {job_id} (user {user_id}, type {eval_type})")
    return token


def revoke_eval_token(token: str) -> None:
    """Revoke a JIT eval token (call when job completes/fails/cancels)."""
    with _eval_tokens_lock:
        removed = _eval_tokens.pop(token, None)
    if removed:
        log.info(f"Revoked eval token for job {removed['job_id']}")


def revoke_eval_tokens_for_job(job_id: str) -> None:
    """Revoke all JIT tokens associated with a given job ID."""
    with _eval_tokens_lock:
        to_remove = [t for t, info in _eval_tokens.items() if info["job_id"] == job_id]
        for t in to_remove:
            del _eval_tokens[t]
    if to_remove:
        log.info(f"Revoked {len(to_remove)} eval token(s) for job {job_id}")


def get_eval_token_info(token: str) -> Optional[dict]:
    """Look up a JIT eval token. Returns metadata dict or None."""
    with _eval_tokens_lock:
        return _eval_tokens.get(token)


bearer_security = HTTPBearer(auto_error=False)

# bcrypt consumes at most 72 bytes and ignores everything past that. passlib
# truncated silently; bcrypt >= 5 raises instead. We truncate explicitly so the
# behaviour matches every hash already in the database -- changing it would
# invalidate the stored hash of any user whose password exceeds 72 bytes.
BCRYPT_MAX_BYTES = 72

# A bcrypt modular-crypt string is exactly 60 chars: $2<variant>$<cost>$ plus a
# 53-char radix-64 salt+digest. This is validated BEFORE the value reaches
# bcrypt.checkpw, which does not merely reject a malformed hash -- it panics in
# its Rust extension (`PanicException: range end index N out of range`). That
# panic derives from BaseException, so it passes straight through `except
# Exception` and would take down the login path on a single corrupt row.
_BCRYPT_HASH_RE = re.compile(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$")


def _bcrypt_bytes(password: str) -> bytes:
    """Encode a password the way bcrypt will actually consume it.

    Truncation is applied to the ENCODED bytes rather than the string, because
    that is what bcrypt itself truncates -- slicing the str first would produce
    a different prefix for any non-ASCII password.
    """
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def verify_password(plain_password, hashed_password):
    if not hashed_password:
        return None
    if not isinstance(hashed_password, str) or not _BCRYPT_HASH_RE.match(hashed_password):
        log.warning("password verification failed: stored hash is not valid bcrypt")
        return False
    try:
        return bcrypt.checkpw(
            _bcrypt_bytes(plain_password), hashed_password.encode("utf-8")
        )
    except (ValueError, TypeError):
        # Malformed or non-bcrypt hash in the database. passlib raised
        # UnknownHashError here and every caller treats a falsey result as
        # "wrong password", so a corrupt stored hash must not become a 500 on
        # the login path.
        log.warning("password verification failed: stored hash is not valid bcrypt")
        return False


def get_password_hash(password):
    return bcrypt.hashpw(_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def create_token(data: dict, expires_delta: Union[timedelta, None] = None) -> str:
    payload = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
        payload.update({"exp": expire})

    encoded_jwt = jwt.encode(payload, SESSION_SECRET, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    try:
        decoded = jwt.decode(token, SESSION_SECRET, algorithms=[ALGORITHM])
        return decoded
    except Exception:
        return None


def extract_token_from_auth_header(auth_header: str):
    return auth_header[len("Bearer ") :]


def create_api_key():
    key = str(uuid.uuid4()).replace("-", "")
    return f"sk-{key}"


def get_http_authorization_cred(auth_header: str):
    try:
        scheme, credentials = auth_header.split(" ")
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)
    except Exception:
        raise ValueError(ERROR_MESSAGES.INVALID_TOKEN)


def _path_is_allowed(path: str, allowed_paths: list) -> bool:
    """True if `path` is covered by an API_KEY_ALLOWED_ENDPOINTS entry.

    An entry ending in ``/*`` is a prefix match (``/mcp/*`` covers
    ``/mcp/glab``, ``/mcp/echo``, ...); everything else is an exact match,
    unchanged from before.

    Needed because some routes carry a variable path segment -- the MCP
    proxy's backend name is part of the URL (``/mcp/{server}``) -- so an
    exact-match-only allowlist can never admit it no matter what an operator
    writes. Found auditing selfai/gitlab-profile#30: ENABLE_API_KEY_ENDPOINT_
    RESTRICTIONS defaults off, so this was dormant, but main.py's own comment
    told an operator to add "/mcp" to the allowlist, which would not have
    worked -- "/mcp/glab" != "/mcp" under the old exact-match check.
    """
    for entry in allowed_paths:
        if entry.endswith("/*"):
            if path.startswith(entry[:-1]):
                return True
        elif path == entry:
            return True
    return False


def get_current_user(
    request: Request,
    auth_token: HTTPAuthorizationCredentials = Depends(bearer_security),
):
    token = None

    if auth_token is not None:
        token = auth_token.credentials

    if token is None and "token" in request.cookies:
        token = request.cookies.get("token")

    if token is None:
        # self.ai#112: Anthropic-native clients (the SDKs, claude-code, crew-code)
        # send their credential as `x-api-key`, not `Authorization: Bearer`. Accepting
        # it here rather than in the /v1/messages route keeps one auth path -- the
        # api-key branch below still applies, so the endpoint restrictions and the
        # ENABLE_API_KEY switch govern it exactly as they do a bearer key.
        token = request.headers.get("x-api-key")

    if token is None:
        raise HTTPException(status_code=403, detail="Not authenticated")

    # auth by JIT eval token
    if token.startswith("eval-"):
        eval_info = get_eval_token_info(token)
        if eval_info is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Eval token expired or invalid",
            )
        user = Users.get_user_by_id(eval_info["user_id"])
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.INVALID_TOKEN,
            )
        # Attach eval metadata to request state for downstream handlers
        request.state.eval_job_id = eval_info["job_id"]
        request.state.eval_type = eval_info["eval_type"]
        return user

    # auth by api key
    if token.startswith("sk-"):
        if not request.state.enable_api_key:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED)

        if request.app.state.config.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS:
            allowed_paths = [
                path.strip() for path in str(request.app.state.config.API_KEY_ALLOWED_ENDPOINTS).split(",")
            ]

            if not _path_is_allowed(request.url.path, allowed_paths):
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED)

        return get_current_user_by_api_key(token)

    # auth by jwt token
    try:
        data = decode_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if data is not None and "id" in data:
        user = Users.get_user_by_id(data["id"])
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.INVALID_TOKEN,
            )
        else:
            Users.update_user_last_active_by_id(user.id)
        return user
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )


def get_current_user_by_api_key(api_key: str):
    user = Users.get_user_by_api_key(api_key)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.INVALID_TOKEN,
        )
    else:
        Users.update_user_last_active_by_id(user.id)

    return user


def get_verified_user(user=Depends(get_current_user)):
    if user.role not in {"user", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user


def get_admin_user(user=Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user
