"""Publishes the registry: each entry whose files changed becomes a new
Release asset, and `index.json` is rebuilt with every entry's author and download count.

Runs in this repo's workflow with its GITHUB_TOKEN. An entry is `<kinds>/<id>/`; its asset is
its files at the tarball's root, packed so the same files always give the same bytes (sorted,
no owners, no times), which is how "changed" is decided: the new tarball's sha256 against the
one the index already names. The author is the login of the pull request that merged the
entry's first version, and never what its toml claims.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

KINDS = {"faces": "face", "toolbelts": "toolbelt", "bodies": "body"}
TOML = {"face": "face.toml", "toolbelt": "toolbelt.toml", "body": "body.toml"}
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
SHA = os.environ.get("GITHUB_SHA", "")
ACTOR = os.environ.get("GITHUB_ACTOR", "")
ROOT = Path(".")


def api(method: str, url: str, payload=None, data: bytes | None = None,
        content_type: str = "application/json"):
    if not url.startswith("https://"):
        url = f"https://api.github.com{url}"
    body = data if data is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {url} answered {exc.code}: {exc.read()[:500].decode(errors='replace')}")
    return json.loads(text) if text else {}


def pack(directory: Path) -> bytes:
    """A layer's files, refused whole if one is a link: packing follows it, and a link merged
    by mistake would publish what it points at on the runner, its token among them."""
    links = sorted(str(p) for p in directory.rglob("*") if p.is_symlink())
    if links:
        sys.exit(f"{directory} holds links, which a layer may not: {', '.join(links)}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            info = tarfile.TarInfo(path.relative_to(directory).as_posix())
            info.size = path.stat().st_size
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.mtime = 0
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def releases() -> list[dict]:
    out, page = [], 1
    while True:
        batch = api("GET", f"/repos/{REPO}/releases?per_page=100&page={page}")
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def merged_by_pull() -> str:
    """Whose pull request this push merged; a push that merged none is the pusher's own."""
    if not SHA:
        return ACTOR
    pulls = api("GET", f"/repos/{REPO}/commits/{SHA}/pulls")
    merged = [p for p in pulls if p.get("merged_at")]
    return merged[0]["user"]["login"] if merged else ACTOR


def main() -> None:
    old_path = ROOT / "index.json"
    old = {(e["kind"], e["id"]): e for e in json.loads(old_path.read_text())["entries"]} \
        if old_path.is_file() else {}
    all_releases = releases()
    downloads: dict[tuple[str, str], int] = {}
    for rel in all_releases:
        kind, _, rest = rel["tag_name"].partition("-")
        entry_id = rest.rpartition("-v")[0]
        downloads[(kind, entry_id)] = downloads.get((kind, entry_id), 0) + sum(
            a["download_count"] for a in rel.get("assets", []))

    entries, author_of_push = [], None
    for kinds, kind in KINDS.items():
        for directory in sorted(p for p in (ROOT / kinds).glob("*") if p.is_dir()):
            meta = tomllib.loads((directory / TOML[kind]).read_text())
            if meta.get("id") != directory.name:
                sys.exit(f"{directory}: its toml's id is {meta.get('id')!r}")
            data = pack(directory)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            key = (kind, directory.name)
            previous = old.get(key)
            if previous and previous["sha256"] == digest:
                entry = dict(previous)
            else:
                if author_of_push is None:
                    author_of_push = merged_by_pull()
                version = previous["version"] + 1 if previous else 1
                tag = f"{kind}-{directory.name}-v{version}"
                name = f"{kind}-{directory.name}.tar.gz"
                rel = api("POST", f"/repos/{REPO}/releases",
                          {"tag_name": tag, "name": tag, "target_commitish": SHA or "main"})
                asset = api("POST", f"https://uploads.github.com/repos/{REPO}/releases/"
                                    f"{rel['id']}/assets?name={name}",
                            data=data, content_type="application/gzip")
                entry = {"kind": kind, "id": directory.name, "version": version,
                         "asset": asset["browser_download_url"], "sha256": digest,
                         "size": len(data),
                         "author": previous["author"] if previous else author_of_push}
            entry["name"] = meta.get("name") or directory.name
            entry["description"] = meta.get("description")
            entry["thumbnail"] = (f"https://raw.githubusercontent.com/{REPO}/main/{kinds}/"
                                  f"{directory.name}/thumbnail.png"
                                  if (directory / "thumbnail.png").is_file() else None)
            entry["downloads"] = downloads.get(key, 0)
            entries.append(entry)
    old_path.write_text(json.dumps({"version": 1, "entries": entries}, indent=1) + "\n")


if __name__ == "__main__":
    main()
