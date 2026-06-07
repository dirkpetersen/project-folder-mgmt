"""
FastAPI entry point. Runs as root.
"""
import re
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.projects import (
    get_project,
    held_projects_for,
    projects_visible_to,
    validate_project_name,
    validate_subfolder_name,
)
from app.system import (
    TEST_USERS,
    create_project,
    create_subfolder,
    delete_project,
    delete_subfolder,
    lock_project,
    set_stewards,
    subgroup,
    sync_group_members,
    undelete_project,
    unlock_project,
    user_exists,
    write_metadata,
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

USERNAME_COOKIE = "pfm_user"


def current_user(request: Request) -> str | None:
    return request.cookies.get(USERNAME_COOKIE)


def require_user(request: Request) -> str:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


def require_manager(request: Request, project_name: str):
    """Load the project and ensure the current user may manage it."""
    username = require_user(request)
    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not project.is_manager(username):
        raise HTTPException(status_code=403, detail="Not authorised.")
    return username, project


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    available = [u for u in TEST_USERS if user_exists(u)]
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "available_users": available,
    })


@app.post("/login")
async def do_login(
    request: Request,
    username: Annotated[str, Form()],
):
    username = username.strip().lower()
    if not username or not re.match(r"^[a-z0-9_-]+$", username):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid username."},
            status_code=400,
        )
    if not user_exists(username):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"User '{username}' does not exist on this system."},
            status_code=400,
        )
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(USERNAME_COOKIE, username, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(USERNAME_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    username = current_user(request)
    if not username:
        return RedirectResponse(url="/login", status_code=303)
    visible_projects = projects_visible_to(username)
    held = held_projects_for(username)
    return templates.TemplateResponse(request, "dashboard.html", {
        "username": username,
        "visible_projects": visible_projects,
        "deleted_projects": [p for p in held if p.state == "deleted"],
        "locked_projects": [p for p in held if p.state == "locked"],
    })


# ---------------------------------------------------------------------------
# create project
# ---------------------------------------------------------------------------

@app.get("/projects/new", response_class=HTMLResponse)
async def new_project_page(request: Request):
    username = require_user(request)
    return templates.TemplateResponse(request, "project_form.html", {
        "username": username,
        "error": None,
        "form": {},
    })


@app.post("/projects/new")
async def do_create_project(
    request: Request,
    name: Annotated[str, Form()],
    members: Annotated[str, Form()] = "",
    pi_lead: Annotated[str, Form()] = "",
    department: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    cost_id: Annotated[str, Form()] = "",
    public: Annotated[str, Form()] = "",
):
    username = require_user(request)
    is_public = bool(public)
    form = {
        "name": name, "members": members,
        "pi_lead": pi_lead, "department": department,
        "description": description, "cost_id": cost_id,
        "public": is_public,
    }
    try:
        clean_name = validate_project_name(name)
    except ValueError as e:
        return templates.TemplateResponse(request, "project_form.html", {
            "username": username,
            "error": str(e),
            "form": form,
        }, status_code=400)

    # Each project gets a unique internal id, so duplicate display names are
    # fine — the folder name (<name>_<id>) and groups (grp-<id>) stay distinct.
    member_list, unknown = _split_known_users(members)
    if username not in member_list:
        member_list.insert(0, username)
    folder_name = create_project(clean_name, member_list, {
        "pi_lead": pi_lead, "department": department,
        "description": description, "cost_id": cost_id,
        "public": is_public,
    })
    return _redirect_project(folder_name, _skipped_notice(unknown))


# ---------------------------------------------------------------------------
# project detail / edit
# ---------------------------------------------------------------------------

@app.get("/projects/{project_name}", response_class=HTMLResponse)
async def project_detail(request: Request, project_name: str, error: str = "", notice: str = ""):
    username = require_user(request)
    project = get_project(project_name)
    # Hide existence from users who can't see it: return 404 unless visible.
    if not project or not project.is_visible_to(username):
        raise HTTPException(status_code=404, detail="Project not found.")
    can_manage = project.is_manager(username)
    return templates.TemplateResponse(request, "project_detail.html", {
        "username": username,
        "project": project,
        "can_manage": can_manage,
        "error": error,
        "notice": notice,
    })


@app.post("/projects/{project_name}/members")
async def update_members(
    request: Request,
    project_name: str,
    members: Annotated[str, Form()] = "",
):
    username, project = require_manager(request, project_name)
    member_list, unknown = _split_known_users(members)
    # Don't let me remove my own access: if I'm currently a member, keep me in.
    if username in project.members and username not in member_list:
        member_list.append(username)
    sync_group_members(project.primary_group, member_list)
    return _redirect_project(project_name, _skipped_notice(unknown))


@app.post("/projects/{project_name}/stewards")
async def update_stewards(
    request: Request,
    project_name: str,
    stewards: Annotated[str, Form()] = "",
):
    username, _ = require_manager(request, project_name)
    steward_list, unknown = _split_known_users(stewards)
    # If I'm designating stewards, include myself so I keep management rights.
    if steward_list and username not in steward_list:
        steward_list.append(username)
    set_stewards(project_name, steward_list)
    return _redirect_project(project_name, _skipped_notice(unknown))


@app.post("/projects/{project_name}/metadata")
async def update_metadata(
    request: Request,
    project_name: str,
    pi_lead: Annotated[str, Form()] = "",
    department: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    cost_id: Annotated[str, Form()] = "",
    public: Annotated[str, Form()] = "",
):
    require_manager(request, project_name)
    write_metadata(project_name, {
        "pi_lead": pi_lead, "department": department,
        "description": description, "cost_id": cost_id,
        "public": bool(public),
    })
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


@app.post("/projects/{project_name}/delete")
async def do_delete_project(request: Request, project_name: str):
    require_manager(request, project_name)
    delete_project(project_name)
    return RedirectResponse(url="/", status_code=303)


@app.post("/projects/{project_name}/undelete")
async def do_undelete_project(request: Request, project_name: str):
    require_manager(request, project_name)
    undelete_project(project_name)
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


@app.post("/projects/{project_name}/lock")
async def do_lock_project(request: Request, project_name: str):
    require_manager(request, project_name)
    lock_project(project_name)
    return RedirectResponse(url="/", status_code=303)


@app.post("/projects/{project_name}/unlock")
async def do_unlock_project(request: Request, project_name: str):
    require_manager(request, project_name)
    unlock_project(project_name)
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


# ---------------------------------------------------------------------------
# subfolders
# ---------------------------------------------------------------------------

@app.post("/projects/{project_name}/subfolders/new")
async def do_create_subfolder(
    request: Request,
    project_name: str,
    folder_name: Annotated[str, Form()] = "",
    members: Annotated[str, Form()] = "",
):
    require_manager(request, project_name)
    if not folder_name.strip():
        # Blank field (e.g. clicked Add with nothing): just go back, no error.
        return RedirectResponse(url=f"/projects/{project_name}", status_code=303)
    try:
        clean_folder = validate_subfolder_name(folder_name, project_name)
    except ValueError as e:
        return RedirectResponse(
            url=f"/projects/{project_name}?error={quote(str(e))}", status_code=303)
    member_list, unknown = _split_known_users(members)
    create_subfolder(project_name, clean_folder, member_list)
    return _redirect_project(project_name, _skipped_notice(unknown))


@app.post("/projects/{project_name}/subfolders/{folder_name}/members")
async def update_subfolder_members(
    request: Request,
    project_name: str,
    folder_name: str,
    members: Annotated[str, Form()] = "",
):
    require_manager(request, project_name)
    sub_group = subgroup(project_name, folder_name)
    member_list, unknown = _split_known_users(members)
    sync_group_members(sub_group, member_list)
    return _redirect_project(project_name, _skipped_notice(unknown))


@app.post("/projects/{project_name}/subfolders/{folder_name}/delete")
async def do_delete_subfolder(
    request: Request,
    project_name: str,
    folder_name: str,
):
    require_manager(request, project_name)
    delete_subfolder(project_name, folder_name)
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------

def _parse_usernames(raw: str) -> list[str]:
    """Parse a user list. Space-separated is the norm, but comma- and
    semicolon-separated lists are also accepted. Delimiter precedence is
    semicolon > comma > whitespace: the highest-priority delimiter present in
    the input is the one used to split it. Surrounding whitespace is stripped,
    blank entries dropped, and names lowercased.
    """
    raw = raw.strip()
    if ";" in raw:
        parts = raw.split(";")
    elif "," in raw:
        parts = raw.split(",")
    else:
        parts = raw.split()  # any run of whitespace
    return [p.strip().lower() for p in parts if p.strip()]


def _split_known_users(raw: str) -> tuple:
    """Parse a user list into (known, unknown) by checking each against the
    system's account database. Used to warn about names that don't exist."""
    names = _parse_usernames(raw)
    known = [u for u in names if user_exists(u)]
    unknown = [u for u in names if not user_exists(u)]
    return known, unknown


def _skipped_notice(unknown: list) -> str:
    if not unknown:
        return ""
    plural = "s" if len(unknown) > 1 else ""
    return f"Skipped unknown user{plural} (no such account): {', '.join(unknown)}"


def _redirect_project(project_name: str, notice: str = "") -> RedirectResponse:
    url = f"/projects/{project_name}"
    if notice:
        url += f"?notice={quote(notice)}"
    return RedirectResponse(url=url, status_code=303)
