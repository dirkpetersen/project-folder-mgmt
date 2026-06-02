"""
Read project state from the filesystem + group database.
No writes happen here — writes go through system.py.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.system import GROUP_PREFIX, PROJECTS_BASE, get_group_members, group_exists

_NAME_RE = re.compile(r"^[a-z0-9-]{11,49}$")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_project_name(raw: str) -> str:
    """Normalise and validate. Returns the clean name or raises ValueError."""
    name = raw.strip().lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9-]", "", name)
    if len(name) <= 10:
        raise ValueError("Project name must be longer than 10 characters.")
    if len(name) >= 50:
        raise ValueError("Project name must be shorter than 50 characters.")
    if not _NAME_RE.match(name):
        raise ValueError("Only letters, numbers, and hyphens are allowed.")
    return name


def validate_subfolder_name(raw: str) -> str:
    name = raw.strip().lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9-]", "", name)
    if not name:
        raise ValueError("Subfolder name cannot be empty.")
    if name == "shr":
        raise ValueError("'shr' is reserved.")
    return name


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class Subfolder:
    name: str
    group: str
    members: list[str]


@dataclass
class Project:
    name: str
    primary_group: str
    members: list[str]          # members of primary group
    adm_group: str | None       # grp-<name>-adm if it exists
    adm_members: list[str]      # members of adm group (or [])
    subfolders: list[Subfolder] = field(default_factory=list)

    @property
    def managers(self) -> list[str]:
        """Users allowed to manage this project."""
        if self.adm_group:
            return self.adm_members
        return self.members

    def is_manager(self, username: str) -> bool:
        return username in self.managers


# ---------------------------------------------------------------------------
# read from system
# ---------------------------------------------------------------------------

def _subfolders_for(project_name: str) -> list[Subfolder]:
    """Discover restricted sibling folders from the filesystem.

    Each is a directory under /projects/<name> other than 'shr', backed by a
    grp-<name>-<folder> group. 'adm' is excluded — it is the management group,
    surfaced separately on the project.
    """
    project_dir = PROJECTS_BASE / project_name
    if not project_dir.is_dir():
        return []
    subs = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in ("shr", "adm"):
            continue
        sub_group = f"{GROUP_PREFIX}{project_name}-{entry.name}"
        subs.append(Subfolder(
            name=entry.name,
            group=sub_group,
            members=get_group_members(sub_group),
        ))
    return subs


def get_project(project_name: str) -> Project | None:
    primary_group = f"{GROUP_PREFIX}{project_name}"
    if not group_exists(primary_group):
        return None
    members = get_group_members(primary_group)
    adm_group_name = f"{GROUP_PREFIX}{project_name}-adm"
    adm_group = adm_group_name if group_exists(adm_group_name) else None
    adm_members = get_group_members(adm_group_name) if adm_group else []
    return Project(
        name=project_name,
        primary_group=primary_group,
        members=members,
        adm_group=adm_group,
        adm_members=adm_members,
        subfolders=_subfolders_for(project_name),
    )


def list_projects() -> list[Project]:
    """Return all projects, discovered from the /projects directory.

    The filesystem is the source of truth: a project exists when
    /projects/<name> is a directory backed by a grp-<name> group. We cannot
    reliably parse project names out of group names because project names may
    themselves contain hyphens (e.g. grp-my-research-project).
    """
    if not PROJECTS_BASE.is_dir():
        return []
    projects = []
    for entry in sorted(PROJECTS_BASE.iterdir()):
        if not entry.is_dir():
            continue
        p = get_project(entry.name)
        if p:
            projects.append(p)
    return projects


def projects_for_user(username: str) -> list[Project]:
    """Return projects where the user is a member of the primary group."""
    return [p for p in list_projects() if username in p.members]
