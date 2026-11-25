"""Helpers to build throwaway fixture repos OUTSIDE any git tree."""
from __future__ import annotations

import tempfile
from pathlib import Path


def make_repo(files: dict[str, str]) -> Path:
    """Create a temp dir with the given {relative_path: content} files."""
    root = Path(tempfile.mkdtemp(prefix="onboarding_fix_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


NODE_REPO = {
    "README.md": "# Acme Web\n\nAcme Web is the customer portal. It lets users manage orders and billing.\n",
    "package.json": '{\n  "name": "acme-web",\n  "scripts": {"start": "vite"}\n}\n',
    "src/app.js": "console.log('hi');\n",
    "src/utils/format.js": "export const f = x => x;\n",
}
