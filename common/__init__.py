"""Общий код для boards/ и ru/: HTTP, состояние, текстовые фильтры, клиент JobSync MCP."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
JOBSYNC_LEDGER = os.path.join(STATE_DIR, "jobsync_ledger.json")  # один на всё: лимит JobSync общий
