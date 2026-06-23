"""Release-notes extraction (msts_trader.changelog).

Guards the bug that made every GitHub release fall back to a generic note: the
old inline awk treated the version heading `## [0.25.0]` as a regex (brackets =
character class) so it never matched. These lock in literal-heading extraction.
"""
from __future__ import annotations

from msts_trader.changelog import FALLBACK, release_notes

SAMPLE = """# Changelog

## [Unreleased]

## [0.25.0] — 2026-06-23

### Added
- liquidate command.

### Fixed
- MOC double-execution.

## [0.24.4] — 2026-06-22

### Fixed
- paper-reset cash.
"""


def test_extracts_the_version_section_with_bracketed_heading():
    # The exact failure mode: brackets in the version must be matched literally.
    notes = release_notes(SAMPLE, "0.25.0")
    assert "liquidate command." in notes
    assert "MOC double-execution." in notes
    assert "paper-reset cash." not in notes  # stops at the next section
    assert notes != FALLBACK and notes.strip() != ""


def test_tag_form_with_leading_v():
    assert release_notes(SAMPLE, "v0.25.0") == release_notes(SAMPLE, "0.25.0")


def test_missing_version_returns_fallback():
    assert release_notes(SAMPLE, "9.9.9") == FALLBACK


def test_empty_or_blank_version_returns_fallback():
    assert release_notes(SAMPLE, "") == FALLBACK


def test_last_section_captured_to_eof():
    notes = release_notes(SAMPLE, "0.24.4")
    assert "paper-reset cash." in notes


def test_every_version_heading_has_a_link_definition():
    # Keep-a-Changelog: every `## [x.y.z]` heading must have a matching
    # `[x.y.z]: <url>` link-reference definition (else the heading renders as
    # plain text). The footer had silently drifted — stale since 0.16.2 — so
    # 0.17.0..0.25.1 weren't clickable. This guard fails if any heading lacks one.
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE))
    defs = set(re.findall(r"^\[(\d+\.\d+\.\d+)\]:\s+http", text, re.MULTILINE))
    missing = sorted(headings - defs)
    assert not missing, f"CHANGELOG.md version headings without a link definition: {missing}"
    assert re.search(r"^\[Unreleased\]:\s+http", text, re.MULTILINE), "missing [Unreleased] link definition"


def test_real_changelog_current_version_is_nonempty():
    # The repo's own CHANGELOG must yield real notes for the current version —
    # this is the regression guard that would have caught the silent fallback.
    from pathlib import Path

    from msts_trader import __version__

    root = Path(__file__).resolve().parent.parent
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = release_notes(text, __version__)
    assert notes != FALLBACK, f"CHANGELOG.md has no '## [{__version__}]' section — release notes would be generic"
    assert notes.strip() != ""
