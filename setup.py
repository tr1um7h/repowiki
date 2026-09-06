#!/usr/bin/env python3
"""Custom build command that injects git commit hash."""

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist
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


def generate_build_info():
    """Generate _build_info.py with git commit hash."""
    commit = get_git_commit()
    
    build_info_path = Path("src/repowiki/_build_info.py")
    
    content = f'''"""Build information - auto-generated during build."""

__commit__ = {repr(commit)}
'''
    
    build_info_path.write_text(content, encoding="utf-8")
    print(f"Generated _build_info.py with commit: {commit}")


class CustomBuildPy(build_py):
    """Custom build command that injects git commit hash."""
    
    def run(self):
        generate_build_info()
        super().run()


class CustomSdist(sdist):
    """Custom sdist command that injects git commit hash."""
    
    def run(self):
        generate_build_info()
        super().run()


setup(
    cmdclass={
        "build_py": CustomBuildPy,
        "sdist": CustomSdist,
    },
)