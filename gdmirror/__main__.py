"""Entry point. Everything happens inside the TUI; the flags only preseed it."""

from __future__ import annotations

import argparse

from .settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gdmirror",
        description="Interactive mirror for a shared Google Drive folder.",
    )
    parser.add_argument("--folder-id", help="override the saved Drive folder id")
    parser.add_argument("--dest", help="override the saved destination directory")
    parser.add_argument("--workers", type=int, help="parallel transfers (1-16)")
    parser.add_argument(
        "--no-splash", action="store_true", help="skip the intro animation"
    )
    args = parser.parse_args(argv)

    cfg = Settings.load()
    if args.folder_id:
        cfg.folder_id = args.folder_id
    if args.dest:
        cfg.dest = args.dest
    if args.workers:
        cfg.workers = max(1, min(16, args.workers))

    from .ui import MirrorApp

    MirrorApp(cfg=cfg, splash=not args.no_splash).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
