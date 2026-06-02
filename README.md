# project-folder-mgmt

manage project folders in a posix file system with posix only groups

## Scope 

we want a fastapi applicaiton for users to create of project folders and sub folders that are managed with the right permisisons , the setup requires root access which we cannot give to the users so they go to a web app that does it on their behalf, in the simple form the web app will ceate the folder strucnture under /projects and then create unix groups on the local system, the test users will be called apple, banana, strawberry, orange, blueberry, mango, watermelon, pineapple, grape, peach and will be created if not exist (use option --create-users to bootstrap these usera and --remove-users to delete them and remove them from groups 

Web user experience: Can investigator or there designee should go to a website they should see the list of their current projects So matrix that they manage  For this read all the groups based on their read all the project groups based on their Manage by attribute where you can see who manages projects and who has access to mamaging them So we see the list of current proijects Then there's a button to create a new project the project name has to be longer than 10 characters and shorter than 50 characters Replace all spaces in the project names with - and the project name to lower case don't allow any special characters in the project names only letters numbers and hyphens no dots Grinder is a window that shows all the project members This is just a text field with user names and that is, separated Umm if you remove one of them they will be removed from the project  This project also has the ability to create a subroject and there it's the same thing Users a text field with, separated user names that should be members that you can edit and modify It's very simple I want this app to look stunningly beautiful ..... the web app wil run as root on a linux box 


### Below is the approach how to manage this manually 


Here is the comprehensive, end-to-end blueprint for building a scalable, automated project storage architecture. This plan utilizes standard Linux groups and Samba's Access-Based Enumeration (ABE) without relying on ACLs, ensuring it remains lightning-fast and easy to maintain from 10 projects to 10,000 projects.

---

## Phase 1: The Core Foundation (Do This Once)

This phase establishes the global settings on your Linux/Samba server. It creates a single, permanent configuration block that never needs to change, no matter how large your storage grows.

### 1. The Samba Global Layout (`/etc/samba/smb.conf`)

Open your Samba configuration file and add the following share block.

```ini
[Projects]
    path = /projects
    writable = yes
    browsable = yes
    
    # Enable Access-Based Enumeration (Hides folders users can't read)
    access-based share enum = yes
    
    # Enforce standard UNIX permission inheritance via SetGID
    inherit permissions = yes
    inherit owner = yes
    
    # Ensure newly created network files/folders maintain clean 770/660 mapping
    directory mask = 0770
    create mask = 0660

```

*Run `sudo systemctl reload smbd` to apply.*

---

## Phase 2: Starting Small (The "Day One" Blueprint)

When a project is brand new, it is simple. It only needs the root folder and the open collaboration folder (`/shr`).

### The Identity Setup (LDAP, Active Directory, or Local Linux)

Create **one** master group for the project and add all project members to it:

* Group Name: `grp-banana`

### The Directory Provisioning

Run these commands to build the initial structure (or put them into a basic script):

```bash
# 1. Create the directories
mkdir -p /projects/banana/shr

# 2. Configure the Project Root
# Owned by root, group-owned by the project team.
chown root:grp-banana /projects/banana
# 2750 = Owners can modify; Project Group can entry/read (r-x); Others are locked out (---)
chmod 2750 /projects/banana

# 3. Configure the Standard Share Folder (/shr)
# Group-owned by the project team.
chown root:grp-banana /projects/banana/shr
# 2770 = Project Group gets full read/write/execute (rwx) + SetGID inheritance bit
chmod 2770 /projects/banana/shr

```

### The Result on Day One:

* Everyone in `grp-banana` can see the `/projects/banana` folder.
* Inside it, they see `/shr` and have full read/write rights to collaborate.
* Network visibility is clean, and paths are compact.

---

## Phase 3: Growing Complex (The "Add-On" Blueprint)

Six months later, the project expands. The team requests a restricted management folder (`/adm`) and a samples folder (`/samples`) that standard users shouldn't even know exist.

**Crucial Rule:** You do *not* touch the root folder or the existing `/shr` folder. You leave them completely alone.

### The Identity Upgrade

Create specialized sub-groups for the specific restricted areas, and assign only the privileged users to them:

* Group Name: `grp-banana-adm` (Managers)
* Group Name: `grp-banana-samples` (Samples Team)

### The Sibling Provisioning

Run these commands to deploy the new complex folders side-by-side with `/shr`:

```bash
# 1. Create the new sibling folders
mkdir -p /projects/banana/adm
mkdir -p /projects/banana/mkt

# 2. Configure the Admin Folder
chown root:grp-banana-adm /projects/banana/adm
chmod 2770 /projects/banana/adm

# 3. Configure the Marketing Folder
chown root:grp-banana-mkt /projects/banana/mkt
chmod 2770 /projects/banana/mkt

```

### The Result of Growing Complex:

Samba’s ABE automatically evaluates the local UNIX permissions on the fly.

* **Standard Users** (only in `grp-banana`) open the project and see **only** `/shr`. To them, `/adm` and `/mkt` are completely invisible.
* **Managers** (in `grp-banana` and `grp-banana-adm`) open the project and see both `/shr` and `/adm`.
* **Marketing Users** (in `grp-banana` and `grp-banana-mkt`) open the project and see both `/shr` and `/mkt`.

---

## Phase 4: Scaling to Thousands (Automation Script)

Because your `smb.conf` is static and the logic is entirely embedded in standard directory permissions, you can automate project creation completely.

Below is an enterprise-grade Bash script (`create_project.sh`) that you can run manually, hook into a web portal, or trigger via an API whenever a new project is approved.

```bash
#!/bin/bash

# Usage: ./create_project.sh [project_name] [primary_group]
# Example: ./create_project.sh apple grp-apple

PROJECT_NAME=$1
PRIMARY_GROUP=$2
BASE_PATH="/projects/$PROJECT_NAME"

if [ -z "$PROJECT_NAME" ] || [ -z "$PRIMARY_GROUP" ]; then
    echo "Usage: $0 [project_name] [primary_group]"
    exit 1
fi

echo "Provisioning project: $PROJECT_NAME..."

# 1. Create the fundamental directories
mkdir -p "$BASE_PATH/shr"

# 2. Apply permissions to the Root Gatekeeper (Read-Only to group)
chown root:"$PRIMARY_GROUP" "$BASE_PATH"
chmod 2750 "$BASE_PATH"

# 3. Apply Full Write permissions to the /shr folder
chown root:"$PRIMARY_GROUP" "$BASE_PATH/shr"
chmod 2770 "$BASE_PATH/shr"

echo "Success! Project $PROJECT_NAME is active."
echo "Root path: $BASE_PATH (chmod 2750)"
echo "Shared path: $BASE_PATH/shr (chmod 2770)"

```

### To add a complex sibling later, use this secondary automation script (`add_sibling.sh`):

```bash
#!/bin/bash

# Usage: ./add_sibling.sh [project_name] [folder_name] [target_group]
# Example: ./add_sibling.sh banana adm grp-banana-adm

PROJECT_NAME=$1
FOLDER_NAME=$2
TARGET_GROUP=$3
TARGET_PATH="/projects/$PROJECT_NAME/$FOLDER_NAME"

if [ -z "$PROJECT_NAME" ] || [ -z "$FOLDER_NAME" ] || [ -z "$TARGET_GROUP" ]; then
    echo "Usage: $0 [project_name] [folder_name] [target_group]"
    exit 1
fi

echo "Adding restricted sibling '$FOLDER_NAME' to project '$PROJECT_NAME'..."

mkdir -p "$TARGET_PATH"
chown root:"$TARGET_GROUP" "$TARGET_PATH"
chmod 2770 "$TARGET_PATH"

echo "Success! Restricted sibling folder is live and managed by ABE."

```

### Why this plan works at massive scale:

1. **Zero Configuration Bloat:** Your `smb.conf` stays tiny forever.
2. **Instant Performance:** Linux group evaluation (`770`/`2750`) happens at the kernel layer, making file browsing insanely fast even with tens of thousands of users.
3. **Short Paths:** Using 3-letter structures (`/shr`, `/adm`, `/mkt`) prevents remote Windows and Mac clients from breaking over deep file structures. 
