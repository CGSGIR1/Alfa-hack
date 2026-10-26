"""Single entry point for the integrated application."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('NEWS_ANALYTICS_HOME', str(ROOT))
sys.path.insert(0, str(ROOT / 'company-news-analytics'))

if __name__ == '__main__':
    from src.integration.cli import main
    raise SystemExit(main())
