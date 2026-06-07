# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Set up a virtual environment (do this once)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the app (must be root — preserve the venv's Python)
sudo .venv/bin/python ./run.py                        # default: 0.0.0.0:8000
sudo .venv/bin/python ./run.py --host 127.0.0.1 --port 8080
sudo .venv/bin/python ./run.py --reload               # dev mode with auto-reload

# Bootstrap / remove test users
sudo .venv/bin/python ./run.py --create-users
sudo .venv/bin/python ./run.py --remove-users
```

> Use `.venv/bin/python` with `sudo` rather than `sudo python` so the venv packages are used instead of the system Python.

## What this builds

A FastAPI web app that lets project investigators (and their designees) self-service the creation and membership management of project folders, without giving them root. The app itself **runs as root on a Linux box** — the FastAPI/uvicorn process runs as root and calls `useradd`/`groupadd`/`mkdir`/`chown`/`chmod` directly on the users' behalf (no sudo helper, no separate daemon).

## Authentication

There is **no password**. Auth is a single username text field — the app trusts whatever username is entered. Authorization (which projects a user can see/manage) is derived entirely from that username's group membership (see below).

## Who can manage a project

A project may be managed by **all members of `grp-<name>`** — *unless* a `grp-<name>-adm` subgroup exists. The existence of an `-adm` subgroup signals the project is sensitive, and management is then restricted to members of `grp-<name>-adm`. There is no separate "managed by" data store; this is computed from group membership alone (`Project.managers` in `app/projects.py`).

**Data stewards / project admins** are exactly the members of `grp-<name>-adm`, edited via `set_stewards()` (`app/system.py`) / `POST /projects/<name>/stewards`. Listing anyone here is what creates the adm group, so regular members immediately lose management rights; clearing the field deletes the adm group and management reverts to all members. There is no dedicated `adm` *folder* unless a manager also creates one as a restricted subfolder.

**Two reversible holding states (`app/system.py`), both root-only `0700` dirs that keep groups intact and disappear from `list_projects()`:**
- **Delete** → `projects/.deleted/<name>` via `delete_project()`, stamping a `.deleted_at` marker. Restorable with `undelete_project()`. `purge_expired()` permanently `rmtree`s entries older than `RETENTION_DAYS` (90) and drops their groups; it runs on startup in `run.py` and via `--purge-expired` (for cron).
- **Lock** → `projects/.locked/<name>` via `lock_project()` / `unlock_project()`. No retention — kept until unlocked.

`get_project()` is location-aware: it finds a project in active/`.deleted`/`.locked` and sets `Project.state` (`active`|`deleted`|`locked`) plus `days_left` for deleted ones. The detail page shows Delete+Lock when active, Undelete when deleted, Unlock when locked; editing forms are gated on `state == 'active'`. The dashboard lists held projects the user belongs to (`held_projects_for()`) with restore buttons. Restore raises if an active project of the same name exists.

**Subfolders get the same delete/lock treatment**, in holding dirs *inside the project root* (`<project>/.deleted/<area>`, `<project>/.locked/<area>`). The generic `_hold()`/`_unhold(parent, item, subdir)` helpers back both levels; `delete_subfolder`/`undelete_subfolder`/`lock_subfolder`/`unlock_subfolder` pass `parent = PROJECTS_BASE/<project>`. `Subfolder` carries `state`/`days_left`; `get_project` populates `subfolders` (active) and `held_subfolders` (deleted/locked) — the detail page shows Lock+Delete per active subfolder and a "Removed Subfolders" table with Undelete/Unlock. `purge_expired()` sweeps both top-level deleted projects and each active project's deleted subfolders. `_subfolders_for` skips dot-dirs so the holding areas never show as subfolders.

## The permission model (the core domain logic)

This is the part that requires care — it is the whole point of the app. **Access** is gated purely by **standard UNIX groups + SetGID bits** (no access ACLs), so Samba Access-Based Enumeration (ABE) can still hide folders a user can't read. Get these modes exactly right:

The project root is `PROJECTS_BASE` (`app/system.py`), which defaults to `./projects` resolved relative to the launch directory, overridable via the `PROJECTS_BASE` env var. Paths below are written as `/projects/...` for the canonical Samba deployment, but the app uses the resolved base.

- **Project root** `/projects/<name>`: `chown root:grp-<name>`, `chmod 2750`. Group members can traverse and see contents but not write at the root.
- **Shared folder** `/projects/<name>/shr`: `chown root:grp-<name>`, `chmod 2770`. Full collaborative read/write for the project group. Created on day one.
- **Restricted sibling folders** (e.g. `adm`, `mkt`, `samples`) added later: `chown root:grp-<name>-<sibling>`, `chmod 2770`. Each gets its own sub-group; users not in that sub-group cannot even see the folder (ABE).
- The `2` prefix (SetGID) on every folder is mandatory — it makes new files/dirs inherit the group.
- **Never modify the root or `/shr` when adding siblings.** Siblings are deployed side-by-side, leaving existing folders untouched.

### Project id & group naming

Every project gets a short internal id `xx-xx` (each char `[a-z0-9]`, e.g. `a3-f1`), generated uniquely at creation (`generate_project_id`). The on-disk folder is **`<name>_<id>`** (the display name never contains `_`, so `split_project_name()` recovers both). **Groups are keyed on the id, not the name**, so they stay short and always fit the platform limit: primary `grp-<id>`, stewards `grp-<id>-adm`, restricted subfolders `grp-<id>-<area>` — all built via `project_group()` / `subgroup()` in `app/system.py`. The id is stored in `.project.json` (`project_id`) and, in the UI, appears only as part of the project name (the folder name). Legacy projects created before ids (no `_<id>` suffix) fall back to `grp-<name>`.

`subgroup()` guarantees the result is ≤ `MAX_GROUP_NAME` (32 on Linux, larger on LDAP — env-overridable): if `grp-<id>-<area>` is too long it truncates and appends a short deterministic hash of the full name, so distinct areas never collapse to the same group, and it's reconstructable from the folder + area (the folder keeps the full readable area name).

### Default ACLs enforce group read/write (the one ACL use)

SetGID only makes new files inherit the *group*; their permission *bits* still come from the creator's `umask`, so a restrictive umask creates files the group can't read/write. To enforce collaboration we set a **default (inheritable) POSIX ACL** on every group-writable folder (`shr/` and restricted siblings) via `_set_inherit_acl()` in `app/system.py`: `setfacl -d -m u::rwx,g::rwx,o::-`. New files/dirs then get owner+group rwx regardless of umask. The `chmod 2770` stays as the human-readable advertisement `ls -l` shows; the default ACL does the enforcing. `g::` targets each folder's *own* owning group, so nothing leaks across folders.

Deliberately **no ACL on the gatekeeper root** (`2750`): it has no user-created content, and a default ACL there would be inherited by restricted siblings and leak primary-group access. Folders with a default ACL show a `+` in `ls -l` (and the middle triad reflects the ACL mask); `getfacl` is the source of truth. Requires `setfacl`/`getfacl` (the `acl` package) and a filesystem mounted with ACL support.

## Project metadata

Each project stores extra fields in a `.project.json` file at its root, written `chown root:root` / `chmod 0600` so **only root can read it** — it is not exposed via the project group. The free-text fields (`pi_lead`, `department`, `description`, `cost_id`) are listed in `METADATA_FIELDS`; there is also a boolean `public` flag handled separately in `read_metadata`/`write_metadata` (`app/system.py`). All are surfaced on the `Project` dataclass and editable by managers via `POST /projects/<name>/metadata`. Reads tolerate a missing or malformed file by returning defaults (empty strings, `public=False`).

## Visibility

A project is visible to a user only if they are a member, **or** the project is `public`. This is `Project.is_visible_to()`; the dashboard lists `projects_visible_to(username)` and the detail route returns **404** (not 403) for non-visible projects so their existence stays hidden. Public projects are visible (read-only) to everyone but still only manageable by their managers/stewards. Marking a project public is a checkbox in the create form and the Project Details editor.

## Project naming rules (validate on creation)

- Length: **> 10 and < 50 characters**.
- Lowercase the name; replace spaces with `-`.
- Allowed characters only: letters, numbers, hyphens. **No dots, no other special characters.**

## Membership & web UX

- The project list a user sees is derived from group membership; show the matrix of who manages each project (per the rule above) and who has access.
- Membership editing is a plain comma-separated text field of usernames. Removing a username from the field removes that user from the project group.
- Subprojects (restricted siblings) use the same comma-separated-usernames editing model.
- The README explicitly wants the UI to "look stunningly beautiful."

## Test users & bootstrap flags

The app must support these test users, created on demand if they don't exist:
`apple, banana, strawberry, orange, blueberry, mango, watermelon, pineapple, grape, peach`

- `--create-users`: bootstrap (create) the test users.
- `--remove-users`: delete them and remove them from groups.

## Reference scripts

The README's Phase 4 contains canonical bash provisioning logic (`create_project.sh`, `add_sibling.sh`) and the Samba `[Projects]` share config. Mirror that exact behavior in the app's provisioning code.
