"""Minimal operator CLI; deliberately has no trading or network side effects."""
import argparse
from .config import AppConfig


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrade", description="Autotrade-11 safe foundation")
    parser.add_argument("command", choices=["doctor", "status"])
    args = parser.parse_args()
    config = AppConfig()
    if args.command == "doctor":
        print("Configuration valid; execution mode=PAPER; live execution=LOCKED")
    elif args.command == "status":
        print(f"mode={config.mode} equity_idr={config.initial_equity_idr} live_enabled={config.live_enabled}")


if __name__ == "__main__":
    main()
