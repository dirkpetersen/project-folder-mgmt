#!/usr/bin/env python3
"""
Entry point. Must run as root.

Usage:
  sudo ./run.py [--create-users] [--remove-users] [--host 0.0.0.0] [--port 8000]

The project root defaults to ./projects (relative to the launch directory)
and can be overridden with the PROJECTS_BASE environment variable.
"""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="ProjectVault — project folder manager")
    parser.add_argument("--create-users", action="store_true",
                        help="Bootstrap test users and exit")
    parser.add_argument("--remove-users", action="store_true",
                        help="Remove test users and exit")
    parser.add_argument("--purge-expired", action="store_true",
                        help="Purge deleted projects past the 90-day retention and exit")
    parser.add_argument("--deactivate", type=int, metavar="DAYS",
                        help="Deactivate active projects with no file activity in DAYS "
                             "(move to .deactivated) and exit, e.g. --deactivate 90")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Enable auto-reload (development)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: This application must run as root (sudo ./run.py)", file=sys.stderr)
        sys.exit(1)

    from app.system import create_test_users, remove_test_users

    if args.create_users:
        created = create_test_users()
        print(f"Created {len(created)} test user(s): {', '.join(created) or 'none (all already existed)'}")
        sys.exit(0)

    if args.remove_users:
        removed = remove_test_users()
        print(f"Removed {len(removed)} test user(s): {', '.join(removed) or 'none found'}")
        sys.exit(0)

    if args.purge_expired:
        from app.system import purge_expired
        purged = purge_expired()
        print(f"Purged {len(purged)} expired deleted project(s): {', '.join(purged) or 'none'}")
        sys.exit(0)

    if args.deactivate is not None:
        from app.system import deactivate_inactive
        done = deactivate_inactive(args.deactivate)
        print(f"Deactivated {len(done)} project(s) inactive >{args.deactivate}d: "
              f"{', '.join(done) or 'none'}")
        sys.exit(0)

    # Ensure the project root exists (./projects by default)
    from app.system import PROJECTS_BASE, purge_expired
    PROJECTS_BASE.mkdir(parents=True, exist_ok=True)
    print(f"Project root: {PROJECTS_BASE}")

    # Permanently remove deleted projects past their 90-day retention.
    purged = purge_expired()
    if purged:
        print(f"Purged {len(purged)} expired deleted project(s): {', '.join(purged)}")

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
