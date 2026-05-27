#!/usr/bin/env python3
"""Remote code quality scanner — runs on the target machine via SSH.
Collects metrics on all code files, outputs JSON to stdout.
Only uses stdlib — no dependencies needed on remote.
"""
import os
import re
import json
import sys
import hashlib
from pathlib import Path
from collections import Counter

# ── Configuration ──────────────────────────────────────────────
SCAN_ROOT = os.path.expandvars(r"%USERPROFILE%")
MAX_FILE_BYTES = 500_000  # skip files larger than 500KB
EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".c", ".cpp", ".cxx", ".h", ".hpp", ".rs", ".go", ".sh", ".bash",
    ".java", ".rb", ".php", ".sql", ".swift", ".kt", ".m", ".mm",
}
EXCLUDE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "AppData", ".cache", "site-packages", "dist", "build",
    ".next", ".nuxt", ".turbo", "target", ".gradle", ".idea",
    "backups", "Corrupt_Backups", "DriveBackup", ".hermes",
    ".gemini", ".claude", ".antigravity", ".openclaw",
    ".resonance_staging", "drivers", "drivers_backup",
    "ASUS_RESONANCE", "Carved_Logic", "bwc_audio.wav",
}

# ── Security patterns to flag ──────────────────────────────────
SECURITY_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|apikey|secret|password|token|auth)\s*[:=]\s*["\'][^"\']{8,}["\']'), "hardcoded_secret"),
    (re.compile(r'(?i)(-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----)'), "private_key"),
    (re.compile(r'\beval\s*\(', re.IGNORECASE), "eval_usage"),
    (re.compile(r'\bexec\s*\(', re.IGNORECASE), "exec_usage"),
    (re.compile(r'os\.system\s*\(', re.IGNORECASE), "os_system"),
    (re.compile(r'subprocess\.(call|run|Popen)\s*\(\s*["\']', re.IGNORECASE), "subprocess_shell"),
]

# ── Quality anti-patterns ──────────────────────────────────────
QUALITY_PATTERNS = [
    (re.compile(r'except\s*:'), "bare_except"),
    (re.compile(r'except\s+Exception\s*:'), "catchall_except"),
    (re.compile(r'#\s*(TODO|FIXME|HACK|XXX|TEMP|KLUDGE)'), "debt_comment"),
    (re.compile(r'pass\s*$'), "bare_pass"),
    (re.compile(r'print\s*\(.*\)'), "debug_print"),  # flagged if >3 in file
    (re.compile(r'global\s+\w+'), "global_var"),
]


def should_scan(path: Path) -> bool:
    """Check if a file should be scanned."""
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return False
    if path.suffix.lower() not in EXTENSIONS:
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def line_length_issues(lines: list[str]) -> int:
    """Count lines longer than 120 characters."""
    return sum(1 for l in lines if len(l) > 120)


def cyclomatic_rough(lines: list[str]) -> int:
    """Estimate cyclomatic complexity by counting branch keywords."""
    branch_pattern = re.compile(
        r'^\s*(if|elif|else|for|while|case|catch|except|and|or|\|\||&&)\b'
    )
    return sum(1 for l in lines if branch_pattern.search(l))


def comment_ratio(lines: list[str]) -> float:
    """Fraction of lines that are comments or blank."""
    total = len(lines)
    if total == 0:
        return 0.0
    comment_or_blank = sum(
        1 for l in lines
        if l.strip() == ""
        or l.strip().startswith("#")
        or l.strip().startswith("//")
        or l.strip().startswith("/*")
        or l.strip().startswith("*")
        or l.strip().startswith("<!--")
    )
    return comment_or_blank / total


def duplication_score(lines: list[str]) -> float:
    """Estimate internal duplication by hashing normalized lines."""
    hashes = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 10:
            hashes.append(hashlib.md5(stripped.encode()).hexdigest())
    if len(hashes) < 10:
        return 0.0
    counts = Counter(hashes)
    dup_lines = sum(c - 1 for c in counts.values() if c > 1)
    return dup_lines / len(hashes)


def function_metrics(lines: list[str]) -> dict:
    """Count functions and find longest function (indentation-based heuristic)."""
    func_starts = re.compile(
        r'^\s*(def |function |async function |class |fn |func |public |private |protected |void |int |static )'
    )
    function_bodies = []
    current_body = 0
    in_function = False
    func_indent = 0

    for line in lines:
        stripped = line.strip()
        if func_starts.match(line) and not stripped.startswith("//") and not stripped.startswith("#"):
            if in_function:
                function_bodies.append(current_body)
            in_function = True
            current_body = 0
            func_indent = len(line) - len(line.lstrip())
            continue

        if in_function:
            indent = len(line) - len(line.lstrip())
            if stripped == "" or stripped.startswith("#") or stripped.startswith("//"):
                current_body += 1
                continue
            if indent <= func_indent and stripped != "":
                function_bodies.append(current_body)
                in_function = False
                current_body = 0
            else:
                current_body += 1

    if in_function and current_body > 0:
        function_bodies.append(current_body)

    return {
        "function_count": len(function_bodies),
        "max_function_lines": max(function_bodies) if function_bodies else 0,
        "avg_function_lines": sum(function_bodies) / len(function_bodies) if function_bodies else 0,
    }


