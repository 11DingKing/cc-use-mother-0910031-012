"""启动评分复核 HTTP 服务。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from score_review.api import main

if __name__ == "__main__":
    main()
