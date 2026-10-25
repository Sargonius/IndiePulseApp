"""IndiePulse.

__version__ is the one version of the app: the footer, /version, the updater (is a release newer than me?) and the
release workflow (the tag must be v + this) all read it. Kept as a plain string literal: tools/release.ps1 and
tools/build_release.py read and rewrite it as text.

- main says what comes next with -dev ("2.0.0-dev"): a checkout of main is never mistaken for a release;
- tools/release.ps1 2.0.0 commits "2.0.0" and tags that commit v2.0.0 (the release workflow builds the zip from it),
  then puts main on the next patch's -dev ("2.0.1-dev");
- order: 2.0.0-dev < 2.0.0-rc.1 < 2.0.0 < 2.0.1-dev (app/services/updates.py, parse_version).
"""
__version__ = "2.0.1-dev"
