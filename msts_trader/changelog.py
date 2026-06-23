"""Extract a version's section from CHANGELOG.md — for GitHub release notes.

Release tooling, not runtime. Lives in the package so it is import-testable
(the previous inline awk in release.yml was not, and shipped a silent bug:
it fed `## [0.25.0]` to awk's regex `~`, where `[0.25.0]` is a character
class, so it never matched and EVERY release fell back to the generic note).

Matching is on the literal heading prefix, so version brackets can never be
misread as a regex. If the section is absent we return the fallback string
(a release should still publish) rather than raising.
"""

from __future__ import annotations

FALLBACK = "See CHANGELOG.md for details."


def release_notes(changelog_text: str, version: str, *, fallback: str = FALLBACK) -> str:
    """Return the body of the ``## [<version>]`` section, stripped.

    ``version`` may include a leading ``v`` (tag form); it is stripped. Capture
    starts after the matching heading and ends at the next ``## [`` heading.
    Returns ``fallback`` when the section is missing or empty.
    """
    version = (version or "").lstrip("v").strip()
    if not version:
        return fallback
    heading = f"## [{version}]"
    out: list[str] = []
    capturing = False
    for line in changelog_text.splitlines():
        if not capturing:
            if line.startswith(heading):  # literal prefix — handles "## [0.25.0] — 2026-06-23"
                capturing = True
            continue
        if line.startswith("## ["):  # next version section
            break
        out.append(line)
    body = "\n".join(out).strip()
    return body or fallback


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m msts_trader.changelog <version> [CHANGELOG.md]``."""
    import sys
    from pathlib import Path

    args = list(sys.argv[1:] if argv is None else argv)
    version = args[0] if args else ""
    path = Path(args[1]) if len(args) > 1 else Path("CHANGELOG.md")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out = release_notes(text, version) + "\n"
    # Write UTF-8 bytes directly — the changelog has em-dashes/arrows that a
    # legacy console codepage (cp1252) can't encode, which would crash the
    # text stdout. Falls back to text write where buffer isn't available.
    try:
        sys.stdout.buffer.write(out.encode("utf-8"))
    except (AttributeError, ValueError):
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
