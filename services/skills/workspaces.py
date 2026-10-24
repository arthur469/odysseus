"""Temporary fixtures for manual skill tests; never a source of tool grants."""
import os
from pathlib import Path
import stat
import tempfile


def create_test_workspace(skill_path=None):
    """Stage bounded, regular bundle resources without executing any of them.

    Keep the TemporaryDirectory alive in the transient test job, including
    approval pauses. A retry or server shutdown releases it. Linked files and
    junctions are excluded so a bundle cannot copy unrelated local data.
    """
    workspace = tempfile.TemporaryDirectory(prefix="odysseus-skill-test-", ignore_cleanup_errors=True)
    try:
        if not skill_path:
            return workspace
        source = Path(skill_path).parent
        if source.is_symlink() or source.is_junction():
            raise ValueError("Linked skill directories cannot be used as test fixtures")
        source_root = source.resolve(strict=True)
        total = count = 0
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()
                       and not (Path(root) / d).is_junction()]
            for name in files:
                path = Path(root) / name
                info = path.stat(follow_symlinks=False)
                if name == "SKILL.md" or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    continue
                count += 1
                total += info.st_size
                if count > 200 or total > 2 * 1024 * 1024:
                    raise ValueError("Skill test resources exceed the fixture size limit")
                if not path.resolve(strict=True).is_relative_to(source_root):
                    raise ValueError("Skill resource is outside its bundle")
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                            or not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1):
                        raise ValueError("Skill resource identity changed while staging fixtures")
                    content = stream.read(2 * 1024 * 1024 + 1)
                if len(content) != info.st_size:
                    raise ValueError("Skill resource changed while staging fixtures")
                target = Path(workspace.name) / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        return workspace
    except BaseException:
        workspace.cleanup()
        raise
