r"""
Guards for secret handling:

  * .env parsing (quotes, comments, export prefix, blank lines) and precedence
    (a real environment variable always wins over the file)
  * .env and token files are ignored by git, .env.example is committed and empty
  * no tracked file, in any commit, contains something that looks like a token

Run:  python tests/test_secrets.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import sources  # noqa: E402

SECRET_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{16,}"),            # Hugging Face
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),       # AWS access key id
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{20,}"),     # GitHub PAT
]


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def test_dotenv_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".env"
        p.write_text(
            "# a comment\n"
            "\n"
            "HF_TOKEN=hf_plain_value\n"
            'QUOTED="with spaces"\n'
            "SINGLE='single quoted'\n"
            "export EXPORTED=yes\n"
            "SPACED  =  padded \n"
            "EMPTY=\n"
            "not_a_pair\n"
            "URL=https://example.com/a=b\n",
            encoding="utf-8")
        for k in ("HF_TOKEN", "QUOTED", "SINGLE", "EXPORTED", "SPACED", "EMPTY", "URL", "PRESET"):
            os.environ.pop(k, None)
        os.environ["PRESET"] = "from-environment"
        Path(p.parent / "unused").write_text("", encoding="utf-8")
        values = sources.load_dotenv(p)
        assert values["HF_TOKEN"] == "hf_plain_value"
        assert values["QUOTED"] == "with spaces" and values["SINGLE"] == "single quoted"
        assert values["EXPORTED"] == "yes" and values["SPACED"] == "padded" and values["EMPTY"] == ""
        assert values["URL"] == "https://example.com/a=b", values["URL"]
        assert "not_a_pair" not in values and len(values) == 7, sorted(values)
        assert os.environ["HF_TOKEN"] == "hf_plain_value"
        # a real environment variable must win over the file
        p.write_text("PRESET=from-file\n", encoding="utf-8")
        sources.load_dotenv(p)
        assert os.environ["PRESET"] == "from-environment"
        # a missing file is not an error
        assert sources.load_dotenv(Path(tmp) / "nope.env") == {}
        for k in ("HF_TOKEN", "QUOTED", "SINGLE", "EXPORTED", "SPACED", "EMPTY", "URL", "PRESET"):
            os.environ.pop(k, None)
    print("dotenv parsing OK")


def test_gitignore():
    assert (ROOT / ".env.example").exists(), ".env.example must be committed as the template"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for pat in SECRET_PATTERNS:
        assert not pat.search(example), ".env.example must never contain a real secret"
    assert re.search(r"^HF_TOKEN=\s*$", example, re.M), ".env.example must list HF_TOKEN with an empty value"
    tracked = git("ls-files").splitlines()
    assert ".env.example" in tracked, ".env.example should be tracked"
    for name in (".env", ".env.local", "hf_token.txt", "secrets.token.txt"):
        assert name not in tracked, f"{name} must not be tracked"
        out = subprocess.run(["git", "check-ignore", name], cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, f"{name} is not covered by .gitignore"
    print("gitignore rules OK")


def test_no_secrets_in_history():
    """Every blob reachable from any commit, checked for token-shaped strings."""
    revs = git("rev-list", "--all").split()
    if not revs:
        print("no commits yet; skipping history scan")
        return
    hits = []
    for pat in SECRET_PATTERNS:
        out = subprocess.run(["git", "grep", "-I", "-n", "-E", pat.pattern, *revs],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if out:
            hits += [line for line in out.splitlines() if "test_secrets.py" not in line]
    assert not hits, "secret-looking strings found in git history:\n" + "\n".join(hits[:10])
    print(f"history scan OK ({len(revs)} commits, no secrets)")


if __name__ == "__main__":
    test_dotenv_parsing()
    test_gitignore()
    test_no_secrets_in_history()
    print("\nALL SECRET TESTS PASSED")
