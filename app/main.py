"""
FastAPI entry point. Runs as root.
"""
import re
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.projects import (
    get_project,
    list_projects,
    projects_for_user,
    validate_project_name,
    validate_subfolder_name,
)
from app.system import (
    create_project,
    create_subfolder,
    delete_project,
    delete_subfolder,
    sync_group_members,
    user_exists,
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
    return templates.TemplateResponse(request, "login.html", {"error": error})


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
    my_projects = projects_for_user(username)
    all_projects = list_projects()
    return templates.TemplateResponse(request, "dashboard.html", {
        "username": username,
        "my_projects": my_projects,
        "all_projects": all_projects,
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
):
    username = require_user(request)
    try:
        clean_name = validate_project_name(name)
    except ValueError as e:
        return templates.TemplateResponse(request, "project_form.html", {
            "username": username,
            "error": str(e),
            "form": {"name": name, "members": members},
        }, status_code=400)

    if get_project(clean_name):
        return templates.TemplateResponse(request, "project_form.html", {
            "username": username,
            "error": f"Project '{clean_name}' already exists.",
            "form": {"name": name, "members": members},
        }, status_code=400)

    member_list = _parse_usernames(members)
    if username not in member_list:
        member_list.insert(0, username)
    create_project(clean_name, member_list)
    return RedirectResponse(url=f"/projects/{clean_name}", status_code=303)


# ---------------------------------------------------------------------------
# project detail / edit
# ---------------------------------------------------------------------------

@app.get("/projects/{project_name}", response_class=HTMLResponse)
async def project_detail(request: Request, project_name: str):
    username = require_user(request)
    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    can_manage = project.is_manager(username)
    return templates.TemplateResponse(request, "project_detail.html", {
        "username": username,
        "project": project,
        "can_manage": can_manage,
    })


@app.post("/projects/{project_name}/members")
async def update_members(
    request: Request,
    project_name: str,
    members: Annotated[str, Form()] = "",
):
    _, project = require_manager(request, project_name)
    sync_group_members(project.primary_group, _parse_usernames(members))
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


@app.post("/projects/{project_name}/delete")
async def do_delete_project(request: Request, project_name: str):
    require_manager(request, project_name)
    delete_project(project_name)
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# subfolders
# ---------------------------------------------------------------------------

@app.post("/projects/{project_name}/subfolders/new")
async def do_create_subfolder(
    request: Request,
    project_name: str,
    folder_name: Annotated[str, Form()],
    members: Annotated[str, Form()] = "",
):
    require_manager(request, project_name)
    try:
        clean_folder = validate_subfolder_name(folder_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    create_subfolder(project_name, clean_folder, _parse_usernames(members))
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


@app.post("/projects/{project_name}/subfolders/{folder_name}/members")
async def update_subfolder_members(
    request: Request,
    project_name: str,
    folder_name: str,
    members: Annotated[str, Form()] = "",
):
    require_manager(request, project_name)
    sub_group = f"grp-{project_name}-{folder_name}"
    sync_group_members(sub_group, _parse_usernames(members))
    return RedirectResponse(url=f"/projects/{project_name}", status_code=303)


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
    return [u.strip().lower() for u in re.split(r"[,\s]+", raw) if u.strip()]
