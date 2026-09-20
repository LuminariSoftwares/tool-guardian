# @studio: Guardian | tool-guardian spill store -- archive of full tool results behind every lossy replacement
# @kind: library
# @called_by: tool_guardian.Router.shape + retrieve_spill | modules/tg_bridge.py op_ladder
"""py tg_spill.py --get ID [--start N] [--max N] [--grep REGEX] [--root DIR]
py tg_spill.py --selftest

The spill store: before a tool result is shortened lossily, the FULL original is archived
here, and the model can read it back in bounded windows or grep it. Ids are content hashes,
so saving the same text twice is a no-op.

History: library written by the local model through delegate_task (dlg_20260919_183235,
10/10 on the overseer's contract probe); the overseer removed unused imports and bare
excepts and recalibrated two selftest fixtures that contradicted the contract. MIT licensed.
"""

import os
import re
import json
import tempfile
import pathlib
import hashlib
import sys
import argparse


ID_RE = r"^sp_[0-9a-f]{12}$"


def default_root() -> pathlib.Path:
    # $TOOL_GUARDIAN_SPILL_DIR when set and non-empty, else  Path.home() / ".tool-guardian" / "spill"
    # Read the environment at CALL time, not at import time.
    spill_dir = os.environ.get("TOOL_GUARDIAN_SPILL_DIR", "")
    if spill_dir:
        return pathlib.Path(spill_dir)
    else:
        return pathlib.Path.home() / ".tool-guardian" / "spill"


def notice(spill_id: str, original_chars: int, shown_chars: int) -> str:
    # ONE line (no newline inside), exactly this shape:
    # [tool-guardian: showing {shown_chars} of {original_chars} chars. Full original archived as {spill_id} -- call retrieve_spill(id="{spill_id}") to read it, or add grep="<regex>" to search it.]
    return f"[tool-guardian: showing {shown_chars} of {original_chars} chars. Full original archived as {spill_id} -- call retrieve_spill(id=\"{spill_id}\") to read it, or add grep=\"<regex>\" to search it.]"


