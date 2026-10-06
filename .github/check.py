"""Checks an upload's pull request from its file list alone.

It runs from main's copy of this file and reads the pull request through the API, never its
files on disk. An upload is one entry: every path under one `<kinds>/<id>/`, with that kind's
toml (GitHub itself refuses a file over 100 MB), and, when the entry already exists, the same author as
the index names. Merging stays with the registry's owner.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

KINDS = {"faces": "face.toml", "toolbelts": "toolbelt.toml", "bodies": "body.toml"}
REPO, PULL, AUTHOR = os.environ["GITHUB_REPOSITORY"], os.environ["PULL"], os.environ["AUTHOR"]


def api(path: str):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> None:
    files, page = [], 1
    while True:
        batch = api(f"/repos/{REPO}/pulls/{PULL}/files?per_page=100&page={page}")
        files += batch
        if len(batch) < 100:
            break
        page += 1
    # A rename touches the entry it came from as well as the one it lands in.
    roots = {tuple(name.split("/")[:2]) for f in files
             for name in (f["filename"], f.get("previous_filename")) if name}
    problems = []
    if len(roots) != 1:
        problems.append(f"an upload is one entry; this touches {sorted('/'.join(r) for r in roots)}")
    else:
        [(kinds, *rest)] = roots
        if not rest:
            sys.exit(f"{kinds} is not under faces/, toolbelts/ or bodies/")
        entry_id = rest[0]
        if kinds not in KINDS:
            problems.append(f"{kinds}/ is not faces/, toolbelts/ or bodies/")
        else:
            toml = f"{kinds}/{entry_id}/{KINDS[kinds]}"
            if not any(f["filename"] == toml and f["status"] != "removed" for f in files) \
                    and not Path(toml).is_file():
                problems.append(f"{toml} is missing")
        index = Path("index.json")
        if index.is_file():
            for entry in json.loads(index.read_text())["entries"]:
                if f"{entry['kind']}" == {"faces": "face", "toolbelts": "toolbelt",
                                          "bodies": "body"}.get(kinds) \
                        and entry["id"] == entry_id and entry["author"] != AUTHOR:
                    problems.append(f"{kinds}/{entry_id} is {entry['author']}'s")
    for f in files:
        if f["filename"].count("..") or f["filename"].startswith("/"):
            problems.append(f"{f['filename']} is not a plain path")
    if problems:
        sys.exit("\n".join(problems))
    print(f"one entry, {len(files)} files: ready for the owner to merge")


if __name__ == "__main__":
    main()
