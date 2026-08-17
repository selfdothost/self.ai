"""Dynamically-registered MCP backends for the front-door proxy (self.ai#25).

Phase 1 of the front door hard-coded a name → URL map read from the
``MCP_PROXY_BACKENDS`` env at import time. That is fine for two entries an
operator writes, and clumsy past that: the env REPLACES rather than merges, and
a change needs a pod restart. This table is the durable half — a registrar adds
a backend over the API and it serves immediately, with no manifest edit and no
restart.

Trust model, which is the whole design
--------------------------------------
Registration decides where ``/mcp/{name}`` points, and the proxy forwards the
caller's own backend credential (``X-Mcp-Authorization`` → ``Authorization``:
a real GitLab PAT for glab-mcp, an IPA password for mcp-mailbox) to whatever URL
is registered. **Whoever controls a name can therefore harvest the credentials of
everyone who uses it.** Three controls, in order of how much they carry:

1. **The URL must be an in-cluster Service in an allowlisted namespace**
   (``MCP_REGISTRY_ALLOWED_NAMESPACES``). This is the load-bearing one: it holds
   even if a registrar credential leaks, because a stolen key still cannot point
   a backend at an attacker-controlled host off-cluster. Credentials can only
   ever be forwarded somewhere the platform already governs.
2. **GitOps names are reserved and immutable.** Anything in
   ``MCP_PROXY_BACKENDS`` cannot be shadowed, updated or deleted through this
   table — a registration cannot capture ``echo`` or ``playwright``.
3. **A dynamic name is owned by its registrant** (``owner_id``, a real self.ai
   user id) and only they may update or delete it.

Control 3 is deliberately not built on a service ticket. self.ai#79 established
that ``require_service_ticket`` proves possession of one shared
``SERVICE_AUTH_SECRET`` and nothing else — ``iss`` is unpinned, so every mesh
service can act as every other. Gating registration on it would have meant any
one compromised mesh service could re-point ``glab`` and collect PATs. A self.ai
user id is a real identity, individually revocable by rotating one API key.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text

from selfai_ui.internal.db import Base, get_db

log = logging.getLogger(__name__)


####################
# McpBackend DB Schema
####################


class McpBackend(Base):
    __tablename__ = "mcp_backend"

    #: The path segment it serves at — `/mcp/{name}`. Primary key: registration
    #: is an upsert on it, guarded by the owner check.
    name = Column(Text, unique=True, primary_key=True)
    #: The backend's MCP endpoint, e.g. http://glab-mcp.crew-system.svc:8081/mcp.
    #: Validated against the namespace allowlist before it ever reaches here.
    url = Column(Text, nullable=False)
    #: The self.ai user who registered it. Only they may update or delete it.
    owner_id = Column(Text, nullable=False)
    #: Denormalised for the listing, so `GET /servers` can say who owns what
    #: without a join. Not authoritative — owner_id is.
    owner_name = Column(Text)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


####################
# Pydantic Models
####################


class McpBackendModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    url: str
    owner_id: str
    owner_name: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class McpBackendForm(BaseModel):
    name: str
    url: str


####################
# Validation
####################

#: Names are a URL path segment and a table key, so keep them boring. Lowercase
#: alphanumerics plus dash/underscore, 1-63 chars — the DNS-label shape everyone
#: already expects of a service name.
_NAME_MAX = 63


def validate_name(name: str) -> Optional[str]:
    """Return an error string, or None if the name is acceptable."""
    if not name or len(name) > _NAME_MAX:
        return f"name must be 1-{_NAME_MAX} characters"
    if not all(c.isalnum() or c in "-_" for c in name):
        return "name may contain only letters, digits, '-' and '_'"
    if not name[0].isalnum():
        return "name must start with a letter or digit"
    if name != name.lower():
        return "name must be lowercase"
    return None


def validate_url(url: str, allowed_namespaces: set) -> Optional[str]:
    """Return an error string, or None if the URL may be registered.

    The URL must be a plain-HTTP in-cluster Service address whose namespace is
    allowlisted: ``http://<service>.<namespace>.svc[.cluster.local][:port]/path``.

    This is the control that survives a leaked registrar credential, so it fails
    closed on anything it does not positively recognise. In particular an IP
    literal is refused even if it would route to an allowed namespace — the
    allowlist is checked against a name, and an address that carries no namespace
    cannot be checked at all.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "url is not parseable"

    if parsed.scheme != "http":
        # https is not refused because TLS is bad, but because in-cluster
        # Services here are plain HTTP and accepting https would invite an
        # external hostname past the namespace check below.
        return "url scheme must be http (in-cluster Service address)"

    host = parsed.hostname
    if not host:
        return "url has no host"

    # Strip the optional cluster suffix, then require the .svc form so the
    # namespace is present and checkable.
    host = host.removesuffix(".cluster.local")
    parts = host.split(".")
    if len(parts) != 3 or parts[2] != "svc":
        return "url host must be <service>.<namespace>.svc (optionally .cluster.local)"

    namespace = parts[1]
    if namespace not in allowed_namespaces:
        allowed = ", ".join(sorted(allowed_namespaces)) or "(none configured)"
        return f"namespace '{namespace}' is not registrable; allowed: {allowed}"

    if not parsed.path or parsed.path == "/":
        return "url must include the backend's MCP path, e.g. /mcp"

    return None


####################
# Table
####################


class McpBackendsTable:
    def get_all(self) -> list[McpBackendModel]:
        with get_db() as db:
            return [McpBackendModel.model_validate(row) for row in db.query(McpBackend).all()]

    def get_by_name(self, name: str) -> Optional[McpBackendModel]:
        with get_db() as db:
            row = db.query(McpBackend).filter_by(name=name).first()
            return McpBackendModel.model_validate(row) if row else None

    def as_map(self) -> dict:
        """The name → URL map the proxy resolves against."""
        with get_db() as db:
            return {row.name: row.url for row in db.query(McpBackend).all()}

    def upsert(self, form: McpBackendForm, owner_id: str, owner_name: str) -> McpBackendModel:
        """Create the backend, or update it if `owner_id` already owns it.

        The caller is responsible for having checked the reserved-name and
        ownership rules; this is the storage half only.
        """
        now = int(time.time())
        with get_db() as db:
            row = db.query(McpBackend).filter_by(name=form.name).first()
            if row:
                row.url = form.url
                row.owner_name = owner_name
                row.updated_at = now
            else:
                row = McpBackend(
                    name=form.name,
                    url=form.url,
                    owner_id=owner_id,
                    owner_name=owner_name,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            db.commit()
            db.refresh(row)
            log.info("MCP backend registered: %s -> %s (owner %s)", form.name, form.url, owner_id)
            return McpBackendModel.model_validate(row)

    def delete(self, name: str) -> bool:
        with get_db() as db:
            deleted = db.query(McpBackend).filter_by(name=name).delete()
            db.commit()
            return bool(deleted)


McpBackends = McpBackendsTable()
