import os
import subprocess
import json
import shutil
import stat

# ==============================================================
# 1. INITIAL CONFIGURATION, PATHS, AND GLOBAL DATA STRUCTURES
# ==============================================================

# Path to the source Mercurial repository
REPO_HG = "/path/to/source/mercurial/repo"

# Path to the destination Git repository (will be initialized)
REPO_GIT = "/path/to/destination/git/repo"

# Configuration for converted submodules:
# Keys are submodule directory names relative to repository root.
# - git_path: Local path to the converted Git repository of the submodule.
# - git_repo_url: URL or path used in .gitmodules (can be a local file:/// URL).
# - mapping_file: Path to fast-export .git/hg2git-mapping file.
# - marks_file: Path to fast-export .git/hg2git-marks file.
SUBREPOS = {
    "subrepo_1": {
        "git_path": "/path/to/subrepo_1.git",
        "git_repo_url": "/path/to/subrepo_1.git/",
        "mapping_file": "/path/to/subrepo_1.git/.git/hg2git-mapping",
        "marks_file": "/path/to/subrepo_1.git/.git/hg2git-marks",
    },
    "subrepo_2": {
        "git_path": "/path/to/subrepo_2.git",
        "git_repo_url": "/path/to/subrepo_2.git/",
        "mapping_file": "/path/to/subrepo_2.git/.git/hg2git-mapping",
        "marks_file": "/path/to/subrepo_2.git/.git/hg2git-marks",
    },
}

# State persistence and report file paths
REPORT_FILE = "migration_report.txt"
SAVE_MAP_FILE = "hg_to_git_save.json"
SAVE_SUBSTATES_FILE = "last_substates_save.json"

# Default commit range (0 = start from scratch; None = process all commits)
START_COMMIT_IDX = 0
END_COMMIT_IDX = None

# Global mapping: Mercurial node hash -> Git commit hash
HG_TO_GIT = {}


# ====================================================
# -1. REPORT LOGGING AND TERMINAL COMMAND EXECUTION
# ====================================================

def log_report(message):
    """ Append message to the migration report file. """
    with open(REPORT_FILE, "a", encoding="utf-8") as f:
        f.write(message + "\n")

def run(cmd, cwd=None, env=None, check=True):
    """
    Execute a shell command via subprocess.
    NOTE: shell=False prevents Windows CMD from misinterpreting special characters such as '|' (pipe).
    """
    result = subprocess.run(
        cmd, 
        cwd=cwd, 
        env=env, 
        capture_output=True, 
        text=True, 
        shell=False
    )
    
    if result.returncode != 0 and check:
        cmd_print = " ".join(cmd) if isinstance(cmd, list) else cmd
        error_msg = f"FATAL ERROR executing: {cmd_print}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        log_report(error_msg)
        print(error_msg)
        raise RuntimeError(f"Command failed with exit code {result.returncode}")
        
    return result.stdout.strip()


# ====================================================
# 2. MAPPING FILE READERS (HG -> GIT)
# ====================================================