class SpillStore:
    def __init__(self, root: str | os.PathLike | None = None, keep: int = 500):
        # root None -> default_root(). The directory is created on first save(), not here.
        self.root = pathlib.Path(root) if root is not None else default_root()
        self.keep = keep
        self._created_dir = False

    def _ensure_dir(self):
        """Create root directory if needed"""
        if not self._created_dir:
            self.root.mkdir(parents=True, exist_ok=True)
            self._created_dir = True

    def save(self, text: str, tool: str = "", rule: str = "") -> dict:
        # id = "sp_" + first 12 hex chars of sha256(text.encode("utf-8"))
        # writes <root>/<id>.txt (the text, UTF-8, newline="" so bytes are preserved) and
        # <root>/<id>.json (sidecar: {"id","tool","rule","chars"}), each via a temp name + os.replace.
        # IDEMPOTENT: if <id>.txt already exists, write nothing and return the same dict.
        # returns {"id": id, "chars": len(text), "path": str(path of the .txt)}
        # After a save that wrote a NEW file, call self.prune().

        self._ensure_dir()
        
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        spill_id = "sp_" + text_hash[:12]
        
        txt_path = self.root / f"{spill_id}.txt"
        json_path = self.root / f"{spill_id}.json"
        
        # Check if files already exist
        if txt_path.exists():
            # File already exists, read the JSON to return the same dict
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {"id": spill_id, "chars": data["chars"], "path": str(txt_path)}

        # Create temporary files for atomic write
        txt_temp = txt_path.with_suffix(".txt.tmp")
        json_temp = json_path.with_suffix(".json.tmp")

        try:
            # Write text file
            with open(txt_temp, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            
            # Write JSON sidecar
            data = {
                "id": spill_id,
                "tool": tool,
                "rule": rule,
                "chars": len(text)
            }
            with open(json_temp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            # Atomic swap
            os.replace(txt_temp, txt_path)
            os.replace(json_temp, json_path)
            
            # Prune old files if needed
            self.prune()
            
            return {"id": spill_id, "chars": len(text), "path": str(txt_path)}
        
        except Exception:
            # Clean up temp files if something went wrong
            for leftover in (txt_temp, json_temp):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise

    def get(self, spill_id: str, start_line: int = 1, max_lines: int = 400, grep: str = "") -> dict:
        # Refuse (never raise) with {"ok": False, "error": "<why>"} when: spill_id does not match ID_RE
        # (this is the path-traversal guard -- test the id BEFORE building any path); the file does
        # not exist; `grep` is not a valid regex (the error text must contain the word "regex").
        # lines = text.splitlines(keepends=True); total_lines = len(lines)
        # Without grep: window = lines[start_line-1 : start_line-1+max_lines] (start_line < 1 -> 1;
        #   max_lines clamped to 1..2000). text = "".join(window).
        #   truncated = True when lines remain after the window.
        # With grep (re.IGNORECASE, re.search per line): every matching line, written as
        #   "L<1-based line number>: <line without its line ending>\n", at most max_lines of them;
        #   truncated = True when more matches existed than were returned.
        # returns {"ok": True, "id", "total_lines", "start_line", "returned_lines", "truncated", "text"}
        #   (start_line is echoed as used; with grep it is 1).

        # Validate spill_id
        if not re.match(ID_RE, spill_id):
            return {"ok": False, "error": f"Invalid spill ID '{spill_id}'"}

        txt_path = self.root / f"{spill_id}.txt"
        
        # Check if file exists
        if not txt_path.exists():
            return {"ok": False, "error": f"Spill file '{spill_id}' does not exist"}

        try:
            with open(txt_path, 'r', encoding='utf-8', newline='') as f:
                text = f.read()
        except Exception as e:
            return {"ok": False, "error": f"Error reading spill file: {str(e)}"}

        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        # Handle grep
        if grep:
            try:
                pattern = re.compile(grep, re.IGNORECASE)
            except Exception as e:
                return {"ok": False, "error": f"invalid regex {grep!r}: {e}"}
            
            # Find matching lines
            matches = []
            for i, line in enumerate(lines):
                if pattern.search(line):
                    matches.append("L%d: %s\n" % (i + 1, line.rstrip("\r\n")))
                    
            # Return at most max_lines matches
            returned_matches = matches[:max_lines]
            truncated = len(matches) > max_lines
            
            return {
                "ok": True,
                "id": spill_id,
                "total_lines": total_lines,
                "start_line": 1,
                "returned_lines": len(returned_matches),
                "truncated": truncated,
                "text": "".join(returned_matches)
            }
        else:
            # Handle window
            start_line = max(1, start_line)
            max_lines = max(1, min(2000, max_lines))
            
            end_pos = start_line + max_lines - 1
            window = lines[start_line-1:end_pos]
            truncated = end_pos < total_lines
            
            text_window = "".join(window)
            
            return {
                "ok": True,
                "id": spill_id,
                "total_lines": total_lines,
                "start_line": start_line,
                "returned_lines": len(window),
                "truncated": truncated,
                "text": text_window
            }

    def prune(self) -> int:
        # When more than `keep` .txt files exist, delete the OLDEST by st_mtime (each .txt together
        # with its .json) until `keep` remain. Returns the number of spills removed. Never raises.
        self._ensure_dir()
        
        # Get all txt files
        txt_files = list(self.root.glob("sp_*.txt"))
        
        if len(txt_files) <= self.keep:
            return 0
        
        # Sort by modification time (oldest first)
        txt_files.sort(key=lambda x: x.stat().st_mtime)
        
        # Remove oldest ones
        to_remove = txt_files[:-self.keep]  # Keep the latest `keep` files
        removed_count = 0
        
        for txt_file in to_remove:
            try:
                # Remove corresponding .json file
                json_file = txt_file.with_suffix(".json")
                if json_file.exists():
                    json_file.unlink()
                    
                # Remove txt file
                txt_file.unlink()
                
                removed_count += 1
            except Exception:
                # Continue removing others even if one fails
                continue
                
        return removed_count


def main():
    parser = argparse.ArgumentParser(description="Tool Guardian Spill Store")
    parser.add_argument("--get", help="Get spill by ID")
    parser.add_argument("--start", type=int, default=1, help="Start line for windowed read (default: 1)")
    parser.add_argument("--max", type=int, default=400, help="Max lines to return (default: 400)")
    parser.add_argument("--grep", help="Regex to grep in spill")
    parser.add_argument("--root", help="Root directory for spill store")
    
    # Check for selftest
    if "--selftest" in sys.argv:
        return run_selftest()
    
    args = parser.parse_args()
    
    if args.get:
        store = SpillStore(root=args.root)
        result = store.get(
            spill_id=args.get,
            start_line=args.start,
            max_lines=args.max,
            grep=args.grep
        )
        print(json.dumps(result))
        return 0
    else:
        parser.print_help()
        return 1


def run_selftest():
    checks = [
        ("save_id_is_sha256_prefix", save_id_is_sha256_prefix),
        ("save_twice_is_idempotent", save_twice_is_idempotent),
        ("sidecar_json_written", sidecar_json_written),
        ("get_window_exact_lines", get_window_exact_lines),
        ("get_default_window_is_400_and_truncated", get_default_window_is_400_and_truncated),
        ("grep_is_case_insensitive_with_line_numbers", grep_is_case_insensitive_with_line_numbers),
        ("bad_regex_refused_not_raised", bad_regex_refused_not_raised),
        ("bad_id_refused_before_path_is_built", bad_id_refused_before_path_is_built),
        ("missing_id_refused", missing_id_refused),
        ("prune_removes_oldest_pairs", prune_removes_oldest_pairs),
        ("default_root_reads_env_at_call_time", default_root_reads_env_at_call_time),
        ("notice_is_one_line", notice_is_one_line),
    ]
    
    passed = 0
    failed = 0
    
    for name, check_fn in checks:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                # Set the environment to control default_root behavior 
                os.environ["TOOL_GUARDIAN_SPILL_DIR"] = tmpdir
                result = check_fn(tmpdir)
                if result is True:
                    passed += 1
                else:
                    failed += 1
        except Exception as e:
            print(f"Error in {name}: {e}")
            failed += 1
    
    print(f"tg_spill selftest: {len(checks)} checks, {passed} passed, {failed} failed")
    
    if len(checks) == 0 or failed > 0:
        return 1
    return 0


def save_id_is_sha256_prefix(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "test content"
    result = store.save(text)
    
    # Check that ID is a sha256 prefix 
    assert re.match(ID_RE, result["id"]), f"ID does not match regex: {result['id']}"
    
    # Check that it matches expected hash
    expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    expected_id = "sp_" + expected_hash[:12]
    assert result["id"] == expected_id, f"ID mismatch. Expected: {expected_id}, got: {result['id']}"
    
    return True


def save_twice_is_idempotent(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "test content for idempotency test"
    
    # Save first time
    result1 = store.save(text)
    
    # Save second time 
    result2 = store.save(text)
    
    # Should return same dict
    assert result1 == result2, f"Results should be identical: {result1} != {result2}"
    
    # Check that file exists and contains right content  
    txt_path = pathlib.Path(tmpdir) / f"{result1['id']}.txt"
    assert txt_path.exists(), "File should exist"
    
    with open(txt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == text, "Content should match"
    
    return True


def sidecar_json_written(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "test content for json check"
    tool_name = "test_tool"
    rule_name = "test_rule"
    
    result = store.save(text, tool=tool_name, rule=rule_name)
    
    # Check that JSON file was written
    json_path = pathlib.Path(tmpdir) / f"{result['id']}.json"
    assert json_path.exists(), "JSON sidecar should exist"
    
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    expected_data = {
        "id": result["id"],
        "tool": tool_name,
        "rule": rule_name,
        "chars": len(text)
    }
    
    assert data == expected_data, f"JSON data mismatch: {data} != {expected_data}"
    
    return True


def get_window_exact_lines(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "line1\nline2\nline3\nline4\nline5"
    
    result = store.save(text)
    id = result["id"]
    
    # Get exact window
    window_result = store.get(id, start_line=2, max_lines=2)
    assert window_result["ok"] is True, "Get should succeed"
    assert window_result["start_line"] == 2, "Start line mismatch"
    assert window_result["returned_lines"] == 2, "Returned lines count mismatch"
    # lines 4-5 remain after the window, so by the contract this IS truncated
    assert window_result["truncated"] is True, "lines remain after the window"
    assert store.get(id, start_line=4, max_lines=2)["truncated"] is False, "the last window is not truncated"
    assert window_result["text"] == "line2\nline3\n", f"Text mismatch: {window_result['text']}"
    
    return True


def get_default_window_is_400_and_truncated(tmpdir):
    store = SpillStore(root=tmpdir)
    
    # Create text with 500 lines
    lines = [f"line{i}" for i in range(1, 501)]
    text = "\n".join(lines)
    
    result = store.save(text)
    id = result["id"]
    
    # Get window with default settings - should be truncated
    window_result = store.get(id)
    assert window_result["ok"] is True, "Get should succeed"
    assert window_result["start_line"] == 1, "Start line should default to 1"
    assert window_result["returned_lines"] == 400, "Should return 400 lines by default"
    assert window_result["truncated"] is True, "Should be truncated"
    assert len(window_result["text"]) > 0, "Should have content"
    
    return True


def grep_is_case_insensitive_with_line_numbers(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "Line with test\nAnother TEST line\nMixed Casing LiNe\nNo match"
    
    result = store.save(text)
    id = result["id"]
    
    # Grep for "test"
    grep_result = store.get(id, grep="test")
    assert grep_result["ok"] is True, "Get should succeed"
    assert grep_result["truncated"] is False, "Should not be truncated"
    
    # Should contain line numbers
    expected_lines = ["L1: Line with test\n", "L2: Another TEST line\n"]
    actual_lines = grep_result["text"].splitlines(keepends=True)
    
    # Check both lines are present (order might vary due to case-insensitive search)
    assert len(actual_lines) == 2, f"Should find 2 matches, got {len(actual_lines)}"
    for expected_line in expected_lines:
        assert any(expected_line in actual_line for actual_line in actual_lines), \
            f"Expected '{expected_line}' not found in results"
    
    return True


def bad_regex_refused_not_raised(tmpdir):
    store = SpillStore(root=tmpdir)
    text = "test content"
    result = store.save(text)
    id = result["id"]
    
    # Try with invalid regex
    grep_result = store.get(id, grep="[")

    assert grep_result["ok"] is False, "Should return error for bad regex"
    assert "regex" in grep_result["error"].lower(), "Error should contain word 'regex'"
    
    return True


def bad_id_refused_before_path_is_built(tmpdir):
    store = SpillStore(root=tmpdir)
    
    # Try with invalid ID - should not even try to build path
    result = store.get("invalid_id")
    assert result["ok"] is False, "Should refuse invalid ID"
    assert "Invalid spill ID" in result["error"], "Should have correct error message"
    
    return True


def missing_id_refused(tmpdir):
    store = SpillStore(root=tmpdir)
    
    # Try with non-existent ID
    result = store.get("sp_1234567890ab")
    assert result["ok"] is False, "Should refuse missing ID"
    assert "does not exist" in result["error"], "Should have correct error message"
    
    return True


def prune_removes_oldest_pairs(tmpdir):
    # save() prunes after every new file, so build the backlog under a roomy store and
    # prune through a SECOND store over the same directory (overseer fixture, 2026-09-19).
    roomy = SpillStore(root=tmpdir, keep=100)
    ids = []
    for n in range(5):
        saved = roomy.save("content %d" % n)
        ids.append(saved["id"])
        os.utime(saved["path"], (1000 + n, 1000 + n))
    removed = SpillStore(root=tmpdir, keep=2).prune()
    assert removed == 3, f"Should remove 3 spills, got {removed}"
    left = sorted(p.stem for p in pathlib.Path(tmpdir).glob("sp_*.txt"))
    assert left == sorted(ids[3:]), "the two NEWEST must survive"
    assert len(list(pathlib.Path(tmpdir).glob("sp_*.json"))) == 2, "each .txt goes with its .json"
    return True


def default_root_reads_env_at_call_time(tmpdir):
    # Set environment and call the function
    old_spill_dir = os.environ.get("TOOL_GUARDIAN_SPILL_DIR", "")
    try:
        os.environ["TOOL_GUARDIAN_SPILL_DIR"] = tmpdir 
        root = default_root()
        assert str(root) == tmpdir, f"Should return env var value: {root} != {tmpdir}"
    finally:
        # Restore
        if old_spill_dir:
            os.environ["TOOL_GUARDIAN_SPILL_DIR"] = old_spill_dir
        else:
            os.environ.pop("TOOL_GUARDIAN_SPILL_DIR", None)
    return True


def notice_is_one_line(tmpdir):
    notice_str = notice("sp_1234567890ab", 1000, 500)
    
    # Should be exactly one line with no newline characters inside  
    assert "\n" not in notice_str, "Notice should be single line"
    assert notice_str.startswith("[tool-guardian:"), f"Notice should start properly: {notice_str}"
    
    return True


if __name__ == "__main__":
    exit(main())