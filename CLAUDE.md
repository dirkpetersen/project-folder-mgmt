# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app (must be root)
sudo python run.py                        # default: 0.0.0.0:8000
sudo python run.py --host 127.0.0.1 --port 8080
sudo python run.py --reload               # dev mode with auto-reload

# Bootstrap / remove test users
sudo python run.py --create-users
sudo python run.py --remove-users
```

## What this builds

A FastAPI web app that lets project investigators (and their designees) self-service the creation and membership management of project folders, without giving them root. The app itself **runs as root on a Linux box** — the FastAPI/uvicorn process runs as root and calls `useradd`/`groupadd`/`mkdir`/`chown`/`chmod` directly on the users' behalf (no sudo helper, no separate daemon).

## Authentication

There is **no password**. Auth is a single username text field — the app trusts whatever username is entered. Authorization (which projects a user can see/manage) is derived entirely from that username's group membership (see below).

## Who can manage a project

A project may be managed by **all members of `grp-<name>`** — *unless* a `grp-<name>-adm` subgroup exists. The existence of an `-adm` subgroup signals the project is sensitive, and management is then restricted to members of `grp-<name>-adm`. There is no separate "managed by" data store; this is computed from group membership alone.

## The permission model (the core domain logic)

This is the part that requires care — it is the whole point of the app. Folders are gated purely by **standard UNIX groups + SetGID bits**, deliberately *no ACLs*, so Samba Access-Based Enumeration (ABE) can hide folders a user can't read. Get these modes exactly right:

- **Project root** `/projects/<name>`: `chown root:grp-<name>`, `chmod 2750`. Group members can traverse and see contents but not write at the root.
- **Shared folder** `/projects/<name>/shr`: `chown root:grp-<name>`, `chmod 2770`. Full collaborative read/write for the project group. Created on day one.
- **Restricted sibling folders** (e.g. `adm`, `mkt`, `samples`) added later: `chown root:grp-<name>-<sibling>`, `chmod 2770`. Each gets its own sub-group; users not in that sub-group cannot even see the folder (ABE).
- The `2` prefix (SetGID) on every folder is mandatory — it makes new files/dirs inherit the group.
- **Never modify the root or `/shr` when adding siblings.** Siblings are deployed side-by-side, leaving existing folders untouched.

Group naming convention: primary group `grp-<project>`; restricted sub-groups `grp-<project>-<area>`.

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
