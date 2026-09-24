"""Tags the wheel platform-specific but Python-agnostic.

Two things are wrong with the wheel setuptools would produce from pyproject.toml alone, and
neither can be expressed there.

It would be tagged ``py3-none-any``. The package carries a compiled linux-x64 library, so such a
wheel installs happily on Windows and macOS and then fails at import — a worse failure than pip
refusing it outright.

The obvious fix, declaring the distribution impure, over-corrects: setuptools then assumes a
CPython extension and tags the wheel ``cp314-cp314``, pinning it to one interpreter version. That
is wrong here. The library is reached through ``ctypes``, whose ABI is stable across CPython
versions, so one wheel serves every supported Python 3.

What is wanted is ``py3-none-<platform>``, which is what this produces. The platform tag itself
comes from the build command, because the glibc floor is a property of the image the library was
compiled in rather than of this file — see build/Dockerfile.
"""

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel


class PlatformWheel(bdist_wheel):
    """A wheel that is platform-specific yet runs on any Python 3."""

    def finalize_options(self) -> None:
        super().finalize_options()
        # Carries a binary, so it gets a platform tag rather than "any".
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _python, _abi, platform = super().get_tag()
        # ctypes is ABI-stable, so neither the interpreter nor its ABI belongs in the tag.
        return "py3", "none", platform


setup(cmdclass={"bdist_wheel": PlatformWheel})
