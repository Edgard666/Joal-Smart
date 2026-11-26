#!/usr/bin/env python3
"""Release notes for one published image: the CHANGELOG section of the app version
plus the JOAL / Java / profiles it was built with.

Usage: python3 ci/release_notes.py APP_VERSION JOAL_VERSION JRE_IMAGE PROFILES
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import changelog  # noqa: E402


def notes(text, app_version, joal_version, jre_image, profiles):
    section = next((s for s in changelog.parse(text) if s['version'] == app_version), None)
    lines = []
    if section:
        if section['title']:
            lines += [f'## {section["title"]}', '']
        lines += [f'- {item}' for item in section['items']]
    else:
        lines.append(f'Reconstruction de la version {app_version} (aucune entrée dans le journal).')
    lines += ['', '### Construite avec', f'- JOAL {joal_version}', f'- Java : `{jre_image}`',
              f'- {profiles} profils .client', '',
              'Mise à jour dans Unraid : onglet Docker → *Check for updates* → *update*.']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    app_version, joal_version, jre_image, profiles = sys.argv[1:5]
    sys.stdout.write(notes(changelog.read(ROOT / 'CHANGELOG.md'), app_version, joal_version, jre_image, profiles))
