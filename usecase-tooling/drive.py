"""Find AI use cases in the Drive mirror the pipeline already produces.

No API calls and no network. `pipeline/sync.ps1` has already turned the team's
Drive into markdown under `<vault>/drive/`, so this reads that, which means it
costs nothing and works offline. It is also the only one of the three sources
where the material was already shared with colleagues by the person who wrote
it, so Drive records can be quoted, subject to the per-recipient permission
check in publish.py.

Each record carries the Drive file id where the manifest knows it. Without the
id publish.py cannot check who may read the document, and a record it cannot
check is reduced to metadata rather than trusted.
"""

import argparse
import json
from pathlib import Path

import aiuse


def manifest_ids(vault):
    """path -> Drive file id, from the team manifest when one exists."""
    p = Path(vault) / "team-manifest.json"
    if not p.is_file():
        return {}
    try:
        # utf-8-sig: a manifest hand-edited on Windows comes back with a BOM
        man = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}
    return {d["path"]: d.get("id", "") for d in man.get("documents", []) if d.get("path")}


def collect(vault, verbose=True):
    """Every mirrored document that records somebody using an AI tool."""
    mirror = Path(vault) / "drive"
    if not mirror.is_dir():
        raise RuntimeError(
            f"No Drive mirror at {mirror}. Run pipeline/sync.ps1 first.")
    ids = manifest_ids(vault)
    out, scanned = [], 0
    for f in mirror.rglob("*.md"):
        scanned += 1
        rec = aiuse.classify_file(f)
        if not rec or not rec["is_use_case"]:
            continue
        # the manifest keys on the drive-relative path, which is what the
        # mirror's folder structure reproduces under drive/
        rel = str(f.relative_to(mirror))
        rel_no_md = rel[:-3] if rel.endswith(".md") else rel
        out.append({
            "source": "drive",
            "visibility": "drive",
            "person": "",                  # filled from the manifest author below
            "email": "",
            "date": "",
            "path": rel_no_md,
            "file_id": ids.get(rel_no_md, ""),
            "text": "",                    # the document stays in the mirror
            "tools": rec["tools"],
            "tasks": rec["tasks"],
            "evidence": rec["evidence"],
            "score": rec["score"],
            "why": rec["why"],
        })
        if verbose and len(out) % 25 == 0:
            print(f"  scanned {scanned}, {len(out)} use cases so far")
    if verbose:
        print(f"  scanned {scanned} documents")
    return out


def enrich_from_manifest(records, vault):
    """Attach the document owner and its modified date, where the manifest has them."""
    p = Path(vault) / "team-manifest.json"
    if not p.is_file():
        return records
    try:
        docs = {d["path"]: d for d in json.loads(
            p.read_text(encoding="utf-8")).get("documents", [])}
    except ValueError:
        return records
    for r in records:
        d = docs.get(r.get("path", ""))
        if not d:
            continue
        r["email"] = (d.get("author") or "").lower()
        r["person"] = aiuse_display(r["email"])
        r["date"] = d.get("modified", "")
    return records


def aiuse_display(email):
    """A readable name from an address. Mirrors teamvault/core.display_name."""
    import re
    local = (email or "").split("@")[0]
    local = re.sub(r"[._-]+", " ", local)
    local = re.sub(r"\d+", "", local).strip()
    if not local:
        return email or ""
    return " ".join(w[:1].upper() + w[1:] for w in local.split())


def main():
    ap = argparse.ArgumentParser(description="Find AI use cases in the Drive mirror.")
    ap.add_argument("--vault", default=str(Path.home() / "Documents" / "TeamVault"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    print("scanning the Drive mirror")
    records = enrich_from_manifest(collect(a.vault), a.vault)
    print(f"\n{len(records)} use cases found")
    no_id = sum(1 for r in records if not r["file_id"])
    if no_id:
        print(f"  {no_id} have no Drive id, so they will be reduced to metadata "
              "rather than quoted. Re-scan in Team Vault to record ids.")
    if a.dry_run:
        for r in records[:20]:
            print(f"  {r['path']}: {r['evidence'][:80]}")
        return
    out = Path(a.vault) / "usecases.json"
    out.write_text(json.dumps(records, indent=1, ensure_ascii=True), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
