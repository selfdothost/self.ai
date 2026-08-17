"""Admin database surface (self.ai#94).

Makes Admin > Settings > Database about the database. The page's previous
contents — config import/export and the two whole-database dumps — were backup
concerns and moved to the backup surface (#93).

Read-only on purpose. `ENABLE_PERSISTENT_CONFIG=False` means any value made
editable here without a matching manifest env line reverts silently on the next
pod restart, so this reports state and changes nothing.
"""

import logging

from fastapi import APIRouter, Depends

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.auth import get_admin_user
from selfai_ui.utils.database_info import database_info

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["DB"])

router = APIRouter()


@router.get("/info")
async def get_database_info(user=Depends(get_admin_user)):
    """Connection, pool, schema revision, size, and the SQLite version floor.

    Never returns a 503 for a partly-unavailable database: each section carries
    its own `error` and the rest still renders. A page that shows the schema
    revision but not the size is more useful than an error card.
    """
    return database_info()
