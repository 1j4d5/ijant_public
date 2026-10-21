from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to Python path so displayerv2 can be imported directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from displayerv2 import app

# Expose WSGI application for Vercel Serverless Function
