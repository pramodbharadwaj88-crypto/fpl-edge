"""Fail if a credential-like string is in any tracked file.

Run in CI and before every commit. Patterns: GitHub tokens, OpenAI/Google
keys, cookie/session assignments, and bare 32-char lowercase tokens (the
shape of the Odds API key and the Fix sessionid).
"""
from __future__ import annotations
import pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATTERNS = [
    ("github token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("google key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("session assignment", re.compile(r"(sessionid|odds_api_key|api_key)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9]{16,}[\"']", re.I)),
    ("32-char token", re.compile(r"(?<![A-Za-z0-9/_.-])[a-z0-9]{32}(?![A-Za-z0-9/_.-])")),
]
SKIP_SUFFIX = {".png", ".jpg", ".gif", ".ico", ".pdf"}


def tracked() -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [ROOT / p for p in out.stdout.decode().split("\0") if p]


def scan(paths) -> list[str]:
    hits = []
    for f in paths:
        if f.suffix.lower() in SKIP_SUFFIX or not f.is_file():
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                tok = m.group(0)
                if label == "32-char token" and (tok.isdigit() or not re.search(r"\d", tok)
                                                 or not re.search(r"[a-z]", tok)):
                    continue          # all digits / all letters: ids and words, not keys
                line = text.count("\n", 0, m.start()) + 1
                where = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f
                hits.append(f"{where}:{line}: {label}")
    return hits


if __name__ == "__main__":
    paths = [pathlib.Path(p).resolve() for p in sys.argv[1:]] or tracked()
    hits = scan(paths)
    for h in hits:
        print("SECRET?", h)
    print(f"secret scan: {len(hits)} finding(s) in {len(paths)} files")
    sys.exit(1 if hits else 0)
