# project-folder-mgmt

**ProjectVault** — a FastAPI web app for self-service management of POSIX project
folders and their UNIX groups. Investigators and their designees create project
folders, manage members, and add restricted subfolders through a web UI, without
ever needing root themselves. The app runs as root and performs the privileged
`mkdir` / `chown` / `chmod` / group operations on their behalf.

Folder access is gated purely by **standard UNIX groups + SetGID bits** (no ACLs),
so it stays fast and works cleanly with Samba's Access-Based Enumeration (ABE).

---

## Quickstart

The app must run as **root** (it creates users, groups, and folders). Run these
from the project directory:

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Bootstrap the 10 test users (apple, banana, ...)
sudo ./run.py --create-users
# → Created 10 test user(s): apple, banana, strawberry, orange, blueberry,
#   mango, watermelon, pineapple, grape, peach

# 4. Start the app
sudo ./run.py --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080> and log in.

> **Note on `sudo` + venv:** if `sudo` resets your `PATH`, run the venv's
> interpreter explicitly so it uses the installed packages:
> `sudo .venv/bin/python run.py --host 127.0.0.1 --port 8080`

### Logging in

There is no password — just a username. Valid logins are any **existing Linux
account**, which after step 3 means the ten test users:

```
apple   banana   strawberry   orange   blueberry
mango   watermelon   pineapple   grape   peach
```

When you're done testing, remove the users and their groups:

```bash
sudo ./run.py --remove-users
```

---

## Commands

```bash
sudo ./run.py                              # serve on 0.0.0.0:8000 (default)
sudo ./run.py --host 127.0.0.1 --port 8080 # custom bind address
sudo ./run.py --reload                     # dev mode with auto-reload
sudo ./run.py --create-users               # bootstrap the test users and exit
sudo ./run.py --remove-users               # delete the test users and exit
```

### Configuration

| Setting         | Default      | Notes                                                       |
| --------------- | ------------ | ----------------------------------------------------------- |
| `PROJECTS_BASE` | `./projects` | Project root, resolved from the launch directory. See below. |

The project root defaults to `./projects` (created at startup) for easy local
testing. For a real Samba deployment, point it at the share path:

```bash
sudo PROJECTS_BASE=/projects ./run.py
```

---

## How it works

### What you can do in the UI

- **Dashboard** — an access matrix of the projects you can see: your own, plus
  any project marked public. Shows members, managers, and subfolders.
- **Create a project** — name must be **11–49 characters**, lowercase, with
  spaces turned into hyphens and only letters, numbers, and hyphens allowed
  (no dots or other special characters). You are automatically added as a member.
- **Project details** — PI / Lead, description, cost ID, and a **public** flag,
  stored in a `.project.json` file at the project root that is readable by root
  only (`chmod 0600`). Editable by managers.
- **Visibility** — by default a project is visible only to its members. Tick the
  **public** checkbox to make it visible (read-only) to everyone; it remains
  manageable only by its managers/stewards.
- **Manage members** — a comma-separated text field of usernames. Removing a name
  removes that user from the project.
- **Data stewards** — a comma-separated field of project admins. If anyone is
  listed here, **only those users can manage the project** and regular members
  lose management rights. Clear the field to let all members manage it again.
- **Restricted subfolders** — add siblings (e.g. `mkt`, `samples`), each
  with its own member list. Users not in a subfolder's group can't even see it.
- **Archive a project** — "deleting" a project is non-destructive: its files are
  moved to `projects/.deleted/<name>` (readable by root only) and it disappears
  from listings, so an administrator can restore it.

### Who can manage a project

A project may be managed by **all members of `grp-<name>`** — *unless* data
stewards have been set, which creates a `grp-<name>-adm` group. When that group
exists, management is restricted to its members. This is computed from group
membership alone; there is no separate data store.

### The permission model

| Path                         | Owner / group              | Mode   | Effect                                                       |
| ---------------------------- | -------------------------- | ------ | ------------------------------------------------------------ |
| `/projects/<name>`           | `root:grp-<name>`          | `2750` | Group can traverse and read; no write at the root.           |
| `/projects/<name>/shr`       | `root:grp-<name>`          | `2770` | Full collaborative read/write. Created on day one.           |
| `/projects/<name>/<sibling>` | `root:grp-<name>-<sibling>`| `2770` | Restricted; invisible (via ABE) to users outside its group.  |

The `2` prefix (SetGID) is mandatory on every folder so new files inherit the
group. Adding a sibling **never** touches the root or `shr` — siblings are
deployed side-by-side.

### Project layout

| File                  | Role                                                              |
| --------------------- | ---------------------------------------------------------------- |
| `run.py`              | Entry point: root check, `--create-users`/`--remove-users`, server. |
| `app/system.py`       | Privileged ops: `groupadd`, `useradd`, `chown`, `chmod`, etc.    |
| `app/projects.py`     | Read-only discovery of projects from the filesystem + name validation. |
| `app/main.py`         | FastAPI routes: login, dashboard, projects, subfolders.          |
| `app/templates/`      | Jinja2 templates.                                                |
| `app/static/css/`     | Styles.                                                          |

---

## Samba deployment blueprint

The web app automates the standard provisioning below. This section is the
reference architecture for serving `/projects` over Samba at scale (10 to 10,000
projects) using standard Linux groups and Access-Based Enumeration (ABE), without
relying on ACLs.

### Phase 1: Samba global layout (do this once)

Add this share block to `/etc/samba/smb.conf`:

```ini
[Projects]
    path = /projects
    writable = yes
    browsable = yes

    # Enable Access-Based Enumeration (hides folders users can't read)
    access-based share enum = yes

    # Enforce standard UNIX permission inheritance via SetGID
    inherit permissions = yes
    inherit owner = yes

    # Keep newly created network files/folders at clean 770/660
    directory mask = 0770
    create mask = 0660
```

Apply with `sudo systemctl reload smbd`.

### Phase 2: Day-one project (root + shared folder)

Create one master group and add all members to it (e.g. `grp-banana`), then:

```bash
mkdir -p /projects/banana/shr

# Project root: group can enter/read, others locked out
chown root:grp-banana /projects/banana
chmod 2750 /projects/banana

# Shared folder: group gets full rwx + SetGID inheritance
chown root:grp-banana /projects/banana/shr
chmod 2770 /projects/banana/shr
```

Result: everyone in `grp-banana` sees `/projects/banana` and can collaborate in
`/shr`.

### Phase 3: Growing complex (restricted siblings)

Later, a team needs a restricted `adm` folder and a `mkt` folder. **Do not touch
the root or `/shr`.** Create dedicated sub-groups and deploy siblings alongside:

```bash
mkdir -p /projects/banana/adm /projects/banana/mkt

chown root:grp-banana-adm /projects/banana/adm
chmod 2770 /projects/banana/adm

chown root:grp-banana-mkt /projects/banana/mkt
chmod 2770 /projects/banana/mkt
```

ABE evaluates the UNIX permissions on the fly:

- Standard users (only in `grp-banana`) see **only** `/shr`; `/adm` and `/mkt`
  are invisible.
- Managers (also in `grp-banana-adm`) additionally see `/adm`.
- Marketing users (also in `grp-banana-mkt`) additionally see `/mkt`.

### Why this scales

1. **Zero config bloat** — `smb.conf` stays tiny forever.
2. **Instant performance** — Linux group evaluation happens in the kernel, so
   browsing is fast even with tens of thousands of users.
3. **Short paths** — three-letter folders (`/shr`, `/adm`, `/mkt`) keep paths
   compact for Windows and Mac clients.
