"""Nonempty discovery and zero-skip gate, including real SDK-backed tests."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    count = suite.countTestCases()
    if count < 37:
        raise SystemExit(f"Expected at least 37 tests; discovered {count}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit("Tests failed or skipped; SDK tests must actually execute")


if __name__ == "__main__":
    main()
