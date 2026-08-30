import unittest

from tools.release_notes import release_notes


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_requested_version(self):
        changelog = """# Changelog

## 1.0.1 - 2026-08-30

### Fixed

- Short fix.

## 1.0.0 - 2026-08-26

- First release.
"""

        notes = release_notes(changelog, "v1.0.1")

        self.assertIn("Short fix.", notes)
        self.assertNotIn("First release.", notes)

    def test_requires_matching_changelog_section(self):
        with self.assertRaisesRegex(ValueError, "no section"):
            release_notes("# Changelog\n", "v2.0.0")


if __name__ == "__main__":
    unittest.main()
