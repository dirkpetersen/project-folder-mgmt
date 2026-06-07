"""
Privileged system operations — mkdir, chown, chmod, group and user management.
This module is called from a process that runs as root.
"""
import grp
import hashlib
import json
import os
import pwd
import random
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root resolves to ./projects relative to the directory the app is
# launched from. Override with the PROJECTS_BASE environment variable.
PROJECTS_BASE = Path(os.environ.get("PROJECTS_BASE", "projects")).resolve()
GROUP_PREFIX = "grp-"

# Every project gets a short internal id of the form xx-xx (each x a lowercase
# letter or digit). The on-disk folder is "<name>_<id>"; UNIX groups are keyed
# on the id (grp-<id>, grp-<id>-adm, grp-<id>-<area>) so group names stay short
# and always fit MAX_GROUP_NAME regardless of how long the project name is.
PROJECT_ID_RE = re.compile(r"[a-z0-9]{2}-[a-z0-9]{2}")
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

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
# project id / group naming
# ---------------------------------------------------------------------------

def split_project_name(folder_name: str) -> tuple:
    """Split a folder name into (display_name, project_id).

    New projects are stored as "<name>_<id>"; project_id is None for legacy
    projects created before ids existed (display names never contain '_').
    """
    base, sep, tail = folder_name.rpartition("_")
    if sep and PROJECT_ID_RE.fullmatch(tail):
        return base, tail
    return folder_name, None


def project_group(folder_name: str) -> str:
    """Primary group for a project: grp-<id> (or grp-<folder> for legacy ones)."""
    _, pid = split_project_name(folder_name)
    return f"{GROUP_PREFIX}{pid}" if pid else f"{GROUP_PREFIX}{folder_name}"


def subgroup(folder_name: str, area: str) -> str:
    """Group name for a sub-area (adm, or a restricted subfolder), always within
    the platform's group-name limit (MAX_GROUP_NAME — 32 on Linux, larger on
    LDAP). grp-<id>-<area> is used verbatim when it fits; if it is too long the
    name is cut and a short deterministic hash of the full name is appended, so
    two distinct areas can never collapse to the same group. It is reconstructed
    deterministically from (folder, area), so the on-disk folder keeps the full
    readable area name while the group stays short."""
    name = f"{project_group(folder_name)}-{area}"
    if len(name) <= MAX_GROUP_NAME:
        return name
    h = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{name[:MAX_GROUP_NAME - len(h) - 1]}-{h}"


def generate_project_id() -> str:
    """Allocate a unique xx-xx id not already used by a group or folder."""
    existing = set()
    for sub in ("", DELETED_DIR, LOCKED_DIR):
        d = PROJECTS_BASE / sub if sub else PROJECTS_BASE
        if d.is_dir():
            for entry in d.iterdir():
                _, pid = split_project_name(entry.name)
                if pid:
                    existing.add(pid)
    for _ in range(10000):
        pid = (random.choice(_ID_ALPHABET) + random.choice(_ID_ALPHABET) + "-"
               + random.choice(_ID_ALPHABET) + random.choice(_ID_ALPHABET))
        if pid not in existing and not group_exists(f"{GROUP_PREFIX}{pid}"):
            return pid
    raise RuntimeError("Could not allocate a unique project id.")


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
    meta["project_id"] = str(data.get("project_id", ""))
    return meta


def write_metadata(project_name: str, metadata: dict) -> None:
    """Write .project.json at the project root, owned by root and mode 0600.

    The project_id is always (re)derived from the folder name so it is recorded
    in the file and never lost when other metadata is edited.
    """
    path = PROJECTS_BASE / project_name / METADATA_FILE
    data = {k: str(metadata.get(k, "")).strip() for k in METADATA_FIELDS}
    data["public"] = bool(metadata.get("public", False))
    _, pid = split_project_name(project_name)
    if pid:
        data["project_id"] = pid
    path.write_text(json.dumps(data, indent=2))
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)


def create_project(display_name: str, members: list[str], metadata: dict | None = None) -> str:
    """Create a project: allocate an id, make <name>_<id>/ + /shr, and the
    grp-<id> primary group. Returns the on-disk folder name (<name>_<id>)."""
    pid = generate_project_id()
    folder_name = f"{display_name}_{pid}"
    primary_group = f"{GROUP_PREFIX}{pid}"
    sync_group_members(primary_group, members)  # creates the group if needed

    root_path = PROJECTS_BASE / folder_name
    _provision_dir(root_path, primary_group, 0o2750)
    _provision_dir(root_path / "shr", primary_group, 0o2770)
    write_metadata(folder_name, metadata or {})
    return folder_name


def create_subfolder(project_name: str, folder_name: str, members: list[str]) -> None:
    """Create a restricted sibling folder and its dedicated sub-group."""
    sub_group = subgroup(project_name, folder_name)
    sync_group_members(sub_group, members)  # creates the group if needed

    folder_path = PROJECTS_BASE / project_name / folder_name
    _provision_dir(folder_path, sub_group, 0o2770)


def set_stewards(project_name: str, stewards: list[str]) -> None:
    """Set the data stewards (project admins) = members of the adm sub-group.

    If stewards is non-empty, only they may manage the project. If it is empty,
    the adm group is removed and management reverts to all project members.
    """
    adm_group = subgroup(project_name, "adm")
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