def load_hg2git_mapping(mapping_file):
    """ Read `.git/hg2git-mapping` -> {hg_hash (short): git_hash}. """
    mapping = {}
    if not os.path.exists(mapping_file):
        log_report(f"Warning: Mapping file not found: {mapping_file}")
        return mapping
        
    with open(mapping_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith(":"):
                try:
                    git_hash, hg_hash = line[1:].strip().split()
                    mapping[hg_hash[:12]] = git_hash  
                except ValueError:
                    continue
    return mapping

def load_hg2git_marks(marks_file):
    """ Read `.git/hg2git-marks` -> {mark_id: git_hash}. """
    mapping = {}
    if not os.path.exists(marks_file):
        log_report(f"Warning: Marks file not found: {marks_file}")
        return mapping
        
    with open(marks_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith(":"):
                try:
                    hg_hash, git_hash = line[1:].strip().split()
                    mapping[hg_hash[:12]] = git_hash
                except ValueError:
                    continue
    return mapping

HASH_MAPS = {name: load_hg2git_mapping(info["mapping_file"]) for name, info in SUBREPOS.items()}
HASH_MARKS = {name: load_hg2git_marks(info["marks_file"]) for name, info in SUBREPOS.items()}


# ====================================================
# 3. MERCURIAL HISTORY EXTRACTION
# ====================================================

def hg_log():
    """
    Extract the Mercurial commit history formatted with a structured delimiter.
    Returns a list of dictionaries ordered chronologically from OLDEST to NEWEST.
    """
    print("Extracting Mercurial history...")
    log_report("Extracting Mercurial history...")
    
    sep = "|||SEP|||"
    log_format = f"{{rev}}{sep}{{node}}{sep}{{author|person}}{sep}{{author|email}}{sep}{{date|isodate}}{sep}{{p1node}}{sep}{{p2node}}{sep}{{desc|strip}}"
    
    out = run(["hg", "log", "-T", f"{log_format}\\n;;;END;;;\\n"], cwd=REPO_HG)
    
    entries = []
    commits = out.split(";;;END;;;")
    
    for commit_raw in commits:
        commit_raw = commit_raw.strip()
        if not commit_raw:
            continue
            
        parts = commit_raw.split(sep, 7)
        if len(parts) == 8:
            rev, node, author, email, date, p1node, p2node, message = parts
            
            message = message.replace("&lt;", "<").replace("&gt;", ">")
            
            entries.append({
                "rev": rev,
                "node": node,
                "author": author.strip(),
                "email": email.strip() if email.strip() else "dev@localhost",
                "date": date.strip(),
                "p1node": p1node.strip(),
                "p2node": p2node.strip(),
                "message": message.strip()
            })
            
    entries.reverse()
    return entries

def read_hgsubstate():
    """ Read the Mercurial .hgsubstate file and return {submodule_name: hg_hash}. """
    path = os.path.join(REPO_HG, ".hgsubstate")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {
            line.strip().split()[1]: line.strip().split()[0]
            for line in f if line.strip()
        }


# ====================================================
# 4. SYSTEM INITIALIZATION: RESET OR RESUME FROM SAVE POINT
# ====================================================

def remove_readonly(func, path, _excinfo):
    """ Error callback to force removal of read-only files on Windows during shutil.rmtree. """
    os.chmod(path, stat.S_IWRITE)
    func(path)

def clean_and_init_git():
    """ Delete the destination Git repository directory and initialize a clean repository. """
    log_report("\n--- INITIALIZING GIT REPOSITORY ---")
    
    if os.path.exists(REPO_GIT):
        shutil.rmtree(REPO_GIT, onexc=remove_readonly)
        
    os.makedirs(REPO_GIT, exist_ok=True)
    run(["git", "init"], cwd=REPO_GIT)
    
    run(["git", "config", "protocol.file.allow", "always"], cwd=REPO_GIT)
    
    log_report(f"Git repository initialized at {REPO_GIT}")

def hg_checkout(rev):
    """ Checkout a specific revision in Mercurial after removing nested subrepo directories. """
    for sub in SUBREPOS.keys():
        sub_path = os.path.join(REPO_HG, sub)
        if os.path.exists(sub_path):
            shutil.rmtree(sub_path, onexc=remove_readonly)
            
    cmd = ["hg", "update", "-C", rev]
    result = subprocess.run(cmd, cwd=REPO_HG, capture_output=True, text=True, shell=False)
    
    if result.returncode != 0:
        error_output = (result.stdout + result.stderr).lower()
        if "subrepo" in error_output or "nested repo" in error_output:
            log_report("    [!] Ignoring broken link or nested repo in Mercurial...")
        else:
            log_report(f"FATAL ERROR executing: {' '.join(cmd)}")
            log_report(f"STDERR: {result.stderr}")
            raise RuntimeError(f"Command failed with exit code {result.returncode}")


# ====================================================
# 5. SUBMODULE CHANGE DETECTION AND POINTER SYNCHRONIZATION
# ====================================================

def process_submodules(substates, last_substates):
    """ Check if any submodule state changed and update submodule pointers in Git. """
    if substates == last_substates:
        return
        
    for sub, hg_commit in substates.items():
        if sub not in SUBREPOS:
            continue
        if int(hg_commit, 16) == 0:
            log_report(f"    Submodule '{sub}' deactivated/empty (Hash 0000...). Ignoring.")
            if sub in last_substates and int(last_substates[sub], 16) != 0:
                run(["git", "rm", "-f", sub], cwd=REPO_GIT, check=False)
            continue

        git_commit_hash = None
        for key_hash, value_hash in HASH_MAPS[sub].items():
            if value_hash == hg_commit:
                git_commit_hash = HASH_MARKS[sub].get(key_hash)
                break

        if not git_commit_hash:
            log_report(f"    [!] WARNING: Hash {hg_commit} not found in subrepo '{sub}'!")
            continue

        sub_path = os.path.join(REPO_GIT, sub)

        if not os.path.exists(os.path.join(sub_path, ".git")):
            # Submodule registration check:
            gitmodules_path = os.path.join(REPO_GIT, ".gitmodules")
            in_gitmodules = False
            
            # Check whether the submodule is already registered in .gitmodules
            if os.path.exists(gitmodules_path):
                with open(gitmodules_path, "r") as f:
                    if f'[submodule "{sub}"]' in f.read():
                        in_gitmodules = True

            if not in_gitmodules:
                # If not registered in .gitmodules, force Git to register it from scratch
                if os.path.exists(sub_path):
                    shutil.rmtree(sub_path, ignore_errors=True)
                run(["git", "rm", "-r", "--cached", sub], cwd=REPO_GIT, check=False)
                
                repo_url = f"file:///{SUBREPOS[sub]['git_repo_url']}"
                run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-f", repo_url, sub], cwd=REPO_GIT)

        log_report(f"    Updating '{sub}' to Git commit: {git_commit_hash[:8]}")
        run(["git", "fetch"], cwd=sub_path)
        
        run(["git", "reset", "--hard"], cwd=sub_path)
        run(["git", "clean", "-fd"], cwd=sub_path)
        
        run(["git", "checkout", "-f", git_commit_hash], cwd=sub_path)


# ====================================================
# 6. PARENT RESOLUTION, FILE RSYNC, AND COMMIT CREATION
# ====================================================

def convert_path_for_rsync(path):
    """ Convert Windows drive path ('C:/dir') to POSIX path ('/c/dir') for rsync compatibility. """
    if len(path) > 1 and path[1] == ':':
        return f"/{path[0].lower()}{path[2:]}"
    return path

def copy_files(substates):
    """ Synchronize files from Hg to Git using rsync, excluding metadata and active submodules. """
    src = convert_path_for_rsync(REPO_HG) + "/"
    dst = convert_path_for_rsync(REPO_GIT) + "/"
    
    cmd = [
        "rsync", "-a", "--delete",
        "--exclude=.gitmodules", 
        "--exclude=.git/", 
        "--exclude=.hg", 
        "--exclude=.hgsubstate", 
        "--exclude=.hgsub"
    ]
    
    # Exclude active submodule directories to protect submodule checkouts from rsync deletion
    for sub, hg_commit in substates.items():
        if int(hg_commit, 16) != 0:
            cmd.append(f"--exclude={sub}/")
            cmd.append(f"--exclude={sub}")
            
    cmd.extend([src, dst])
    run(cmd)

def is_valid_parent(node_hash):
    """ Verify if the parent node hash is valid (ignoring Mercurial null sentinel '0000...'). """
    try:
        return node_hash and int(node_hash, 16) != 0
    except ValueError:
        return False

def git_commit(author, email, date, message):
    """ Create a Git commit replicating Mercurial author, committer, and timestamp metadata. """
    env = os.environ.copy()
    
    env["GIT_AUTHOR_NAME"] = author
    env["GIT_COMMITTER_NAME"] = author
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_EMAIL"] = email
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date

    run(["git", "add", "."], cwd=REPO_GIT)

    msg_file = os.path.join(REPO_GIT, ".git", "commit_msg.tmp")
    with open(msg_file, "w", encoding="utf-8") as f:
        f.write(message)

    merge_file = os.path.join(REPO_GIT, ".git", "MERGE_HEAD")
    
    if os.path.exists(merge_file):
        run(["git", "commit", "--allow-empty", "--no-edit", "-F", msg_file], cwd=REPO_GIT, env=env)
    else:
        run(["git", "commit", "--allow-empty", "-F", msg_file], cwd=REPO_GIT, env=env)
        
    if os.path.exists(msg_file):
        os.remove(msg_file)


# ====================================================
# 7. MAIN REPLAY LOOP AND STATE PERSISTENCE
# ====================================================

def main(start_idx=START_COMMIT_IDX, end_idx=END_COMMIT_IDX):
    global HASH_MAPS, HASH_MARKS
    HASH_MAPS = {name: load_hg2git_mapping(info["mapping_file"]) for name, info in SUBREPOS.items()}
    HASH_MARKS = {name: load_hg2git_marks(info["marks_file"]) for name, info in SUBREPOS.items()}
    
    commit_list = hg_log()
    last_substates = {}
    
    if start_idx == 0:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("=== START OF MIGRATION ===\n")
        clean_and_init_git()
        HG_TO_GIT.clear()
    else:
        print(f"\n--- RESUMING FROM SAVE POINT: COMMIT {start_idx} ---")
        with open(REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n--- RESUMING FROM SAVE POINT: COMMIT {start_idx} ---\n")
            
        with open(SAVE_MAP_FILE, "r") as f:
            HG_TO_GIT.update(json.load(f))
        with open(SAVE_SUBSTATES_FILE, "r") as f:
            last_substates.update(json.load(f))

    limit_idx = len(commit_list) if end_idx is None else min(end_idx + 1, len(commit_list))
    for idx in range(start_idx, limit_idx):
        c = commit_list[idx]
        rev = c['rev']
        node = c['node']
        p1 = c['p1node']
        p2 = c['p2node']
        
        print(f"[{idx}] Processing rev {rev}...")
        log_report(f"\n[{idx}] Processing rev {rev} (Hg: {node[:12]})...")

        if is_valid_parent(p1) and p1 in HG_TO_GIT:
            parent_git_hash = HG_TO_GIT[p1]
            run(["git", "checkout", "-f", parent_git_hash], cwd=REPO_GIT)
        elif idx > 0:
            log_report(f"  [!] WARNING: Parent 1 ({p1[:12]}) not found in mapping!")

        if is_valid_parent(p2) and p2 in HG_TO_GIT:
            p2_git_hash = HG_TO_GIT[p2]
            log_report(f"  --> Preparing merge with p2: {p2[:12]}")
            merge_file = os.path.join(REPO_GIT, ".git", "MERGE_HEAD")
            with open(merge_file, "wb") as f:
                f.write((p2_git_hash + "\n").encode("utf-8"))

        hg_checkout(rev)

        substates = read_hgsubstate()
        copy_files(substates)
        process_submodules(substates, last_substates)
        last_substates = substates.copy()

        git_commit(c['author'], c['email'], c['date'], c['message'])

        current_git_hash = run(["git", "rev-parse", "HEAD"], cwd=REPO_GIT)
        HG_TO_GIT[node] = current_git_hash
        log_report(f"  --> Commit saved in Git: {current_git_hash[:8]}")
        
        with open(SAVE_MAP_FILE, "w") as f:
            json.dump(HG_TO_GIT, f)
        with open(SAVE_SUBSTATES_FILE, "w") as f:
            json.dump(last_substates, f)
    print("\n--- FINALIZING MIGRATION ---")
    log_report("--- FINALIZING MIGRATION ---")
    
    print("Creating and pointing to 'main' branch...")
    run(["git", "checkout", "-B", "main"], cwd=REPO_GIT)
    
    print("Initializing and synchronizing final submodules...")
    run(["git", "submodule", "update", "--init", "--recursive"], cwd=REPO_GIT)
    
    print("✅ REPOSITORY CONVERTED SUCCESSFULLY!")
    log_report("✅ REPOSITORY CONVERTED SUCCESSFULLY!")


# ====================================================
# 8. SCRIPT ENTRY POINT
# ====================================================

if __name__ == "__main__":
    main(START_COMMIT_IDX, END_COMMIT_IDX)

# Command to view git commit history graph:
# cd <REPO_GIT>
# git log --all --decorate --oneline --graph
