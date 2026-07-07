import logging
import threading
import uuid
import jwt

from datetime import UTC, datetime, timedelta
from typing import Optional, Union, List, Dict

from selfai_ui.models.users import Users

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import WEBUI_SECRET_KEY

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

logging.getLogger("passlib").setLevel(logging.ERROR)

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
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password, hashed_password):
    return (
        pwd_context.verify(plain_password, hashed_password) if hashed_password else None
    )


def get_password_hash(password):
    return pwd_context.hash(password)


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
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED
            )

        if request.app.state.config.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS:
            allowed_paths = [
                path.strip()
                for path in str(
                    request.app.state.config.API_KEY_ALLOWED_ENDPOINTS
                ).split(",")
            ]

            if request.url.path not in allowed_paths:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED
                )

        return get_current_user_by_api_key(token)

    # auth by jwt token
    try:
        data = decode_token(token)
    except Exception as e:
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
