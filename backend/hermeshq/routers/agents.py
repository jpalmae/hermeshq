"""Agents router – thin aggregator that includes all agent sub-routers.

The original monolithic module has been split into focused sub-modules:
  - agents_crud.py      → create, list, get, update, delete (archive)
  - agents_runtime.py   → start, stop, restart, mode changes
  - agents_avatar.py    → avatar get / upload / delete
  - agents_workspace.py → workspace file browsing and content
  - agents_bulk.py      → bulk task dispatch, bulk message send
  - agents_template.py  → create from template, system operator bootstrap
  - agents_managed.py   → managed integration test / actions

Shared constants and helper functions live in agents_shared.py.
"""

from hermeshq.routers.agents_crud import router as crud_router
from hermeshq.routers.agents_runtime import router as runtime_router
from hermeshq.routers.agents_avatar import router as avatar_router
from hermeshq.routers.agents_workspace import router as workspace_router
from hermeshq.routers.agents_bulk import router as bulk_router
from hermeshq.routers.agents_template import router as template_router
from hermeshq.routers.agents_managed import router as managed_router

from fastapi import APIRouter

router = APIRouter(prefix="/agents", tags=["agents"])

router.include_router(crud_router)
router.include_router(runtime_router)
router.include_router(avatar_router)
router.include_router(workspace_router)
router.include_router(bulk_router)
router.include_router(template_router)
router.include_router(managed_router)
