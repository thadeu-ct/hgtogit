"""
test_synthetic_hgtogit.py - Hermetic Synthetic Test Suite for hgtogit

Comprehensive end-to-end synthetic verification suite implementing Requirement R4.
This suite creates isolated, on-the-fly Mercurial and Git repositories inside
temporary directories with zero dependencies on external network or private repositories.

Cross-Platform Compatibility:
- Includes a pure-Python file synchronizer replacing `rsync` where unavailable (e.g. Windows).
- Provides execution shims for environments where MSYS2/Cygwin sub-processes encounter
  restricted sandboxes.
- Configures standard streams for UTF-8 compatibility across Windows consoles.

Test Tiers:
- Tier 1: Happy path linear conversion (file additions, modifications, deletions,
          commit author/email/date metadata fidelity).
- Tier 2: Boundary and corner cases (empty commit, spaces/special characters in filenames,
          XML/HTML angle bracket unescaping in commit messages, missing author email fallback,
          and two-parent merge DAG topologies).
- Tier 3: Submodule mapping (.hgsubstate parsing, pointer translation via fast-export
          mapping and marks tables, submodule addition, and submodule deactivation).
- Tier 4: Save-point and resume capability (partial batch migration 0..N followed by
          resumed migration N..M verifying state persistence and unbroken history continuity).
"""

import os
import sys
import json
import stat
import shutil
import tempfile
import subprocess
import unittest

# Ensure UTF-8 output encoding on consoles that default to cp1252 or ascii
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure project root is present in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import hgtogit


# ==============================================================================
# Helper Utilities: Filesystem and Synchronizer Shims
# ==============================================================================

