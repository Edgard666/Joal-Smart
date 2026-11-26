"""CHANGELOG.md reader, shared by the « Nouveautés » window (v1.31) and the GitHub release notes."""
import re
from pathlib import Path

SECTION = re.compile(r'^## v(\d+(?:\.\d+){1,3})\s*(?:·\s*(.*))?$', re.M)


def version_key(version):
    return tuple(int(part) for part in str(version).split('.') if part.isdigit())


def parse(text):
    """[{'version', 'title', 'items': [text, ...]}], newest first, as written in the file."""
    sections = []
    matches = list(SECTION.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end]
        items, current = [], None
        for line in body.splitlines():
            if line.startswith('- '):
                current = line[2:].strip()
                items.append(current)
            elif current is not None and line.startswith('  ') and line.strip():
                items[-1] = items[-1] + ' ' + line.strip()
        sections.append({'version': match.group(1), 'title': (match.group(2) or '').strip(), 'items': items})
    return sections


def plain(text):
    """Markdown emphasis and code marks removed, for display as plain text (never HTML)."""
    return re.sub(r'\*\*(.+?)\*\*|`([^`]+)`', lambda m: m.group(1) or m.group(2), text)


def since(text, seen, current, limit=10):
    """Sections newer than `seen` and not newer than `current`, newest first."""
    low, high = version_key(seen or '0'), version_key(current)
    result = []
    for section in parse(text):
        key = version_key(section['version'])
        if low < key <= high:
            result.append({'version': section['version'], 'title': plain(section['title']),
                           'items': [plain(item) for item in section['items']]})
    return result[:limit]


def read(path):
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        return ''
