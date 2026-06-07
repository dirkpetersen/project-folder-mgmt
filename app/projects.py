"""
Read project state from the filesystem + group database.
No writes happen here — writes go through system.py.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.system import (
    DELETED_DIR,
    GROUP_PREFIX,
    LOCKED_DIR,
    PROJECTS_BASE,
    RETENTION_DAYS,
    deleted_at,
    get_group_members,
    group_exists,
    read_metadata,
)

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _normalize(raw: str) -> str:
    """Lowercase, spaces to hyphens, strip everything but [a-z0-9-]."""
    return re.sub(r"[^a-z0-9-]", "", raw.strip().lower().replace(" ", "-"))


def validate_project_name(raw: str) -> str:
    """Normalise and validate. Returns the clean name or raises ValueError."""
    name = _normalize(raw)
    if len(name) <= 10:
        raise ValueError("Project name must be longer than 10 characters.")
    if len(name) >= 50:
        raise ValueError("Project name must be shorter than 50 characters.")
    return name


def validate_subfolder_name(raw: str) -> str:
    name = _normalize(raw)
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
    pi_lead: str = ""           # from .project.json
    department: str = ""        # from .project.json
    description: str = ""       # from .project.json
    cost_id: str = ""           # from .project.json
    public: bool = False        # from .project.json; visible to everyone if true
    state: str = "active"       # "active" | "deleted" | "locked"
    days_left: int | None = None  # days until purge (deleted projects only)
    subfolders: list[Subfolder] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Absolute filesystem path of the project's current location."""
        if self.state == "deleted":
            return str(PROJECTS_BASE / DELETED_DIR / self.name)
        if self.state == "locked":
            return str(PROJECTS_BASE / LOCKED_DIR / self.name)
        return str(PROJECTS_BASE / self.name)

    @property
    def managers(self) -> list[str]:
        """Users allowed to manage this project."""
        if self.adm_group:
            return self.adm_members
        return self.members

    def is_manager(self, username: str) -> bool:
        return username in self.managers

    def is_visible_to(self, username: str) -> bool:
        """A project is visible to its members, or to anyone if public."""
        return self.public or username in self.members


# ---------------------------------------------------------------------------
# read from system
# ---------------------------------------------------------------------------

def _locate(project_name: str) -> tuple:
    """Find a project's directory and state. Active first, then the holding
    areas. Returns (Path, state) or (None, None) if it exists nowhere."""
    active = PROJECTS_BASE / project_name
    if active.is_dir():
        return active, "active"
    deleted = PROJECTS_BASE / DELETED_DIR / project_name
    if deleted.is_dir():
        return deleted, "deleted"
    locked = PROJECTS_BASE / LOCKED_DIR / project_name
    if locked.is_dir():
        return locked, "locked"
    return None, None


def _subfolders_for(project_name: str, project_dir) -> list[Subfolder]:
    """Discover restricted sibling folders inside a project directory.

    Each is a directory other than 'shr', backed by a grp-<name>-<folder>
    group. 'adm' is excluded — it is the management group, surfaced separately.
    """
    if not project_dir or not project_dir.is_dir():
        return []
    subs = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir() or entry.name in ("shr", "adm"):
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
    project_dir, state = _locate(project_name)
    if state is None:
        return None  # group exists but no folder anywhere
    members = get_group_members(primary_group)
    adm_group_name = f"{GROUP_PREFIX}{project_name}-adm"
    adm_group = adm_group_name if group_exists(adm_group_name) else None
    adm_members = get_group_members(adm_group_name) if adm_group else []
    meta = read_metadata(project_dir)

    days_left = None
    if state == "deleted":
        ts = deleted_at(project_name)
        if ts is not None:
            elapsed = (datetime.now(timezone.utc) - ts).days
            days_left = max(0, RETENTION_DAYS - elapsed)

    return Project(
        name=project_name,
        primary_group=primary_group,
        members=members,
        adm_group=adm_group,
        adm_members=adm_members,
        pi_lead=meta.get("pi_lead", ""),
        department=meta.get("department", ""),
        description=meta.get("description", ""),
        cost_id=meta.get("cost_id", ""),
        public=meta.get("public", False),
        state=state,
        days_left=days_left,
        subfolders=_subfolders_for(project_name, project_dir),
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
        if not entry.is_dir() or entry.name.startswith("."):
            continue  # skip dotfiles like the .deleted archive
        p = get_project(entry.name)
        if p:
            projects.append(p)
    return projects


def projects_for_user(username: str) -> list[Project]:
    """Return projects where the user is a member of the primary group."""
    return [p for p in list_projects() if username in p.members]


def projects_visible_to(username: str) -> list[Project]:
    """Return projects the user may see: their own, plus any public project."""
    return [p for p in list_projects() if p.is_visible_to(username)]


def _list_holding(subdir: str) -> list[Project]:
    """Build Project objects for everything in a holding area (.deleted/.locked)."""
    holding = PROJECTS_BASE / subdir
    if not holding.is_dir():
        return []
    projects = []
    for entry in sorted(holding.iterdir()):
        if not entry.is_dir():
            continue
        p = get_project(entry.name)
        if p:
            projects.append(p)
    return projects


def held_projects_for(username: str) -> list[Project]:
    """Deleted + locked projects the given user is a member of (for restore UI)."""
    held = _list_holding(DELETED_DIR) + _list_holding(LOCKED_DIR)
    return [p for p in held if username in p.members]
