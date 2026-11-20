# Migrate from Mercurial to Git (`hgtogit`)

> [Português (PT-BR)](README.pt-BR.md)

A standalone Python 3 script to migrate Mercurial (`hg`) repositories to Git, built for repositories with subrepositories (`.hgsub` / `.hgsubstate`), complex merge histories, and large commit counts.

- [Migrate from Mercurial to Git (`hgtogit`)](#migrate-from-mercurial-to-git-hgtogit)
  - [Why `hgtogit`?](#why-hgtogit)
  - [Prerequisites & Directory Layout](#prerequisites--directory-layout)
  - [Configuration](#configuration)
  - [Migrate an existing Hg repository to local Git](#migrate-an-existing-hg-repository-to-local-git)
  - [Resume an interrupted migration](#resume-an-interrupted-migration)
  - [Verify with the synthetic test suite](#verify-with-the-synthetic-test-suite)
  - [How it works](#how-it-works)

---

## Why `hgtogit`?

Existing tools like `hg-fast-export` and `hg-git` often struggle with subrepositories, long-running Windows migrations, or interrupted runs. `hgtogit` addresses those gaps:

- **Dynamic submodule mapping**: Translates 40-char Mercurial hashes in `.hgsubstate` into Git commit hashes using `hg2git-mapping` / `hg2git-marks`, pinning `.gitmodules` and gitlinks at every commit.
- **Per-commit save-points & resume**: Saves progress (`hg_to_git_save.json` and `last_substates_save.json`) after every commit so you can pause or recover without restarting from commit `0`.
- **True 2-parent merge commits**: Checks out `p1node` and writes `p2node` to `.git/MERGE_HEAD` before committing, preserving the original DAG topology.
- **Submodule directory shielding**: Excludes active submodule folders during `rsync` so nested `.git` metadata is never overwritten.
- **Windows-ready & zero Python dependencies**: Uses only the Python 3 standard library plus `git`, `hg`, and `rsync` CLI tools, with read-only NTFS object cleanup and UTF-8 file-based commit messages.

---

## Prerequisites & Directory Layout

Make sure the following CLI tools are available in your `PATH`:

- `python` (Python 3.8+)
- `git`
- `hg` (Mercurial)
- `rsync` (on Windows, available via Git Bash / MSYS2 / WSL)

### Expected layout before running

1. **Source Hg repository (`REPO_HG`)**: A local clone/copy of your Mercurial repository.
2. **Pre-converted submodule Git repos (`SUBREPOS`)**: Each subrepository listed in `.hgsubstate` must already be converted to a local Git repository (e.g., via `hg-fast-export`) containing `.git/hg2git-mapping` and `.git/hg2git-marks`.
3. **Working directory**: Run `hgtogit.py` from the **same directory** where the script lives (or where you want `migration_report.txt`, `hg_to_git_save.json`, and `last_substates_save.json` to be written).

> **Warning:** When `START_COMMIT_IDX = 0`, the script wipes and recreates `REPO_GIT` from scratch. Never point `REPO_GIT` at a directory with unsaved work.

---

## Configuration

Edit the constants at the top of `hgtogit.py`:

```python
REPO_HG = "/path/to/source/mercurial/repo"
REPO_GIT = "/path/to/destination/git/repo"

SUBREPOS = {
    "subrepo_1": {
        "git_path": "/path/to/subrepo_1.git",
        "git_repo_url": "/path/to/subrepo_1.git/",
        "mapping_file": "/path/to/subrepo_1.git/.git/hg2git-mapping",
        "marks_file": "/path/to/subrepo_1.git/.git/hg2git-marks",
    },
}

START_COMMIT_IDX = 0
END_COMMIT_IDX = None  # None = process until the latest commit
```

---

## Migrate an existing Hg repository to local Git

Go to the directory containing `hgtogit.py`:

```sh
cd /path/to/hgtogit
python hgtogit.py
```

The script will initialize `REPO_GIT`, replay each Mercurial revision in chronological order, map submodules, create `main`, and run `git submodule update --init --recursive`.

To inspect the resulting Git history:

```sh
cd /path/to/destination/git/repo
git log --all --decorate --oneline --graph
```

---

## Resume an interrupted migration

If a migration stops mid-way (check `migration_report.txt` or console output for the last completed index):

1. Keep `hg_to_git_save.json` and `last_substates_save.json` in the working directory.
2. Update `START_COMMIT_IDX` in `hgtogit.py` to the next commit index to process (e.g., `START_COMMIT_IDX = 1500`).
3. Run `python hgtogit.py` again from the **same directory** where the save-point JSON files are located.

---

## Verify with the synthetic test suite

You can verify the migration logic offline without any private repositories using the included synthetic test suite:

```sh
python -m py_compile hgtogit.py
python -m unittest -v test_synthetic_hgtogit.py
```

---

## How it works

For each revision from `START_COMMIT_IDX` to `END_COMMIT_IDX`, `hgtogit.py` executes:

1. **Parent checkout**: Checks out `p1node` in Git and writes `p2node` (if present) to `.git/MERGE_HEAD`.
2. **Mercurial update**: Cleans physical subrepo folders in `REPO_HG` and runs `hg update -C <rev>`.
3. **File sync**: Reads `.hgsubstate` and runs `rsync` from `REPO_HG` to `REPO_GIT`, excluding `.hg/`, `.git/`, and active submodules.
4. **Submodule update**: Resolves each Hg subrepo hash to its Git hash via `hg2git-mapping` / `hg2git-marks` and updates `.gitmodules` + gitlink pointers.
5. **Commit & checkpoint**: Commits with the original author, email, date, and UTF-8 message, then saves `HG_TO_GIT` and `last_substates` to disk.
