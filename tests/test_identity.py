from __future__ import annotations

import hashlib
import pathlib
import subprocess
import tomllib
import unittest

import kilix_image_shop


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIRTH_PATHS = {
    ".gitignore",
    ".python-version",
    "CHANGELOG.md",
    "LICENSE",
    "Makefile",
    "NOTICE",
    "PUBLICATION.md",
    "README.md",
    "THIRD-PARTY-NOTICES.md",
    "VERSION",
    "pyproject.toml",
    "src/kilix_image_shop/__init__.py",
    "src/kilix_image_shop/py.typed",
    "tests/test_identity.py",
    "uv.lock",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = tomllib.loads((ROOT / "pyproject.toml").read_text())

    def test_version_identity(self) -> None:
        self.assertEqual((ROOT / "VERSION").read_text(), "0.2.1\n")
        self.assertEqual(self.config["project"]["version"], "0.2.1")
        self.assertEqual(kilix_image_shop.__version__, "0.2.1")

    def test_package_identity(self) -> None:
        project = self.config["project"]
        self.assertEqual(project["name"], "kilix-image-shop")
        self.assertEqual(project["requires-python"], ">=3.13,<3.14")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            self.config["dependency-groups"],
            {"build": ["hatchling==1.27.0"]},
        )
        self.assertEqual(
            project["authors"],
            [
                {
                    "name": "itsmygithubacct",
                    "email": "itsmygithubacct@users.noreply.github.com",
                }
            ],
        )

    def test_build_identity(self) -> None:
        self.assertEqual(
            self.config["build-system"],
            {
                "requires": ["hatchling==1.27.0"],
                "build-backend": "hatchling.build",
            },
        )
        self.assertEqual(self.config["tool"]["uv"]["required-version"], "==0.12.5")
        self.assertEqual((ROOT / ".python-version").read_text(), "3.13.5\n")

    def test_apache_license_is_unmodified(self) -> None:
        digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        )
        self.assertEqual(self.config["project"]["license"], "Apache-2.0")

    def test_legal_carrier_declarations(self) -> None:
        self.assertEqual(
            self.config["project"]["license-files"],
            ["LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"],
        )
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("Copyright 2026 itsmygithubacct", notice)

    def test_local_only_publication_boundary(self) -> None:
        publication = (ROOT / "PUBLICATION.md").read_text().lower()
        self.assertIn("local-only", publication)
        for action in ("remote", "push", "tag", "package publication", "release-pin"):
            self.assertIn(action, publication)
        self.assertEqual(git("remote"), "")

    def test_tracked_manifest_retains_the_exact_birth_surface(self) -> None:
        tracked = set(git("ls-files").splitlines())
        self.assertTrue(BIRTH_PATHS <= tracked)
        self.assertFalse(
            {
                path
                for path in tracked
                if path.startswith(("evidence/", "research/", "planning/"))
            }
        )

    def test_package_root_stays_adapter_free(self) -> None:
        package_files = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src" / "kilix_image_shop").iterdir()
            if path.is_file()
        }
        self.assertEqual(
            package_files,
            {
                "src/kilix_image_shop/__init__.py",
                "src/kilix_image_shop/py.typed",
            },
        )

    def test_no_private_workspace_paths(self) -> None:
        forbidden = ("/home/" + "pleb", "research/" + "gpu_terminal")
        for relative in git("ls-files").splitlines():
            data = (ROOT / relative).read_text(errors="ignore")
            for value in forbidden:
                self.assertNotIn(value, data, relative)

    def test_third_party_populations_are_explicit(self) -> None:
        ledger = (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
        for heading in (
            "Python build and test distributions",
            "Native lazy image-engine closure",
            "Codec, colour, and font dependencies",
            "Contract and model-operation carriers",
        ):
            self.assertIn(heading, ledger)


if __name__ == "__main__":
    unittest.main()
