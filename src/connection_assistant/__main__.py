"""Entry point. GUI is the primary experience; a small CLI aids testing/debugging.

    icebreaker-connect                 # launch the guided GUI
    icebreaker-connect --check         # print an environment readiness report
    icebreaker-connect --doctor        # same as --check, plus resolved tool paths
    icebreaker-connect --version

The CLI never captures, prints, or logs any token value; ``--check`` only reports
whether tools are present.
"""

from __future__ import annotations

import argparse
import sys

from connection_assistant import __version__


def _cmd_check(show_paths: bool) -> int:
    from connection_assistant.android.installer import toolchain_paths
    from connection_assistant.android.toolchain import detect_toolchain

    tc = detect_toolchain()
    print("Environment readiness:")
    for name, ok in tc.summary().items():
        print(f"  [{'ok ' if ok else 'MISS'}] {name}")
    if tc.missing:
        print("\nMissing components:", ", ".join(tc.missing))
    else:
        print("\nAll required components are present.")
    if show_paths:
        print("\nResolved tool paths:")
        for name, path in toolchain_paths(tc).items():
            print(f"  {name}: {path or '(not found)'}")
    return 0 if not tc.missing else 1


def _cmd_gui() -> int:
    try:
        from connection_assistant.gui import run_gui
    except ImportError as exc:  # PySide6 missing
        print(f"GUI unavailable ({exc}). Install the GUI extras or use --check.", file=sys.stderr)
        return 2
    return run_gui()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="icebreaker-connect",
        description="Icebreaker Connect — securely connect your Tinder account to Icebreaker.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--check", action="store_true", help="print an environment readiness report and exit")
    parser.add_argument("--doctor", action="store_true", help="like --check, plus resolved tool paths")
    args = parser.parse_args(argv)

    if args.doctor:
        return _cmd_check(show_paths=True)
    if args.check:
        return _cmd_check(show_paths=False)
    return _cmd_gui()


if __name__ == "__main__":
    raise SystemExit(main())
