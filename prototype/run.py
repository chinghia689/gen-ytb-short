from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from prototype.cli import main


if __name__ == "__main__":
    # Giữ lệnh fetch cũ tương thích với README trước đó.
    if sys.argv[1:] == ["--fetch-rss"]:
        sys.argv[1:] = ["fetch"]
    main()

