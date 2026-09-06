#!/usr/bin/env python3
"""Build hook to inject git commit hash into the package."""

import subprocess
from pathlib import Path


def get_git_commit() -> str | None:
    """Get current git commit hash if available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main():
    """Generate _build_info.py with git commit hash."""
    commit = get_git_commit()
    
    build_info_path = Path(__file__).parent.parent / "src" / "repowiki" / "_build_info.py"
    
    content = f'''"""Build information - auto-generated during build."""

__commit__ = {repr(commit)}
'''
    
    build_info_path.write_text(content, encoding="utf-8")
    print(f"Generated _build_info.py with commit: {commit}")


if __name__ == "__main__":
    main()