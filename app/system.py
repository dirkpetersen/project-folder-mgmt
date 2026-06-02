"""
Privileged system operations — mkdir, chown, chmod, group and user management.
This module is called from a process that runs as root.
"""
import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

# Project root resolves to ./projects relative to the directory the app is
# launched from. Override with the PROJECTS_BASE environment variable.
PROJECTS_BASE = Path(os.environ.get("PROJECTS_BASE", "projects")).resolve()
GROUP_PREFIX = "grp-"

# Per-project metadata file, stored at the project root. Readable by root only.
METADATA_FILE = ".project.json"
METADATA_FIELDS = ("pi_lead", "description", "cost_id")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {result.stderr.strip()}")


def group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def get_group_members(group_name: str) -> list[str]:
    try:
        return list(grp.getgrnam(group_name).gr_mem)
    except KeyError:
        return []


# ---------------------------------------------------------------------------
# group management
# ---------------------------------------------------------------------------

def create_group(group_name: str) -> None:
    if not group_exists(group_name):
        _run(["groupadd", group_name])


def delete_group(group_name: str) -> None:
    if group_exists(group_name):
        _run(["groupdel", group_name])


def add_user_to_group(username: str, group_name: str) -> None:
    _run(["usermod", "-aG", group_name, username])


def remove_user_from_group(username: str, group_name: str) -> None:
    _run(["gpasswd", "-d", username, group_name])


def sync_group_members(group_name: str, desired: list[str]) -> None:
    """Set group membership to exactly the desired list."""
    create_group(group_name)
    current = set(get_group_members(group_name))
    wanted = set(desired)
    for u in wanted - current:
        if user_exists(u):
            add_user_to_group(u, group_name)
    for u in current - wanted:
        remove_user_from_group(u, group_name)


# ---------------------------------------------------------------------------
# project folder operations
# ---------------------------------------------------------------------------

def _provision_dir(path: Path, group_name: str, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    gid = grp.getgrnam(group_name).gr_gid
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def read_metadata(project_name: str) -> dict:
    """Read .project.json from the project root. Returns {} if absent/invalid."""
    path = PROJECTS_BASE / project_name / METADATA_FILE
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {k: str(data.get(k, "")) for k in METADATA_FIELDS}


def write_metadata(project_name: str, metadata: dict) -> None:
    """Write .project.json at the project root, owned by root and mode 0600."""
    path = PROJECTS_BASE / project_name / METADATA_FILE
    data = {k: str(metadata.get(k, "")).strip() for k in METADATA_FIELDS}
    path.write_text(json.dumps(data, indent=2))
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)


def create_project(project_name: str, members: list[str], metadata: dict | None = None) -> None:
    """Create the project root + /shr, the primary group, and set membership."""
    primary_group = f"{GROUP_PREFIX}{project_name}"
    sync_group_members(primary_group, members)  # creates the group if needed

    root_path = PROJECTS_BASE / project_name
    _provision_dir(root_path, primary_group, 0o2750)
    _provision_dir(root_path / "shr", primary_group, 0o2770)
    write_metadata(project_name, metadata or {})


def create_subfolder(project_name: str, folder_name: str, members: list[str]) -> None:
    """Create a restricted sibling folder and its dedicated sub-group."""
    sub_group = f"{GROUP_PREFIX}{project_name}-{folder_name}"
    sync_group_members(sub_group, members)  # creates the group if needed

    folder_path = PROJECTS_BASE / project_name / folder_name
    _provision_dir(folder_path, sub_group, 0o2770)


def delete_project(project_name: str) -> None:
    root_path = PROJECTS_BASE / project_name
    if root_path.exists():
        shutil.rmtree(root_path)
    # remove the primary group and all sub-groups
    for g in grp.getgrall():
        if g.gr_name == f"{GROUP_PREFIX}{project_name}" or \
           g.gr_name.startswith(f"{GROUP_PREFIX}{project_name}-"):
            delete_group(g.gr_name)


def delete_subfolder(project_name: str, folder_name: str) -> None:
    folder_path = PROJECTS_BASE / project_name / folder_name
    if folder_path.exists():
        shutil.rmtree(folder_path)
    delete_group(f"{GROUP_PREFIX}{project_name}-{folder_name}")


# ---------------------------------------------------------------------------
# bootstrap test users
# ---------------------------------------------------------------------------

TEST_USERS = [
    "apple", "banana", "strawberry", "orange", "blueberry",
    "mango", "watermelon", "pineapple", "grape", "peach",
]


def create_test_users() -> list[str]:
    created = []
    for u in TEST_USERS:
        if not user_exists(u):
            _run(["useradd", "-m", "-s", "/bin/bash", u])
            created.append(u)
    return created


def remove_test_users() -> list[str]:
    removed = []
    for u in TEST_USERS:
        if user_exists(u):
            _run(["userdel", "-r", u])
            removed.append(u)
    return removed
