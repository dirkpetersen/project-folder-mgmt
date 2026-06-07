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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root resolves to ./projects relative to the directory the app is
# launched from. Override with the PROJECTS_BASE environment variable.
PROJECTS_BASE = Path(os.environ.get("PROJECTS_BASE", "projects")).resolve()
GROUP_PREFIX = "grp-"

# Linux limits group (and user) names. shadow-utils rejects names longer than
# this, so every group we create — grp-<project> and grp-<project>-<area> —
# must fit. Overridable for systems configured with a different limit.
MAX_GROUP_NAME = int(os.environ.get("MAX_GROUP_NAME", "32"))

# Per-project metadata file, stored at the project root. Readable by root only.
METADATA_FILE = ".project.json"
METADATA_FIELDS = ("pi_lead", "department", "description", "cost_id")  # free-text fields

# Holding areas for projects taken out of the active listing. Both are root-only
# (0700) so members can't reach the files inside. Deleted projects are purged
# from disk after RETENTION_DAYS; locked projects stay until explicitly unlocked.
DELETED_DIR = ".deleted"
LOCKED_DIR = ".locked"
RETENTION_DAYS = 90
DELETED_AT_MARKER = ".deleted_at"   # ISO timestamp written when a project is deleted


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

def _set_inherit_acl(path: Path) -> None:
    """Enforce group read/write on a collaborative folder via a default ACL.

    setgid makes new files inherit the folder's *group*, but their permission
    *bits* still come from each user's umask — so a restrictive umask silently
    creates files the group can't read or write. A default (inheritable) ACL
    fixes this: new files and subdirectories get owner+group rwx regardless of
    umask. The chmod 2770 above stays as the human-readable advertisement that
    `ls -l` shows; this default ACL does the actual enforcing.

    `g::` targets the folder's owning group, so each folder enforces its own
    group (grp-<project> for shr/, grp-<project>-<area> for restricted siblings)
    without leaking access across folders.
    """
    _run(["setfacl", "-d", "-m", "u::rwx,g::rwx,o::-", str(path)])


def _provision_dir(path: Path, group_name: str, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    gid = grp.getgrnam(group_name).gr_gid
    os.chown(path, 0, gid)
    os.chmod(path, mode)
    # Collaborative (group-writable) folders get a default ACL so new files
    # inherit group rwx despite the creator's umask. The gatekeeper root (2750,
    # group r-x, no user-created content) is left as pure mode bits — and must
    # be, so restricted subfolders don't inherit primary-group access.
    if mode & 0o020:  # group-writable bit set
        _set_inherit_acl(path)


def read_metadata(project_dir: Path) -> dict:
    """Read .project.json from a project directory. Returns defaults if absent/invalid.

    Takes the directory (not just the name) so it works for active projects as
    well as ones held under .deleted/ or .locked/.
    """
    path = Path(project_dir) / METADATA_FILE
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        data = {}
    meta = {k: str(data.get(k, "")) for k in METADATA_FIELDS}
    meta["public"] = bool(data.get("public", False))
    return meta


def write_metadata(project_name: str, metadata: dict) -> None:
    """Write .project.json at the project root, owned by root and mode 0600."""
    path = PROJECTS_BASE / project_name / METADATA_FILE
    data = {k: str(metadata.get(k, "")).strip() for k in METADATA_FIELDS}
    data["public"] = bool(metadata.get("public", False))
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


def set_stewards(project_name: str, stewards: list[str]) -> None:
    """Set the data stewards (project admins) = members of grp-<name>-adm.

    If stewards is non-empty, only they may manage the project. If it is empty,
    the adm group is removed and management reverts to all project members.
    """
    adm_group = f"{GROUP_PREFIX}{project_name}-adm"
    # Only real accounts can be stewards. If none of the entered names exist,
    # don't leave an empty adm group behind (that would lock everyone out of
    # management); delete it so management reverts to all members.
    valid = [u for u in stewards if user_exists(u)]
    if valid:
        sync_group_members(adm_group, valid)
    else:
        delete_group(adm_group)


# ---------------------------------------------------------------------------
# delete / lock (reversible holding areas) + purge
# ---------------------------------------------------------------------------

def _holding(subdir: str) -> Path:
    """Return (creating if needed) a root-only 0700 holding directory."""
    d = PROJECTS_BASE / subdir
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _move_to_holding(project_name: str, subdir: str) -> Path | None:
    """Move an active project into a holding area; return its new path."""
    src = PROJECTS_BASE / project_name
    if not src.exists():
        return None
    target = _holding(subdir) / project_name
    suffix = 1
    while target.exists():  # don't clobber a prior entry of the same name
        target = _holding(subdir) / f"{project_name}.{suffix}"
        suffix += 1
    shutil.move(str(src), str(target))
    return target


def _move_from_holding(project_name: str, subdir: str) -> bool:
    """Move a held project back to the active area. Raises if a name clash exists."""
    src = PROJECTS_BASE / subdir / project_name
    if not src.is_dir():
        return False
    dest = PROJECTS_BASE / project_name
    if dest.exists():
        raise RuntimeError(
            f"Cannot restore '{project_name}': an active project with that name already exists."
        )
    shutil.move(str(src), str(dest))
    return True


def delete_project(project_name: str) -> None:
    """Soft-delete: move the project into projects/.deleted/<name> and stamp the
    deletion time. Files and groups are preserved; it can be undeleted until it
    is purged from disk after RETENTION_DAYS (see purge_expired)."""
    target = _move_to_holding(project_name, DELETED_DIR)
    if target is not None:
        (target / DELETED_AT_MARKER).write_text(datetime.now(timezone.utc).isoformat())


def undelete_project(project_name: str) -> None:
    """Restore a deleted project to the active area."""
    src = PROJECTS_BASE / DELETED_DIR / project_name
    marker = src / DELETED_AT_MARKER
    if marker.exists():
        marker.unlink()
    _move_from_holding(project_name, DELETED_DIR)


def lock_project(project_name: str) -> None:
    """Lock: move the project into projects/.locked/<name>. It disappears from
    listings and members can't reach its files (the holding dir is root-only),
    but it is kept indefinitely and can be unlocked."""
    _move_to_holding(project_name, LOCKED_DIR)


def unlock_project(project_name: str) -> None:
    """Restore a locked project to the active area."""
    _move_from_holding(project_name, LOCKED_DIR)


def deleted_at(project_name: str) -> datetime | None:
    """Return when a deleted project was deleted, or None if unknown."""
    marker = PROJECTS_BASE / DELETED_DIR / project_name / DELETED_AT_MARKER
    try:
        return datetime.fromisoformat(marker.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def purge_expired(retention_days: int = RETENTION_DAYS) -> list[str]:
    """Permanently remove deleted projects older than retention_days, and drop
    their groups. Returns the names purged. Entries without a valid timestamp
    are left alone (fail-safe)."""
    holding = PROJECTS_BASE / DELETED_DIR
    if not holding.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    purged = []
    for entry in holding.iterdir():
        if not entry.is_dir():
            continue
        ts = deleted_at(entry.name)
        if ts is None or ts >= cutoff:
            continue
        shutil.rmtree(entry)
        _delete_project_groups(entry.name)
        purged.append(entry.name)
    return purged


def _delete_project_groups(project_name: str) -> None:
    """Remove a project's primary group and all of its sub-groups."""
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
