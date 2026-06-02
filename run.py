#!/usr/bin/env python3
"""
Entry point. Must run as root.

Usage:
  sudo python run.py [--create-users] [--remove-users] [--host 0.0.0.0] [--port 8000]
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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Enable auto-reload (development)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: This application must run as root (sudo python run.py)", file=sys.stderr)
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

    # Ensure /projects exists
    from pathlib import Path
    Path("/projects").mkdir(parents=True, exist_ok=True)

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
