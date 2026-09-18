"""Public-export hygiene gate (prodocux-labs allowlist style).

Tracked files must be on the file allowlist. Operator gitignored files
(AGENTS.local.md, fsf-executors.json, .env) are out of scope unless committed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exact files. Everything else at the repo root or under docs/compatibility
# is extra unless listed here. Matches labs INCLUDE_FILES, not a folder dump.
PUBLIC_FILES = {
    ".gitattributes",
    ".gitignore",
    ".github/workflows/release.yml",
    ".github/workflows/validate.yml",
    "AGENTS.md",
    "AGENTS.local.example.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "compatibility/pdx_conformance_freeze_v1.json",
    "compatibility/pdx_contract_seal_input_v1.json",
    "compatibility/pdx_official_pin_rc7_a8_a3.json",
    "docs/DATA_GOVERNANCE.md",
    "docs/FSF_LIVE_VERIFY.md",
    "docs/RELEASE.md",
    "fsf-executors.example.json",
    "pyproject.toml",
}

# Whole trees that may contain any file except denied names/suffixes.
PUBLIC_DIR_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
)

DENIED_NAMES = {
    ".env",
    "AGENTS.local.md",
    "credentials.json",
    "fsf-executors.json",
    "id_ed25519",
    "id_rsa",
    "kaggle.json",
    "secrets.yaml",
    "secrets.yml",
}

DENIED_DOC_NAMES = {
    "AGENT_SYNC.md",
    "AI_DEVELOPMENT_LOG.md",
    "DEMO_SCRIPT.md",
    "DEVPOST_SUBMISSION.md",
    "FREE_STUDIO_FLOW_INTEGRATION_PLAN.md",
    "FREE_STUDIO_FLOW_INTEGRATION_PLAN_V2.en.md",
    "FREE_STUDIO_FLOW_INTEGRATION_PLAN_V2.md",
    "FREE_STUDIO_FLOW_INTEGRATION_PLAN_V3.en.md",
    "FREE_STUDIO_FLOW_INTEGRATION_PLAN_V3.md",
    "FSF_M1_IMPLEMENTATION.md",
    "FSF_M1_M4_IMPLEMENTATION.md",
    "FSF_S_P0_IMPLEMENTATION.md",
    "FSF_S_P1_IMPLEMENTATION.md",
    "FSF_S_P2_P3_IMPLEMENTATION.md",
    "JUDGING_GUIDE.md",
    "MVP_DEVELOPMENT_PLAN.md",
    "PDX_CANDIDATE_DF33F0C.md",
    "PDX_CANDIDATE_INTEGRATION.md",
    "PDX_CANDIDATE_RC7_INTEGRATION.md",
    "PDX_CONFORMANCE_FREEZE.md",
    "PDX_CONFORMANCE_PROPOSAL_REVIEW.en.md",
    "PDX_CONFORMANCE_PROPOSAL_REVIEW.md",
    "PDX_CONTRACT_SEAL.md",
    "PDX_HANDOFF.md",
    "PDX_OFFICIAL_PIN_RC7_A8.md",
    "PDX_PUBLISH_WAIT.md",
    "PHASE0_DECISIONS.md",
    "PHASE1_STATUS.md",
    "PHASE3_STATUS.md",
    "PITCH_DECK_PLAN.md",
    "PRODUCT_DECISIONS.md",
    "SECURITY_CANDIDATE_EVIDENCE.md",
    "SECURITY_OS_ACCEPTANCE.md",
    "SECURITY_RUNTIME.md",
}

DENIED_SUFFIXES = {
    ".db",
    ".kdbx",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyd",
    ".sqlite",
    ".sqlite3",
}

MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

ABS_PATH = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:(?:\\+|/)(?:Users|FreeStudioFlow|ProDocuX|Kaggle-Projects|Codex-Projects)\b",
    re.I,
)
CJK = re.compile(
    "["
    "\u1100-\u11FF"  # Hangul Jamo
    "\u2E80-\u2FD5"  # CJK radicals
    "\u3000-\u303F"  # CJK punctuation
    "\u3040-\u309F"  # Hiragana
    "\u30A0-\u30FF"  # Katakana
    "\u3100-\u318F"  # Bopomofo + Hangul compatibility jamo
    "\u3190-\u31FF"  # Kanbun, bopomofo ext, CJK strokes, kana ext
    "\u3200-\u32FF"  # Enclosed CJK
    "\u3300-\u33FF"  # CJK compatibility
    "\u3400-\u4DBF"  # CJK Unified Ideographs Extension A
    "\u4E00-\u9FFF"  # CJK Unified Ideographs
    "\uA960-\uA97F"  # Hangul Jamo Extended-A
    "\uAC00-\uD7AF"  # Hangul syllables
    "\uF900-\uFAFF"  # CJK compatibility ideographs
    "\uFE10-\uFE1F"  # vertical forms
    "\uFE30-\uFE4F"  # CJK compatibility forms
    "\uFF65-\uFF9F"  # halfwidth katakana
    "\U00020000-\U0002A6DF"  # Extension B
    "\U0002A700-\U0002B73F"  # Extension C
    "\U0002B740-\U0002B81F"  # Extension D
    "\U0002B820-\U0002CEAF"  # Extension E
    "\U0002CEB0-\U0002EBEF"  # Extension F
    "\U0002EBF0-\U0002EE5F"  # Extension I
    "\U0002F800-\U0002FA1F"  # compatibility ideographs supplement
    "\U00030000-\U0003134F"  # Extension G
    "\U00031350-\U000323AF"  # Extension H
    "]"
)

SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z])hf_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
)

ENV_EXAMPLE_NAME = ".env.example"


def is_denied_filename(name: str) -> bool:
    """Deny secrets and internal docs by basename, including any .env / .env.*."""
    if name == ENV_EXAMPLE_NAME:
        return False
    if name in DENIED_NAMES or name in DENIED_DOC_NAMES:
        return True
    return name.startswith(".env.")


def is_allowlisted(rel_s: str) -> bool:
    norm = rel_s.replace("\\", "/")
    if norm in PUBLIC_FILES:
        return True
    return any(norm.startswith(prefix) for prefix in PUBLIC_DIR_PREFIXES)


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    return _is_reparse_point(path)


def inspect_entry(rel_s: str, path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> list[str]:
    """Inspect one path. Does not follow broken links; reports them as hits."""
    hits: list[str] = []
    if is_denied_filename(path.name):
        hits.append(f"{rel_s}: denied filename")
        return hits
    if path.suffix.lower() in DENIED_SUFFIXES:
        hits.append(f"{rel_s}: denied suffix {path.suffix}")
        return hits
    if _is_link(path):
        hits.append(f"{rel_s}: symlink or junction is not allowed in the public tree")
        return hits
    if not path.exists():
        hits.append(f"{rel_s}: tracked path is missing")
        return hits
    if not path.is_file():
        return hits
    try:
        size = path.stat().st_size
    except OSError as exc:
        hits.append(f"{rel_s}: unreadable ({exc})")
        return hits
    if size > max_bytes:
        hits.append(f"{rel_s}: file larger than {max_bytes} bytes")
        return hits
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() in {".md", ".txt"} and CJK.search(text):
        hits.append(f"{rel_s}: CJK text is not allowed in public docs")
    for index, line in enumerate(text.splitlines(), 1):
        if ABS_PATH.search(line):
            hits.append(f"{rel_s}:{index}: workstation-absolute path")
        for pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"{rel_s}:{index}: secret-like token pattern")
                break
    return hits


def tracked_files(root: Path | None = None) -> list[Path]:
    base = root or ROOT
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=base,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("git ls-files failed; hygiene scans the tracked public tree")
    names = [item.decode("utf-8", errors="replace") for item in completed.stdout.split(b"\0") if item]
    return [base / name for name in names]


def scan_public_tree(root: Path | None = None) -> list[str]:
    """Return human-readable hit strings. Empty means clean."""
    base = root or ROOT
    hits: list[str] = []
    files = tracked_files(base)
    tracked = {path.relative_to(base).as_posix() for path in files}
    for required in sorted(PUBLIC_FILES):
        if required not in tracked:
            hits.append(f"{required}: allowlisted path is missing")
    for path in files:
        rel = path.relative_to(base).as_posix()
        if not is_allowlisted(rel):
            hits.append(f"{rel}: not on the public allowlist")
            continue
        hits.extend(inspect_entry(rel, path))
    return hits


def main() -> int:
    if "--list" in sys.argv:
        files = tracked_files()
        rels = sorted(
            path.relative_to(ROOT).as_posix()
            for path in files
            if is_allowlisted(path.relative_to(ROOT).as_posix())
        )
        print("\n".join(rels))
        return 0
    try:
        hits = scan_public_tree()
    except RuntimeError as exc:
        print(f"public hygiene scan failed: {exc}", file=sys.stderr)
        return 1
    if hits:
        print("public hygiene scan failed:", file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    print("public hygiene scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