# Generic holding-area moves, used for both whole projects (parent=PROJECTS_BASE)
# and restricted subfolders (parent=PROJECTS_BASE/<project>). Each parent grows
# its own root-only .deleted/ and .locked/ dirs.

def _hold(parent: Path, item: str, subdir: str, stamp: bool = False) -> Path | None:
    """Move parent/item into parent/<subdir>/item; return its new path."""
    src = parent / item
    if not src.exists():
        return None
    holding = parent / subdir
    holding.mkdir(parents=True, exist_ok=True)
    os.chmod(holding, 0o700)  # root-only
    target = holding / item
    suffix = 1
    while target.exists():  # don't clobber a prior entry of the same name
        target = holding / f"{item}.{suffix}"
        suffix += 1
    shutil.move(str(src), str(target))
    if stamp:
        (target / DELETED_AT_MARKER).write_text(datetime.now(timezone.utc).isoformat())
    return target


def _unhold(parent: Path, item: str, subdir: str) -> bool:
    """Move parent/<subdir>/item back to parent/item. Raises on a name clash."""
    src = parent / subdir / item
    if not src.is_dir():
        return False
    dest = parent / item
    if dest.exists():
        raise RuntimeError(f"Cannot restore '{item}': a '{item}' already exists.")
    marker = src / DELETED_AT_MARKER
    if marker.exists():
        marker.unlink()
    shutil.move(str(src), str(dest))
    return True


def read_deleted_marker(path: Path) -> datetime | None:
    """Read the .deleted_at timestamp inside a held directory, or None."""
    try:
        return datetime.fromisoformat((Path(path) / DELETED_AT_MARKER).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


# --- whole projects -------------------------------------------------------

def delete_project(project_name: str) -> None:
    """Soft-delete: move the project into projects/.deleted/<name>, stamped with
    the deletion time. Files and groups are kept; undeletable until purged after
    RETENTION_DAYS."""
    _hold(PROJECTS_BASE, project_name, DELETED_DIR, stamp=True)


def undelete_project(project_name: str) -> None:
    _unhold(PROJECTS_BASE, project_name, DELETED_DIR)


def lock_project(project_name: str) -> None:
    """Lock: move the project into projects/.locked/<name> — hidden and
    inaccessible (root-only holding dir), kept until unlocked."""
    _hold(PROJECTS_BASE, project_name, LOCKED_DIR)


def unlock_project(project_name: str) -> None:
    _unhold(PROJECTS_BASE, project_name, LOCKED_DIR)


def deleted_at(project_name: str) -> datetime | None:
    """When a deleted project was deleted, or None."""
    return read_deleted_marker(PROJECTS_BASE / DELETED_DIR / project_name)


# --- restricted subfolders (same logic, inside the project root) ----------

def delete_subfolder(project_name: str, folder_name: str) -> None:
    """Soft-delete a subfolder into <project>/.deleted/<folder>. Group kept."""
    _hold(PROJECTS_BASE / project_name, folder_name, DELETED_DIR, stamp=True)


def undelete_subfolder(project_name: str, folder_name: str) -> None:
    _unhold(PROJECTS_BASE / project_name, folder_name, DELETED_DIR)


def lock_subfolder(project_name: str, folder_name: str) -> None:
    _hold(PROJECTS_BASE / project_name, folder_name, LOCKED_DIR)


def unlock_subfolder(project_name: str, folder_name: str) -> None:
    _unhold(PROJECTS_BASE / project_name, folder_name, LOCKED_DIR)


# --- purge ----------------------------------------------------------------

def purge_expired(retention_days: int = RETENTION_DAYS) -> list[str]:
    """Permanently remove deleted projects AND deleted subfolders older than
    retention_days, dropping their groups. Entries with no valid timestamp are
    left alone (fail-safe). Returns the names/paths purged."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    purged = []

    def _expired(path: Path) -> bool:
        ts = read_deleted_marker(path)
        return ts is not None and ts < cutoff

    # 1. expired deleted projects
    holding = PROJECTS_BASE / DELETED_DIR
    if holding.is_dir():
        for entry in holding.iterdir():
            if entry.is_dir() and _expired(entry):
                shutil.rmtree(entry)
                _delete_project_groups(entry.name)
                purged.append(entry.name)

    # 2. expired deleted subfolders inside each active project
    if PROJECTS_BASE.is_dir():
        for proj in PROJECTS_BASE.iterdir():
            if not proj.is_dir() or proj.name.startswith("."):
                continue
            sub_holding = proj / DELETED_DIR
            if not sub_holding.is_dir():
                continue
            for entry in sub_holding.iterdir():
                if entry.is_dir() and _expired(entry):
                    shutil.rmtree(entry)
                    delete_group(subgroup(proj.name, entry.name))
                    purged.append(f"{proj.name}/{entry.name}")
    return purged


def _delete_project_groups(project_name: str) -> None:
    """Remove a project's primary group and all of its sub-groups."""
    base = project_group(project_name)
    for g in grp.getgrall():
        if g.gr_name == base or g.gr_name.startswith(f"{base}-"):
            delete_group(g.gr_name)


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
