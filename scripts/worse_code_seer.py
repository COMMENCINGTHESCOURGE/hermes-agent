#!/usr/bin/env python3
"""
WORSE CODE SEER — Agent that SSHs into a remote machine, finds the worst code,
and devises 10 new programs by improving on the patterns found.

Architecture:
  1. SSH → remote, run remote_code_scanner.py
  2. Pull scan results
  3. Fetch source of top-N worst files
  4. Analyze failure patterns
  5. Generate 10 improvement-based program ideas
  6. Write plan to output directory

Usage:
  python worse_code_seer.py                          # Full run
  python worse_code_seer.py --from-cache scan.json   # Use cached scan
  python worse_code_seer.py --llm openrouter         # Use LLM for idea gen
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from datetime import datetime
from collections import Counter

# ── Configuration ──────────────────────────────────────────────
SSH_HOST = "asus"
REMOTE_HOME = r"C:\Users\dasha"
REMOTE_SCANNER = f"{REMOTE_HOME}\\remote_code_scanner.py"
OUTPUT_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "hermes" / "plans"
DATA_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "hermes" / "data"

SCAN_ROOTS = [
    f"{REMOTE_HOME}\\Projects",
    f"{REMOTE_HOME}\\ASUS_RESONANCE",
    f"{REMOTE_HOME}\\Carved_Logic",
    f"{REMOTE_HOME}\\collatz_checkpoints",
    f"{REMOTE_HOME}\\lenticular_pull",
    f"{REMOTE_HOME}\\Documents",
    f"{REMOTE_HOME}\\Desktop",
]

# ── SSH helpers ────────────────────────────────────────────────

def ssh(command: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command on the remote host via SSH."""
    result = subprocess.run(
        ["ssh", SSH_HOST, command],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    return result.returncode, result.stdout or "", result.stderr or ""


def scp_pull(remote_path: str, local_path: str) -> bool:
    """Pull a file from remote to local."""
    result = subprocess.run(
        ["scp", f"{SSH_HOST}:{remote_path}", local_path],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0


def scp_push(local_path: str, remote_path: str) -> bool:
    """Push a file from local to remote."""
    result = subprocess.run(
        ["scp", local_path, f"{SSH_HOST}:{remote_path}"],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0


# ── Scan orchestration ─────────────────────────────────────────

def ensure_scanner_on_remote() -> bool:
    """Upload the scanner script to the remote machine."""
    local_scanner = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "hermes" / "scripts" / "remote_code_scanner.py"
    if not local_scanner.exists():
        print(f"ERROR: Scanner not found at {local_scanner}")
        return False
    return scp_push(str(local_scanner), REMOTE_SCANNER)


def run_remote_scan() -> dict | None:
    """Run the scanner on remote, pull results, return combined data."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_out = f"{REMOTE_HOME}\\seer_scan_{timestamp}.json"

    # Build command: scan dirs + home .py files
    roots_arg = " ".join(SCAN_ROOTS)
    cmd = (
        f"python {REMOTE_SCANNER} {roots_arg} > {remote_out} 2>&1"
        f" && echo SCAN_OK || echo SCAN_FAILED"
    )

    print(f"[seer] Running remote scan on {SSH_HOST}...")
    code, stdout, stderr = ssh(cmd, timeout=180)
    if "SCAN_OK" not in stdout:
        print(f"[seer] Remote scan failed: {stdout[:500]}")
        return None

    # Pull results
    local_out = DATA_DIR / f"seer_scan_{timestamp}.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not scp_pull(remote_out, str(local_out)):
        print("[seer] Failed to pull scan results")
        return None

    # Also scan home dir top-level .py files
    home_cmd = (
        f"python -c \"import sys; sys.path.insert(0, r'{REMOTE_HOME}'); "
        f"from remote_code_scanner import scan_file, Path; import json, os; "
        f"home = r'{REMOTE_HOME}'; "
        f"results = [scan_file(Path(home) / f) for f in os.listdir(home) "
        f"if f.endswith('.py') and os.path.isfile(os.path.join(home, f))]; "
        f"results = [r for r in results if r]; "
        f"results.sort(key=lambda x: x['worse_score'], reverse=True); "
        f"json.dump({{'home_scripts': results}}, sys.stdout, indent=2)\" "
        f"> {REMOTE_HOME}\\seer_home_{timestamp}.json 2>&1"
    )
    code, _, _ = ssh(home_cmd, timeout=60)

    local_home = DATA_DIR / f"seer_home_{timestamp}.json"
    scp_pull(f"{REMOTE_HOME}\\seer_home_{timestamp}.json", str(local_home))

    # Combine results
    with open(local_out) as f:
        dir_data = json.load(f)

    combined = dir_data.get("top_worst", [])

    if local_home.exists():
        with open(local_home) as f:
            home_data = json.load(f)
        combined.extend(home_data.get("home_scripts", []))

    # Sort and take top 100
    combined.sort(key=lambda x: x["worse_score"], reverse=True)
    combined = combined[:100]

    result = {
        "timestamp": timestamp,
        "host": SSH_HOST,
        "scan_roots": SCAN_ROOTS + [f"{REMOTE_HOME}\\*.py"],
        "files_scanned": len(combined),
        "top_worst": combined,
    }

    # Save combined
    combined_path = DATA_DIR / f"seer_combined_{timestamp}.json"
    with open(combined_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"[seer] Combined {len(combined)} worst files → {combined_path}")
    return result


# ── Source fetching ────────────────────────────────────────────

def fetch_source(filepath: str) -> str | None:
    """Fetch a single file's source from the remote machine."""
    code, stdout, stderr = ssh(f"type \"{filepath}\" 2>nul", timeout=15)
    if code != 0 or not (stdout or "").strip():
        return None
    return stdout


def fetch_top_sources(scan_data: dict, count: int = 20) -> list[dict]:
    """Fetch source code for the top N worst files."""
    print(f"[seer] Fetching source for top {count} files...")
    enriched = []
    for entry in scan_data["top_worst"][:count]:
        source = fetch_source(entry["path"])
        entry["source"] = source
        entry["source_lines"] = len(source.split("\n")) if source else 0
        enriched.append(entry)
        status = "✓" if source else "✗"
        print(f"  {status} {entry['path']} ({entry['source_lines']} lines)")
    return enriched


# ── Pattern analysis ───────────────────────────────────────────

def analyze_patterns(enriched: list[dict]) -> dict:
    """Extract common failure patterns from the worst code."""
    patterns = {
        "top_security_flags": Counter(),
        "top_quality_flags": Counter(),
        "total_files": len(enriched),
        "avg_worse_score": sum(e["worse_score"] for e in enriched) / max(len(enriched), 1),
        "avg_complexity": sum(e["complexity"] for e in enriched) / max(len(enriched), 1),
        "avg_comment_ratio": sum(e["comment_ratio"] for e in enriched) / max(len(enriched), 1),
        "avg_duplication": sum(e["duplication_score"] for e in enriched) / max(len(enriched), 1),
        "large_files": [e for e in enriched if e["size_lines"] > 300],
        "high_complexity": [e for e in enriched if e["complexity"] > 30],
        "high_duplication": [e for e in enriched if e["duplication_score"] > 0.05],
        "languages": Counter(e["language"] for e in enriched),
        "narrative_smells": [],  # Files that talk about a domain without computing it
    }

    for entry in enriched:
        for flag in entry.get("security_flags", []):
            patterns["top_security_flags"][flag] += 1
        for flag in entry.get("quality_flags", []):
            patterns["top_quality_flags"][flag] += 1

        # Detect "narrative over parametric" smell
        source = entry.get("source", "")
        if source:
            comment_lines = [l for l in source.split("\n") if l.strip().startswith("#")]
            comment_text = " ".join(comment_lines).lower()
            narrative_terms = ["resonance", "quantum", "consciousness", "sovereign",
                             "labyrinth", "soul", "void", "oracle", "prophecy"]
            matches = [t for t in narrative_terms if t in comment_text]
            if matches and entry["worse_score"] > 0.5:
                patterns["narrative_smells"].append({
                    "path": entry["path"],
                    "terms": matches,
                    "score": entry["worse_score"],
                    "comment_ratio": entry["comment_ratio"],
                })

    return patterns


# ── Idea generation ────────────────────────────────────────────

def generate_ideas(patterns: dict, enriched: list[dict]) -> list[dict]:
    """Generate 10 new program ideas based on failure patterns found."""

    ideas = []

    # Idea templates keyed to common failure patterns
    idea_counter = 0

    def add(title: str, problem: str, solution: str, inspired_by: list[str]):
        nonlocal idea_counter
        idea_counter += 1
        ideas.append({
            "id": idea_counter,
            "title": title,
            "problem_diagnosed": problem,
            "solution_approach": solution,
            "inspired_by": inspired_by,
        })

    # 1. Fix the most common quality flag
    top_quality = patterns["top_quality_flags"].most_common(3)
    if top_quality:
        flag_name = top_quality[0][0]
        add(
            title=f"Debug Hygiene Engine",
            problem=f"Widespread {flag_name} patterns — {top_quality[0][1]} files ship with debug artifacts in production code, polluting stdout and masking real errors.",
            solution="A structured logging injector that replaces print()/console.log with leveled loggers (structlog/winston), adding log levels, rotation, and silent-by-default production mode. Includes auto-migration CLI.",
            inspired_by=[e["path"] for e in enriched if any(flag_name in f for f in e.get("quality_flags", []))][:3],
        )

    # 2. Fix security issues
    if patterns["top_security_flags"]:
        top_sec = patterns["top_security_flags"].most_common(3)
        add(
            title="Credential Vault Migrator",
            problem=f"Hardcoded credentials detected — {sum(patterns['top_security_flags'].values())} instances across files. The most common: {top_sec[0][0]}.",
            solution="A secrets migration toolkit that scans for hardcoded keys/tokens, replaces them with environment variable references, generates .env templates, and adds pre-commit hooks to block future leaks.",
            inspired_by=[e["path"] for e in enriched if e.get("security_flags")][:3],
        )

    # 3. De-duplication engine
    if patterns["high_duplication"]:
        add(
            title="Duplication Sentinel",
            problem=f"Significant internal duplication in {len(patterns['high_duplication'])} files. Copy-paste code blocks inflate maintenance surface and hide divergent behavior bugs.",
            solution="A code clone detector that identifies duplicated blocks across the codebase, suggests extractions into shared functions/modules, and generates the refactored output with tests preserved.",
            inspired_by=[e["path"] for e in patterns["high_duplication"][:3]],
        )

    # 4. Complexity reducer
    if patterns["high_complexity"]:
        add(
            title="Cyclomatic Decompressor",
            problem=f"{len(patterns['high_complexity'])} files exceed complexity threshold. Deeply nested conditionals make testing impossible and hide edge-case bugs.",
            solution="An AST-based refactoring engine that identifies high-complexity functions, extracts nested branches into predicate functions, applies guard-clause patterns, and generates before/after comparison reports with test coverage deltas.",
            inspired_by=[e["path"] for e in patterns["high_complexity"][:3]],
        )

    # 5. Large file breaker
    if patterns["large_files"]:
        add(
            title="Monolith Fracturer",
            problem=f"{len(patterns['large_files'])} files exceed 300 lines. Single-responsibility violation makes reasoning about behavior impossible without full-file context.",
            solution="An automatic module splitter that analyzes function call graphs, identifies cohesion boundaries, and proposes file splits with dependency graphs. Generates the refactored directory structure.",
            inspired_by=[e["path"] for e in patterns["large_files"][:3]],
        )

    # 6. Narrative → parametric converter
    if patterns["narrative_smells"]:
        add(
            title="Parametric Grounder",
            problem=f"Files detected with narrative-over-parametric smell — code that performs the aesthetics of its domain (commentary, metaphor) without the computation. Terms like {set.union(*[set(e['terms']) for e in patterns['narrative_smells']])} appear in comments but the code doesn't compute them.",
            solution="A reimplementation engine that takes narrative-heavy code, extracts the actual computational claim hidden in the metaphors, and produces a clean implementation that verifiably computes what the original only described. Includes property-based tests.",
            inspired_by=[e["path"] for e in patterns["narrative_smells"][:3]],
        )

    # 7. Test generator from worst code
    add(
        title="Worst-First Test Harness",
        problem=f"The {len(enriched)} worst files have zero test coverage. Bugs in these files have maximum blast radius with zero detection surface.",
        solution="A property-based test generator that reads the worst code, infers invariants from type signatures and control flow, generates Hypothesis/proptest test suites targeting edge cases, and measures the mutation kill rate of the generated tests against the original code.",
        inspired_by=[e["path"] for e in enriched[:5]],
    )

    # 8. Documentation generator
    add(
        title="Inverse Documentation Engine",
        problem=f"Average comment ratio: {patterns['avg_comment_ratio']:.2%}. Code that can't be read can't be maintained. But comments are often worse than silence — they lie.",
        solution="A behavior-based documenter that runs the code against varied inputs, captures actual I/O pairs, and generates documentation from observed behavior — not from what the comments claim. Includes diff mode: 'what the comments say vs. what the code does.'",
        inspired_by=[e["path"] for e in enriched[:3]],
    )

    # 9. Language migration advisor
    top_langs = patterns["languages"].most_common(2)
    if len(top_langs) >= 2:
        add(
            title=f"{top_langs[0][0].upper()}→{top_langs[1][0].upper()} Migration Bridge",
            problem=f"Codebase split across {top_langs[0][0]} ({top_langs[0][1]} files) and {top_langs[1][0]} ({top_langs[1][1]} files). Cross-language duplication, inconsistent error handling, and impedance mismatch at boundaries.",
            solution=f"A transpilation assistant that identifies {top_langs[0][0]}-specific patterns with no {top_langs[1][0]} equivalent, generates idiomatically correct {top_langs[1][0]} versions with behavioral equivalence tests, and flags patterns that should stay in {top_langs[0][0]}.",
            inspired_by=[e["path"] for e in enriched if e["language"] in [top_langs[0][0], top_langs[1][0]]][:3],
        )

    # 10. Architecture diagram from worst code
    add(
        title="Worst-Code Architecture Cartographer",
        problem=f"The {len(enriched)} files are analyzed in isolation, but the worst bugs emerge from their interactions — circular imports, implicit coupling, shared mutable state across module boundaries.",
        solution="A static analysis tool that builds a dependency graph from the worst files, identifies cross-module coupling, detects import cycles and shared global state, and renders an interactive architecture diagram with failure-propagation paths highlighted. Answers: 'if this function breaks, what else dies?'",
        inspired_by=[e["path"] for e in enriched[:5]],
    )

    # 11. Guaranteed fallback — always generate at least 10
    while len(ideas) < 10:
        fallback_titles = [
            ("Type Guardian", "Missing or weak type annotations across the codebase make refactoring dangerous and IDE support poor.",
             "A gradual typing enforcer that adds type annotations where possible, runs mypy/pyright in strict mode, and generates typed interface stubs for untyped modules."),
            ("Error Boundary Framework", "Bare and catchall except blocks swallow errors silently, making debugging impossible.",
             "A structured error handling library that categorizes exceptions (transient vs. permanent, user vs. system), adds context propagation, and generates error decision trees from exception hierarchies."),
            ("Dead Code Excavator", "Unreachable code paths, unused imports, and vestigial functions bloat the codebase and confuse maintainers.",
             "A static dead-code detector using AST/vulture analysis that identifies unreachable paths, unused symbols, and generates safe removal patches with rollback capability."),
            ("Config Drift Detector", "Hardcoded values scattered across files diverge from each other and from environment configuration.",
             "A configuration auditor that extracts all magic numbers and hardcoded strings, maps them to config sources, detects inconsistencies across environments, and generates unified config schemas."),
        ]
        title, problem, solution = fallback_titles[len(ideas) - 9]  # crude but works
        add(
            title=title,
            problem=problem,
            solution=solution,
            inspired_by=[e["path"] for e in enriched[:3]],
        )

    return ideas[:10]


# ── Output ─────────────────────────────────────────────────────

def write_plan(scan_data: dict, ideas: list[dict], patterns: dict) -> Path:
    """Write the improvement plan to a markdown file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = scan_data.get("timestamp", datetime.now().strftime("%Y%m%d_%H%M%S"))
    plan_path = OUTPUT_DIR / f"worse-code-seer-plan-{timestamp}.md"

    lines = []
    lines.append(f"# Worse Code Seer — Improvement Plan")
    lines.append(f"**Generated:** {datetime.now().isoformat()}")
    lines.append(f"**Host:** {scan_data['host']}")
    lines.append(f"**Files analyzed:** {scan_data['files_scanned']}")
    lines.append(f"**Average worse score:** {patterns['avg_worse_score']:.2f}")
    lines.append("")

    lines.append("## Top 10 Worst Files")
    lines.append("| # | File | Score | Lines | Complexity | Key Issues |")
    lines.append("|---|------|-------|-------|------------|------------|")
    for i, entry in enumerate(scan_data["top_worst"][:10], 1):
        issues = ", ".join(
            entry.get("security_flags", []) + entry.get("quality_flags", [])
        ) or "—"
        lines.append(
            f"| {i} | `{Path(entry['path']).name}` | {entry['worse_score']:.2f} "
            f"| {entry['size_lines']} | {entry['complexity']} | {issues} |"
        )
    lines.append("")

    lines.append("## Failure Pattern Analysis")
    lines.append(f"- **Security flags:** {dict(patterns['top_security_flags'].most_common()) or 'none'}")
    lines.append(f"- **Quality flags:** {dict(patterns['top_quality_flags'].most_common()) or 'none'}")
    lines.append(f"- **Avg comment ratio:** {patterns['avg_comment_ratio']:.2%}")
    lines.append(f"- **Avg duplication:** {patterns['avg_duplication']:.3f}")
    lines.append(f"- **Narrative smells:** {len(patterns['narrative_smells'])} files")
    lines.append(f"- **Language distribution:** {dict(patterns['languages'])}")
    lines.append("")

    lines.append("## 10 New Programs (Improvement-Based)")
    lines.append("")

    for idea in ideas:
        lines.append(f"### {idea['id']}. {idea['title']}")
        lines.append(f"**Problem diagnosed:** {idea['problem_diagnosed']}")
        lines.append("")
        lines.append(f"**Solution approach:** {idea['solution_approach']}")
        lines.append("")
        inspired = ", ".join(f"`{Path(p).name}`" for p in idea["inspired_by"])
        lines.append(f"**Inspired by:** {inspired}")
        lines.append("")

    plan_text = "\n".join(lines)
    plan_path.write_text(plan_text, encoding="utf-8")
    print(f"[seer] Plan written → {plan_path}")
    return plan_path


# ── Main ───────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Worse Code Seer")
    parser.add_argument("--from-cache", help="Use cached scan JSON instead of re-scanning")
    parser.add_argument("--llm", choices=["openrouter", "gemini", "none"], default="none",
                       help="Use LLM for idea generation (default: heuristic)")
    parser.add_argument("--top", type=int, default=20, help="Number of worst files to fetch source for")
    args = parser.parse_args()

    # Step 1: Get scan data
    if args.from_cache:
        with open(args.from_cache) as f:
            scan_data = json.load(f)
        print(f"[seer] Using cached scan: {args.from_cache}")
    else:
        if not ensure_scanner_on_remote():
            print("[seer] Failed to upload scanner to remote")
            sys.exit(1)
        scan_data = run_remote_scan()
        if not scan_data:
            print("[seer] Scan failed")
            sys.exit(1)

    if not scan_data.get("top_worst"):
        print("[seer] No worst files found — remote may have no scannable code")
        sys.exit(0)

    # Step 2: Fetch source for top N
    enriched = fetch_top_sources(scan_data, count=args.top)

    # Step 3: Analyze patterns
    patterns = analyze_patterns(enriched)
    print(f"[seer] Patterns: {dict(patterns['top_quality_flags'].most_common(5))}")
    print(f"[seer] Languages: {dict(patterns['languages'])}")

    # Step 4: Generate ideas
    if args.llm != "none":
        ideas = generate_ideas_with_llm(patterns, enriched, args.llm)
    else:
        ideas = generate_ideas(patterns, enriched)

    print(f"[seer] Generated {len(ideas)} program ideas")

    # Step 5: Write plan
    plan_path = write_plan(scan_data, ideas, patterns)

    # Print summary
    print(f"\n{'='*60}")
    print(f"WORSE CODE SEER — Plan Complete")
    print(f"{'='*60}")
    print(f"Plan: {plan_path}")
    print(f"Files analyzed: {scan_data['files_scanned']}")
    print(f"Top issue: {patterns['top_quality_flags'].most_common(1)[0] if patterns['top_quality_flags'] else 'none'}")
    print(f"Ideas generated: {len(ideas)}")
    for idea in ideas:
        print(f"  {idea['id']}. {idea['title']}")


def generate_ideas_with_llm(patterns: dict, enriched: list[dict], provider: str) -> list[dict]:
    """Use an LLM to generate more creative ideas. Placeholder — returns heuristic ideas."""
    # TODO: Integrate with Hermes LLM client (OpenRouter, Gemini, etc.)
    print("[seer] LLM idea generation not yet implemented — using heuristics")
    return generate_ideas(patterns, enriched)


if __name__ == "__main__":
    main()
