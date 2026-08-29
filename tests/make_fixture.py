"""Build a throwaway repo shaped like an agent's branch.

Small single-purpose commits, one rename mid-branch, one delete, and one file
the agent rewrites three times — the shapes the track view has to survive.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)


def write(repo: Path, path: str, body: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=fixture@example.com", "-c", "user.name=Fixture",
        "commit", "-q", "-m", message)


def build(repo: Path) -> Path:
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")

    write(repo, "README.md", "# demo\n")
    write(repo, "src/app.py", "def main():\n    return 0\n")
    commit(repo, "initial commit")

    write(repo, "src/util.py", "def clamp(v, lo, hi):\n    return max(lo, min(v, hi))\n")
    commit(repo, "add util")

    git(repo, "checkout", "-q", "-b", "feature/auth")

    write(repo, "src/auth.py", "def login(user):\n    return None\n")
    commit(repo, "scaffold auth")

    write(repo, "src/auth.py",
          "import hashlib\n\n\ndef login(user, password):\n"
          "    digest = hashlib.sha256(password.encode()).hexdigest()\n"
          "    return digest == user.password_hash\n")
    commit(repo, "hash the password")

    write(repo, "tests/test_auth.py",
          "from src.auth import login\n\n\ndef test_login():\n    assert login is not None\n")
    commit(repo, "add auth test")

    # The agent decides the module was misnamed.
    git(repo, "mv", "src/auth.py", "src/authentication.py")
    commit(repo, "rename auth -> authentication")

    write(repo, "src/authentication.py",
          "import hashlib\nimport hmac\n\n\n"
          "def login(user, password):\n"
          "    digest = hashlib.sha256(password.encode()).hexdigest()\n"
          "    return hmac.compare_digest(digest, user.password_hash)\n\n\n"
          "def logout(session):\n    session.clear()\n    return True\n")
    commit(repo, "constant-time compare, add logout")

    write(repo, "src/app.py",
          "from src.authentication import login, logout\n\n\n"
          "def main():\n    return 0\n")
    commit(repo, "wire auth into app")

    (repo / "src/util.py").unlink()
    commit(repo, "drop unused util")

    write(repo, "src/authentication.py",
          "import hashlib\nimport hmac\nimport secrets\n\n\n"
          "def login(user, password):\n"
          "    digest = hashlib.sha256(password.encode()).hexdigest()\n"
          "    if not hmac.compare_digest(digest, user.password_hash):\n"
          "        return None\n"
          "    return secrets.token_urlsafe(32)\n\n\n"
          "def logout(session):\n    session.clear()\n    return True\n")
    write(repo, "tests/test_auth.py",
          "from src.authentication import login, logout\n\n\n"
          "def test_login_returns_token():\n    assert login is not None\n\n\n"
          "def test_logout():\n    assert logout is not None\n")
    commit(repo, "issue a session token on login")

    return repo


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/scrub-fixture")
    print(build(target))
