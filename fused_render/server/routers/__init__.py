"""One APIRouter module per route group, wired together in server/app.py's
create_app(). A router file that also holds substantial reusable logic (fs
mutation helpers, the AI session) lives one level up in server/ instead —
this package is for route-wiring-only modules.
"""
