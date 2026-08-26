"""PyInstaller entrypoint for the self-contained desktop Kernel service."""

from aceval.desktop_service import main


if __name__ == "__main__":
    raise SystemExit(main())