def scan_file(filepath: Path) -> dict | None:
    """Analyze a single file. Returns metrics dict or None if skipped."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    lines = content.split("\n")
    size_bytes = len(content.encode("utf-8"))
    size_lines = len(lines)

    # Skip empty files
    if size_lines == 0:
        return None

    # Security flags
    security_flags = []
    for pattern, flag in SECURITY_PATTERNS:
        if pattern.search(content):
            security_flags.append(flag)

    # Quality flags
    quality_flags = []
    debug_print_count = 0
    for pattern, flag in QUALITY_PATTERNS:
        matches = pattern.findall(content)
        if flag == "debug_print":
            debug_print_count = len(matches)
            if debug_print_count > 3:
                quality_flags.append(f"debug_print({debug_print_count})")
        elif matches:
            quality_flags.append(flag)

    long_lines = line_length_issues(lines)
    complexity = cyclomatic_rough(lines)
    c_ratio = comment_ratio(lines)
    dup = duplication_score(lines)
    func = function_metrics(lines)

    # Composite "worse score" — higher = worse
    # Sub-scores normalized to [0, 10] range
    size_score = min(size_lines / 200.0, 10.0)  # 2000 lines = max penalty
    longline_score = min(long_lines / 20.0, 10.0)
    complexity_score = min(complexity / 50.0, 10.0)
    dup_score = min(dup * 40.0, 10.0)
    func_len_score = min(func["max_function_lines"] / 100.0, 10.0)
    security_score = len(security_flags) * 3.0
    quality_score = len(quality_flags) * 1.5
    comment_score = (1.0 - c_ratio) * 5.0 if c_ratio < 0.1 else 0  # too few comments
    comment_score += c_ratio * 5.0 if c_ratio > 0.6 else 0  # too many comments

    worse_score = (
        size_score * 0.15 +
        longline_score * 0.10 +
        complexity_score * 0.20 +
        dup_score * 0.15 +
        func_len_score * 0.10 +
        security_score * 0.15 +
        quality_score * 0.10 +
        comment_score * 0.05
    )

    return {
        "path": str(filepath),
        "language": filepath.suffix.lstrip("."),
        "size_lines": size_lines,
        "size_bytes": size_bytes,
        "long_lines": long_lines,
        "complexity": complexity,
        "comment_ratio": round(c_ratio, 3),
        "duplication_score": round(dup, 3),
        "functions": func["function_count"],
        "max_function_lines": func["max_function_lines"],
        "avg_function_lines": round(func["avg_function_lines"], 1),
        "security_flags": security_flags,
        "quality_flags": quality_flags,
        "worse_score": round(worse_score, 2),
        "sub_scores": {
            "size": round(size_score, 2),
            "long_lines": round(longline_score, 2),
            "complexity": round(complexity_score, 2),
            "duplication": round(dup_score, 2),
            "func_length": round(func_len_score, 2),
            "security": round(security_score, 2),
            "quality": round(quality_score, 2),
            "comments": round(comment_score, 2),
        },
    }


def walk_roots(roots: list[str]) -> list[dict]:
    """Walk one or more root directories and scan all code files."""
    results = []
    files_scanned = 0
    files_skipped = 0

    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            print(f"WARNING: {root} does not exist, skipping.", file=sys.stderr)
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            # Prune excluded directories
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

            for fname in filenames:
                filepath = Path(dirpath) / fname
                if not should_scan(filepath):
                    files_skipped += 1
                    continue

                metrics = scan_file(filepath)
                if metrics is None:
                    files_skipped += 1
                    continue

                results.append(metrics)
                files_scanned += 1

                if files_scanned % 200 == 0:
                    print(f"Scanned {files_scanned} files...", file=sys.stderr)

    print(f"\nScanned {files_scanned} files, skipped {files_skipped}.", file=sys.stderr)
    return results


def main():
    if len(sys.argv) > 1:
        roots = sys.argv[1:]
    else:
        # Default: scan common project directories + home dir top-level files
        home = os.path.expandvars(r"%USERPROFILE%")
        roots = [
            os.path.join(home, "Projects"),
            os.path.join(home, "Documents"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "ASUS_RESONANCE"),
            os.path.join(home, "Carved_Logic"),
            os.path.join(home, "collatz_checkpoints"),
            os.path.join(home, "lenticular_pull"),
            home,  # for top-level .py files
        ]
        # If scanning home, limit depth to 1
        EXCLUDE_DIRS.add("home_deep")  # will trigger depth limit

    results = walk_roots(roots)

    # For the home/ root, only keep files at depth 1 (top-level scripts)
    home_path = os.path.expandvars(r"%USERPROFILE%")
    results = [
        r for r in results
        if os.path.dirname(r["path"]) != home_path
        or r["path"].count(os.sep) == home_path.count(os.sep) + 1
    ]

    # Sort by worse_score descending
    results.sort(key=lambda x: x["worse_score"], reverse=True)

    output = {
        "scan_roots": roots,
        "files_scanned": len(results),
        "top_worst": results[:100],
        "all_scores": [{"path": r["path"], "worse_score": r["worse_score"]} for r in results],
    }

    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