def remove_readonly(func, path, _excinfo):
    """Callback for shutil.rmtree to remove Windows read-only attributes on NTFS."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def safe_remove_file(path: str) -> None:
    """Remove a file, stripping Windows read-only attribute if necessary."""
    try:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)
    except Exception:
        pass


def safe_copy_git_dir(src_dir: str, dst_dir: str) -> None:
    """
    Recursively copy Git object/ref directories without overwriting identical
    content-addressable read-only objects on Windows.
    """
    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target_dir = os.path.join(dst_dir, rel)
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            src_f = os.path.join(root, f)
            dst_f = os.path.join(target_dir, f)
            if not os.path.exists(dst_f):
                try:
                    shutil.copy2(src_f, dst_f)
                except PermissionError:
                    try:
                        os.chmod(dst_f, stat.S_IWRITE)
                        shutil.copy2(src_f, dst_f)
                    except Exception:
                        pass


def pure_python_copy_files(substates: dict) -> None:
    """
    Hermetic cross-platform file synchronizer replacing rsync in test environments.
    
    Mirrors `rsync -a --delete` while strictly respecting exclude patterns:
    excludes `.git/`, `.gitmodules`, `.hg/`, `.hgsub`, `.hgsubstate`, and all active
    submodule directories. Cleans removed destination files and recursively copies
    new/modified source files.
    """
    repo_hg = hgtogit.REPO_HG
    repo_git = hgtogit.REPO_GIT
    excluded_names = {".git", ".gitmodules", ".hg", ".hgsub", ".hgsubstate"}
    for sub, hg_commit in substates.items():
        try:
            if int(hg_commit, 16) != 0:
                excluded_names.add(sub.rstrip("/\\"))
        except ValueError:
            pass

    # 1. Clean destination: remove files and directories no longer in source
    for root, dirs, files in os.walk(repo_git, topdown=False):
        rel_root = os.path.relpath(root, repo_git)
        if rel_root == ".":
            for f in files:
                if f in excluded_names:
                    continue
                if not os.path.exists(os.path.join(repo_hg, f)):
                    safe_remove_file(os.path.join(root, f))
            for d in list(dirs):
                if d in excluded_names:
                    continue
                dst_d = os.path.join(root, d)
                src_d = os.path.join(repo_hg, d)
                if not os.path.exists(src_d):
                    shutil.rmtree(dst_d, onexc=remove_readonly)
        else:
            first_comp = rel_root.replace("\\", "/").split("/")[0]
            if first_comp in excluded_names:
                continue
            for f in files:
                rel_f = os.path.relpath(os.path.join(root, f), repo_git)
                if not os.path.exists(os.path.join(repo_hg, rel_f)):
                    safe_remove_file(os.path.join(root, f))
            for d in list(dirs):
                rel_d = os.path.relpath(os.path.join(root, d), repo_git)
                src_d = os.path.join(repo_hg, rel_d)
                if not os.path.exists(src_d):
                    shutil.rmtree(os.path.join(root, d), onexc=remove_readonly)

    # 2. Copy source files to destination
    for root, dirs, files in os.walk(repo_hg, topdown=True):
        rel_root = os.path.relpath(root, repo_hg)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in excluded_names]
            dest_root = repo_git
        else:
            first_comp = rel_root.replace("\\", "/").split("/")[0]
            if first_comp in excluded_names:
                dirs[:] = []
                continue
            dest_root = os.path.join(repo_git, rel_root)
            os.makedirs(dest_root, exist_ok=True)

        for f in files:
            if rel_root == "." and f in excluded_names:
                continue
            src_file = os.path.join(root, f)
            dest_file = os.path.join(dest_root, f)
            shutil.copy2(src_file, dest_file)


def make_robust_run(original_run):
    """
    Wrap hgtogit.run to provide fallback interception for missing rsync and
    MSYS2/Cygwin sub-process restrictions in sandboxed Windows environments.
    """
    def robust_run(cmd, cwd=None, env=None, check=True):
        if isinstance(cmd, list) and cmd:
            if cmd[0] == "rsync":
                substates = {}
                pure_python_copy_files(substates)
                return ""

            if "submodule" in cmd and "update" in cmd:
                return ""

            if "submodule" in cmd and "add" in cmd:
                sub = cmd[-1]
                repo_url = cmd[-2]
                target_repo = cwd or hgtogit.REPO_GIT
                gitmodules = os.path.join(target_repo, ".gitmodules")
                entry = f'[submodule "{sub}"]\n\tpath = {sub}\n\turl = {repo_url}\n'
                with open(gitmodules, "a" if os.path.exists(gitmodules) else "w", encoding="utf-8") as f:
                    f.write(entry)
                sub_dest = os.path.join(target_repo, sub)
                if sub in hgtogit.SUBREPOS:
                    src_sub = hgtogit.SUBREPOS[sub]["git_path"]
                    safe_copy_git_dir(src_sub, sub_dest)
                original_run(["git", "add", ".gitmodules"], cwd=target_repo, check=True)
                return ""

            if cmd == ["git", "fetch"] or (len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "fetch"):
                for sub_name, info in hgtogit.SUBREPOS.items():
                    sub_dir = os.path.normpath(os.path.join(hgtogit.REPO_GIT, sub_name))
                    if cwd and os.path.normpath(cwd) == sub_dir:
                        src_git = os.path.join(info["git_path"], ".git") if os.path.exists(os.path.join(info["git_path"], ".git")) else info["git_path"]
                        dst_git = os.path.join(cwd, ".git") if os.path.exists(os.path.join(cwd, ".git")) else cwd
                        src_obj = os.path.join(src_git, "objects")
                        dst_obj = os.path.join(dst_git, "objects")
                        if os.path.exists(src_obj):
                            safe_copy_git_dir(src_obj, dst_obj)
                        src_refs = os.path.join(src_git, "refs")
                        dst_refs = os.path.join(dst_git, "refs")
                        if os.path.exists(src_refs):
                            safe_copy_git_dir(src_refs, dst_refs)
                        break
                return ""

        return original_run(cmd, cwd=cwd, env=env, check=check)
    return robust_run


# ==============================================================================
# Base Test Fixture Class
# ==============================================================================

class BaseSyntheticTest(unittest.TestCase):
    """
    Isolated test fixture managing temporary directories, environment isolation,
    module monkeypatching, and cleanup.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = self.temp_dir.name
        self.orig_cwd = os.getcwd()
        os.chdir(self.test_root)

        self.repo_hg = os.path.join(self.test_root, "repo_hg")
        self.repo_git = os.path.join(self.test_root, "repo_git")
        os.makedirs(self.repo_hg, exist_ok=True)
        os.makedirs(self.repo_git, exist_ok=True)

        # Snapshot original hgtogit module state
        self.orig_REPO_HG = hgtogit.REPO_HG
        self.orig_REPO_GIT = hgtogit.REPO_GIT
        self.orig_SUBREPOS = hgtogit.SUBREPOS.copy()
        self.orig_HG_TO_GIT = hgtogit.HG_TO_GIT.copy()
        self.orig_copy_files = hgtogit.copy_files
        self.orig_run = hgtogit.run

        # Bind test-specific variables and robust synchronization handlers
        hgtogit.REPO_HG = self.repo_hg
        hgtogit.REPO_GIT = self.repo_git
        hgtogit.SUBREPOS = {}
        hgtogit.HG_TO_GIT = {}
        hgtogit.copy_files = pure_python_copy_files
        hgtogit.run = make_robust_run(self.orig_run)

        # Initialize synthetic Mercurial source repository with isolated configuration
        subprocess.run(["hg", "init", self.repo_hg], check=True)
        with open(os.path.join(self.repo_hg, ".hg", "hgrc"), "w", encoding="utf-8") as f:
            f.write("[ui]\nusername = Synthetic Tester <tester@synthetic.local>\n")

    def tearDown(self):
        # Restore hgtogit module state
        hgtogit.REPO_HG = self.orig_REPO_HG
        hgtogit.REPO_GIT = self.orig_REPO_GIT
        hgtogit.SUBREPOS = self.orig_SUBREPOS
        hgtogit.HG_TO_GIT = self.orig_HG_TO_GIT
        hgtogit.copy_files = self.orig_copy_files
        hgtogit.run = self.orig_run

        os.chdir(self.orig_cwd)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def hg_commit(self, message: str, author: str = "Tester <tester@synthetic.local>",
                  date: str = "2026-01-01 12:00:00 +0000", files: dict = None,
                  deletes: list = None, subrepos: bool = False) -> str:
        """Helper to create and commit changes in the synthetic Mercurial repo."""
        if files:
            for rel_path, content in files.items():
                full_path = os.path.join(self.repo_hg, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                subprocess.run(["hg", "add", rel_path], cwd=self.repo_hg, check=False)
        if deletes:
            for rel_path in deletes:
                subprocess.run(["hg", "rm", rel_path], cwd=self.repo_hg, check=True)
        cmd = ["hg", "commit", "-m", message, "-u", author, "-d", date]
        if subrepos:
            cmd.insert(2, "-S")
        subprocess.run(cmd, cwd=self.repo_hg, check=True)
        node = subprocess.run(["hg", "log", "-r", ".", "-T", "{node}"],
                              cwd=self.repo_hg, capture_output=True, text=True, check=True).stdout.strip()
        return node

    def git_query(self, args: list) -> str:
        """Helper to query the destination Git repository."""
        res = subprocess.run(["git"] + args, cwd=self.repo_git, capture_output=True, text=True, check=True)
        return res.stdout.strip()


# ==============================================================================
# Tier 1: Happy Path Linear Conversion
# ==============================================================================

class TestTier1HappyPath(BaseSyntheticTest):
    """
    Tier 1 verifies linear history conversion fidelity:
    - Sequential file addition, modification, and deletion.
    - Full fidelity of commit author, email, date, and message.
    - Checkpoint mapping persistence in save state JSON files.
    """

    def test_linear_conversion(self):
        c0 = self.hg_commit(
            "Commit 0: initial files",
            author="Alice Smith <alice@test.org>",
            date="2026-01-01 10:00:00 +0000",
            files={"file1.txt": "v1", "common.txt": "shared content"}
        )
        c1 = self.hg_commit(
            "Commit 1: modify file1, add file2",
            author="Bob Jones <bob@test.org>",
            date="2026-01-02 11:00:00 +0000",
            files={"file1.txt": "v2 modified", "file2.txt": "file 2 content"}
        )
        c2 = self.hg_commit(
            "Commit 2: delete file1, add nested file",
            author="Charlie Brown <charlie@test.org>",
            date="2026-01-03 12:00:00 +0000",
            files={"nested/dir/f3.txt": "nested file 3 content"},
            deletes=["file1.txt"]
        )

        hgtogit.main()

        # 1. Total commits count
        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "3")
        # 2. Main branch points to HEAD
        self.assertEqual(self.git_query(["rev-parse", "--abbrev-ref", "HEAD"]), "main")

        # 3. File tree verification
        self.assertFalse(os.path.exists(os.path.join(self.repo_git, "file1.txt")), "file1.txt should be deleted")
        self.assertTrue(os.path.exists(os.path.join(self.repo_git, "file2.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.repo_git, "nested", "dir", "f3.txt")))
        with open(os.path.join(self.repo_git, "nested", "dir", "f3.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "nested file 3 content")

        # 4. Metadata fidelity (author, email, date, message)
        log_lines = self.git_query(["log", "--reverse", "--date=iso", "--format=%an|%ae|%ad|%s"]).splitlines()
        self.assertEqual(len(log_lines), 3)
        self.assertEqual(log_lines[0], "Alice Smith|alice@test.org|2026-01-01 10:00:00 +0000|Commit 0: initial files")
        self.assertEqual(log_lines[1], "Bob Jones|bob@test.org|2026-01-02 11:00:00 +0000|Commit 1: modify file1, add file2")
        self.assertEqual(log_lines[2], "Charlie Brown|charlie@test.org|2026-01-03 12:00:00 +0000|Commit 2: delete file1, add nested file")

        # 5. Save state mapping file verification
        with open(hgtogit.SAVE_MAP_FILE, "r", encoding="utf-8") as f:
            save_map = json.load(f)
        self.assertEqual(len(save_map), 3)
        self.assertIn(c0, save_map)
        self.assertIn(c1, save_map)
        self.assertIn(c2, save_map)


# ==============================================================================
# Tier 2: Boundary and Corner Cases
# ==============================================================================

class TestTier2BoundaryAndCornerCases(BaseSyntheticTest):
    """
    Tier 2 verifies corner cases and edge conditions:
    - Empty commits created via --allow-empty.
    - Spaces and special characters in paths and filenames.
    - Angle brackets (< and >) in commit messages unescaped from Hg templates.
    - Fallback author email (`dev@localhost`) when missing from Hg author.
    - Two-parent merge DAG topologies with MERGE_HEAD creation.
    """

    def test_empty_commit(self):
        """Verify empty commits are recorded in Git using --allow-empty without error."""
        self.hg_commit("Commit 0: base", files={"base.txt": "base"})
        subprocess.run(["hg", "branch", "feat"], cwd=self.repo_hg, check=True)
        self.hg_commit("Commit 1: feat file", files={"feat.txt": "feat"})
        subprocess.run(["hg", "commit", "--close-branch", "-m", "Close branch feat", "-u", "Tester <t@test.org>"],
                       cwd=self.repo_hg, check=True)

        hgtogit.main()

        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "3")
        last_msg = self.git_query(["log", "-1", "--format=%s"])
        self.assertEqual(last_msg, "Close branch feat")
        # Tree hash matches parent tree hash
        tree_head = self.git_query(["rev-parse", "HEAD^{tree}"])
        tree_parent = self.git_query(["rev-parse", "HEAD~1^{tree}"])
        self.assertEqual(tree_head, tree_parent)

    def test_special_characters_and_spaces_in_filenames(self):
        """Verify filenames with whitespace and special symbols are staged and synchronized."""
        files = {
            "folder with spaces/file with spaces.txt": "spaced content",
            "special_#1@symbol.txt": "symbol content"
        }
        self.hg_commit("Commit with special filename paths", files=files)

        hgtogit.main()

        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "1")
        f1 = os.path.join(self.repo_git, "folder with spaces", "file with spaces.txt")
        f2 = os.path.join(self.repo_git, "special_#1@symbol.txt")
        self.assertTrue(os.path.exists(f1))
        self.assertTrue(os.path.exists(f2))
        with open(f1, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "spaced content")
        with open(f2, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "symbol content")

    def test_xml_escaping_in_commit_message(self):
        """Verify XML &lt; and &gt; entities are unescaped back to < and > in Git messages."""
        msg = "Fix bug <CORE-100> &lt;critical&gt; fix"
        self.hg_commit(msg, files={"a.txt": "a"})

        hgtogit.main()

        committed_msg = self.git_query(["log", "-1", "--format=%s"])
        self.assertEqual(committed_msg, "Fix bug <CORE-100> <critical> fix")

    def test_author_missing_email_fallback(self):
        """Verify missing author email correctly defaults to dev@localhost."""
        self.hg_commit("Commit without email in author", author="SoloDeveloper <>", files={"a.txt": "a"})

        hgtogit.main()

        author_meta = self.git_query(["log", "-1", "--format=%an|%ae"])
        self.assertEqual(author_meta, "SoloDeveloper|dev@localhost")

    def test_two_parent_merge_dag(self):
        """Verify 2-parent merge DAG topologies create true two-parent merge commits in Git."""
        c0 = self.hg_commit("C0: root", files={"root.txt": "root"})

        # Branch A: feat
        subprocess.run(["hg", "branch", "feat"], cwd=self.repo_hg, check=True)
        c1 = self.hg_commit("C1: feat file", files={"feat.txt": "feat"})

        # Branch B: default
        subprocess.run(["hg", "update", "default"], cwd=self.repo_hg, check=True)
        c2 = self.hg_commit("C2: main file", files={"main.txt": "main"})

        # Merge feat into default
        subprocess.run(["hg", "merge", "feat"], cwd=self.repo_hg, check=True)
        c3 = self.hg_commit("C3: merge feat into default")

        hgtogit.main()

        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "4")
        parents = self.git_query(["rev-list", "--parents", "-n", "1", "HEAD"]).split()
        self.assertEqual(len(parents), 3, "Merge commit in Git must have exactly 2 parents (3 hashes total)")
        head_sha, p1_sha, p2_sha = parents

        with open(hgtogit.SAVE_MAP_FILE, "r", encoding="utf-8") as f:
            save_map = json.load(f)

        self.assertEqual(p1_sha, save_map[c2])
        self.assertEqual(p2_sha, save_map[c1])
        self.assertEqual(head_sha, save_map[c3])

        self.assertTrue(os.path.exists(os.path.join(self.repo_git, "feat.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.repo_git, "main.txt")))


# ==============================================================================
# Tier 3: Submodule Mapping and Pointer Translation
# ==============================================================================

class TestTier3SubmoduleMapping(BaseSyntheticTest):
    """
    Tier 3 verifies Mercurial subrepo mapping to Git native submodules:
    - .hgsubstate parsing and fast-export mapping/marks lookup.
    - Initial submodule registration and gitlink checkout.
    - Submodule pointer transition between revisions.
    - Submodule deactivation when revision is set to null hash (0000...).
    """

    def test_submodule_lifecycle(self):
        # 1. Create upstream synthetic Git subrepository
        sub_upstream = os.path.join(self.test_root, "subrepo_1.git")
        os.makedirs(sub_upstream, exist_ok=True)
        subprocess.run(["git", "init", sub_upstream], check=True)
        subprocess.run(["git", "config", "user.name", "SubTester"], cwd=sub_upstream, check=True)
        subprocess.run(["git", "config", "user.email", "sub@test.org"], cwd=sub_upstream, check=True)

        with open(os.path.join(sub_upstream, "subfile.txt"), "w", encoding="utf-8") as f:
            f.write("sub v1")
        subprocess.run(["git", "add", "."], cwd=sub_upstream, check=True)
        subprocess.run(["git", "commit", "-m", "sub commit 1"], cwd=sub_upstream, check=True)
        git_sub_1 = subprocess.run(["git", "rev-parse", "HEAD"],
                                   cwd=sub_upstream, capture_output=True, text=True, check=True).stdout.strip()

        with open(os.path.join(sub_upstream, "subfile.txt"), "w", encoding="utf-8") as f:
            f.write("sub v2")
        subprocess.run(["git", "add", "."], cwd=sub_upstream, check=True)
        subprocess.run(["git", "commit", "-m", "sub commit 2"], cwd=sub_upstream, check=True)
        git_sub_2 = subprocess.run(["git", "rev-parse", "HEAD"],
                                   cwd=sub_upstream, capture_output=True, text=True, check=True).stdout.strip()

        # 2. Create subrepository inside Mercurial working tree
        hg_sub_dir = os.path.join(self.repo_hg, "subrepo_1")
        subprocess.run(["hg", "init", hg_sub_dir], check=True)
        with open(os.path.join(hg_sub_dir, "f.txt"), "w", encoding="utf-8") as f:
            f.write("sub v1")
        subprocess.run(["hg", "add", "f.txt"], cwd=hg_sub_dir, check=True)
        subprocess.run(["hg", "commit", "-m", "sub hg c0", "-u", "SubHg <sub@hg.org>"], cwd=hg_sub_dir, check=True)
        hg_sub_1 = subprocess.run(["hg", "log", "-r", ".", "-T", "{node}"],
                                  cwd=hg_sub_dir, capture_output=True, text=True, check=True).stdout.strip()

        map_file = os.path.join(sub_upstream, ".git", "hg2git-mapping")
        marks_file = os.path.join(sub_upstream, ".git", "hg2git-marks")

        # Hg Commit 0: Add submodule in root repository
        with open(os.path.join(self.repo_hg, ".hgsub"), "w", encoding="utf-8") as f:
            f.write("subrepo_1 = subrepo_1\n")
        subprocess.run(["hg", "add", ".hgsub"], cwd=self.repo_hg, check=False)
        c0 = self.hg_commit("C0: add submodule", files={"app.txt": "app v1"}, subrepos=True)

        # Hg Commit 1: Update subrepo commit pointer
        with open(os.path.join(hg_sub_dir, "f.txt"), "w", encoding="utf-8") as f:
            f.write("sub v2")
        subprocess.run(["hg", "commit", "-m", "sub hg c1", "-u", "SubHg <sub@hg.org>"], cwd=hg_sub_dir, check=True)
        hg_sub_2 = subprocess.run(["hg", "log", "-r", ".", "-T", "{node}"],
                                  cwd=hg_sub_dir, capture_output=True, text=True, check=True).stdout.strip()

        c1 = self.hg_commit("C1: update submodule pointer", subrepos=True)

        # Write fast-export mapping and marks
        with open(map_file, "w", encoding="utf-8") as f:
            f.write(f":{hg_sub_1} 1001\n")
            f.write(f":{hg_sub_2} 1002\n")

        with open(marks_file, "w", encoding="utf-8") as f:
            f.write(f":1001 {git_sub_1}\n")
            f.write(f":1002 {git_sub_2}\n")

        # Hg Commit 2: Deactivate submodule (hash 0000...)
        subprocess.run(["hg", "rm", ".hgsub"], cwd=self.repo_hg, check=True)
        with open(os.path.join(self.repo_hg, ".hgsubstate"), "w", encoding="utf-8") as f:
            f.write("0000000000000000000000000000000000000000 subrepo_1\n")
        c2 = self.hg_commit("C2: deactivate submodule")

        hgtogit.SUBREPOS = {
            "subrepo_1": {
                "git_path": sub_upstream,
                "git_repo_url": sub_upstream.replace("\\", "/") + "/",
                "mapping_file": map_file,
                "marks_file": marks_file
            }
        }

        hgtogit.main()

        # Check commit 0: gitlink points to git_sub_1
        ls_c0 = self.git_query(["ls-tree", "HEAD~2", "subrepo_1"])
        self.assertIn("160000 commit " + git_sub_1, ls_c0)

        # Check commit 1: gitlink points to git_sub_2
        ls_c1 = self.git_query(["ls-tree", "HEAD~1", "subrepo_1"])
        self.assertIn("160000 commit " + git_sub_2, ls_c1)

        # Check commit 2: submodule deactivated (removed from tree)
        ls_c2 = self.git_query(["ls-tree", "HEAD", "subrepo_1"])
        self.assertEqual(ls_c2, "", "Deactivated submodule must be removed from Git tree")


# ==============================================================================
# Tier 4: Save-Point and Resume Capability
# ==============================================================================

class TestTier4SavePointAndResume(BaseSyntheticTest):
    """
    Tier 4 verifies interrupted conversion and save-point continuity:
    - Step 1: Run commits 0..N, verify checkpoint state files.
    - Step 2: Resume conversion at N+1..M without wiping repository.
    - Verify seamless parent linking and commit graph continuity.
    """

    def test_savepoint_and_resume(self):
        nodes = []
        for i in range(6):
            n = self.hg_commit(f"Commit {i}", files={f"file_{i}.txt": f"content {i}"})
            nodes.append(n)

        # Step 1: Execute partial batch (commits 0 to 2)
        hgtogit.main(0, 2)

        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "3")
        with open(hgtogit.SAVE_MAP_FILE, "r", encoding="utf-8") as f:
            save_map_1 = json.load(f)
        self.assertEqual(len(save_map_1), 3)
        for i in range(3):
            self.assertIn(nodes[i], save_map_1)

        git_c2 = save_map_1[nodes[2]]

        # Step 2: Resume from commit 3 to commit 5
        hgtogit.main(3, 5)

        self.assertEqual(self.git_query(["rev-list", "--count", "HEAD"]), "6")
        with open(hgtogit.SAVE_MAP_FILE, "r", encoding="utf-8") as f:
            save_map_2 = json.load(f)
        self.assertEqual(len(save_map_2), 6)

        # Verify continuity: Git parent of commit 3 must be commit 2
        git_c3 = save_map_2[nodes[3]]
        parents_c3 = self.git_query(["rev-list", "--parents", "-n", "1", git_c3]).split()
        self.assertEqual(parents_c3[1], git_c2, "Commit 3 parent in Git must match Commit 2")

        # Verify all files exist in final working tree
        for i in range(6):
            file_path = os.path.join(self.repo_git, f"file_{i}.txt")
            self.assertTrue(os.path.exists(file_path))
            with open(file_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), f"content {i}")


# ==============================================================================
# Main Entry Point
# ==============================================================================

if __name__ == "__main__":
    unittest.main()
