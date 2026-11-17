"""Validate and export a completed benchmark without changing measurements."""

import argparse
from pathlib import Path

from .dataset import publish


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--output", type=Path, required=True, help="new public data directory"
    )
    args = parser.parse_args()
    publish(args.directory, args.output)
    print(f"Published data: {args.output}")


if __name__ == "__main__":
    main()
