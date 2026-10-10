"""Adapt the pinned desktop client to Studio's authenticated session routes."""
from pathlib import Path
import sys


def prepare(root):
    root = Path(root)
    index = root / 'index.html'
    html = index.read_text(encoding='utf-8')
    if html.count('<head>') != 1:
        raise ValueError('unexpected pinned desktop HTML')
    replacements = {
        f'["./{name}","/{name}"],"{name}",{{timeoutMs:2e3,attempts:7}}':
        f'["./{name}"],"{name}",{{timeoutMs:8e3,attempts:3}}'
        for name in ('settings', 'turn')
    }
    assets = {path: path.read_text(encoding='utf-8') for path in (root / 'assets').glob('*.js')}
    # A changed upstream bundle must be reviewed rather than silently losing this
    # integration. Keep the client's AbortController and retry behavior intact.
    for old in replacements:
        if sum(text.count(old) for text in assets.values()) != 1:
            raise ValueError('unexpected pinned desktop configuration loader')
    for path, text in assets.items():
        updated = text
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding='utf-8')
    index.write_text(html.replace('<head>', '<head><script src="studio-embed.js"></script>'), encoding='utf-8')


if __name__ == '__main__':
    prepare(sys.argv[1])
