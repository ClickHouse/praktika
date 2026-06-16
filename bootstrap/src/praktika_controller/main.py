from __future__ import annotations

import argparse
import sys

from praktika_controller import controller


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="praktika-controller",
        description="Praktika controller bootstrap launcher.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    controller.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
