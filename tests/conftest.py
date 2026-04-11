"""pytest 설정 — 프로젝트 루트를 sys.path에 추가."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
