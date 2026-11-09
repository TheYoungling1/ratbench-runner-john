# Handoff — moving large local folders onto the external SSD

Written 2026-10-01. How `runs/` and `results_pjy/` (≈12 GB, 153,416 entries) were moved off the
internal disk of this Mac onto the external SSD **without losing a byte**, and how to repeat it for
another directory. Read the whole thing before deleting anything.

---

## What the end state looks like

```
<repo>/runs         -> /Volumes/ratbench-archive/runs          (symlink)
<repo>/results_pjy  -> /Volumes/ratbench-archive/results_pjy   (symlink)
/Volumes/ratbench-archive  = mounted APFS sparse image stored at
                             /Volumes/SSD/ratbench-archive.sparsebundle
```

Code keeps using the old relative paths (`runs/setupx/...`, `--pjy results_pjy`). The image must be
mounted for those paths to resolve: `hdiutil attach /Volumes/SSD/ratbench-archive.sparsebundle`
(or double-click it in Finder).

To add another directory, reuse the same image (attach it, copy into it) instead of creating a
second one — it grows on demand up to its 40 GB cap (`hdiutil resize -size 80g <bundle>` to raise it).

## Research: why not just drag the folder to the SSD

| Finding | How it was checked | Consequence |
|---|---|---|
| The SSD is **FAT32** (`MS-DOS FAT32`, USB) | `/usr/sbin/diskutil info /Volumes/SSD` | No symlinks, no hardlinks, no Unix permissions, **4 GiB max file size**. A direct copy silently drops/breaks the 13,113 symlinks. |
| Reformatting the SSD is not an option | `ls /Volumes/SSD` | It holds ~98 GB of unrelated data. |
| An **APFS sparsebundle** on FAT32 works | tested | The bundle is stored as many small 8 MB band files (fine on FAT32); inside it is a real APFS volume with symlinks, hardlinks, xattrs, flags. |
| macOS `rsync` is **openrsync** (protocol 29) | `rsync --version` | Can't be trusted for hardlinks/xattrs. **Use a `tar` pipe (bsdtar)** — tested to preserve hardlinks, symlinks (abs + rel, verbatim), empty dirs, xattrs and BSD flags. |
| No backup exists | `tmutil destinationinfo` → "No destinations configured"; `tmutil isexcluded` → both folders excluded | After the move the image is the **only copy**. Tell the user; suggest a second copy of the `.sparsebundle`. |
| Tools missing from the agent's `PATH` | `diskutil`, `mount` not found | Use full paths: `/usr/sbin/diskutil`, `/sbin/mount`. |

## Audit results for runs/ + results_pjy/ (what "100% mapped" meant)

- 109,300 files (14.8 GB apparent; `du` said 12 GB because of hardlinks + compression), 31,003 dirs, 13,113 symlinks.
- No file > 4 GiB, no unreadable files, no FIFOs/sockets, nothing owned by another user, nothing open (`lsof +D`).
- BSD flags: only `0x8000` (UF_HIDDEN) on `.DS_Store` — harmless.
- 3,211 hardlinked inodes, **all** with every link inside the moved trees (none shared with outside paths).
- Symlinks by class:
  - 14 relative, internal → keep working anywhere.
  - 3,171 absolute **into** the moved trees (`/Users/john/ratbench-runner-john/results_pjy/...`) → keep working through the repo-level symlink while mounted.
  - 3,559 absolute **to data outside** (`~/Graph2Env/outputs/abl40_*`, `~/Graph2Env/EnvBench_Graph2Env`, `~/Graph2Env/RatNew_Graph2Env`) → only the link is moved; **that data stays local and must not be deleted**.
  - 6,369 **already broken** before the move (`/opt/ratbench-runner/results_pjy/...`, VM paths) → not caused or fixable by the move.
  - 0 relative links escaping the moved tree (these are the dangerous class — they would break).
- No symlinks elsewhere (repo, `~/Graph2Env`) pointing *into* the trees; no code with absolute paths to them.

## Procedure

`offload_tools.py` is at the bottom of this doc — save it to a scratch dir first. Throughout,
`BASE` is the parent directory and `ROOTS` are the folder names to move.

### 1. Audit (read-only)

```bash
BASE=/Users/john/<project>; ROOTS="dirA dirB"
du -sh $BASE/{dirA,dirB}; df -h /Volumes/SSD            # fits?
python3 offload_tools.py audit $BASE $ROOTS > audit.json
lsof +D $BASE/dirA; lsof +D $BASE/dirB                     # anything writing right now?
tmutil isexcluded $BASE/dirA                               # is there any backup today?
git -C $BASE check-ignore -v dirA                          # tracked by git?
# anything elsewhere linking INTO the trees (adjust search roots):
grep -rlE "$BASE/(dirA|dirB)" $BASE --exclude-dir=dirA --exclude-dir=dirB --exclude-dir=.git
```

Stop and ask the user if the audit shows any of: `rel_ESCAPES_moved_tree` symlinks,
`hardlinks_shared_with_outside > 0`, `unreadable_*`, `special_file`, `not_owned_by_me`,
or open files. Report `+data_outside` roots — that data is not being moved.

### 2. Create + mount the image (skip if reusing `ratbench-archive`)

```bash
hdiutil create -type SPARSEBUNDLE -fs APFS -size 40g -volname ratbench-archive /Volumes/SSD/ratbench-archive.sparsebundle
hdiutil attach /Volumes/SSD/ratbench-archive.sparsebundle
```

Size the cap comfortably above the data; the bundle only uses what is written.

### 3. Copy with a tar pipe (run in background; ~12 GB took ~8 min over USB)

```bash
cd $BASE && tar -cf - dirA dirB | (cd /Volumes/ratbench-archive && tar -xpf -) && echo COPY_OK
```

Monitor with `df -h /Volumes/ratbench-archive` once a minute.

### 4. Verify every byte (several minutes; it hashes both sides)

```bash
python3 offload_tools.py verify $BASE /Volumes/ratbench-archive $ROOTS
```

It compares every entry by **byte-exact path**, type, size, SHA-256 of contents, symlink target
text, and hardlink grouping. Only `VERIFY_OK` is a pass.

**Expected snag — Unicode normalization.** bsdtar extracted a Vietnamese folder name
(`Việt_Nhân_Sầu_Thương`) in decomposed form (NFD) while the source was NFC. Verify reports it as
`MISSING`+`EXTRA` pairs with identical-looking names; content is identical and APFS resolves either
form, but fix it so the copy is exact, then re-verify:

```bash
python3 offload_tools.py fixnames $BASE /Volumes/ratbench-archive $ROOTS
python3 offload_tools.py verify   $BASE /Volumes/ratbench-archive $ROOTS   # must print VERIFY_OK
```

(CJK names were unaffected; only precomposed accented Latin letters decompose.)

### 5. Swap in symlinks — rename first, delete last

```bash
cd $BASE
mv dirA dirA.local-old && mv dirB dirB.local-old
ln -s /Volumes/ratbench-archive/dirA dirA && ln -s /Volumes/ratbench-archive/dirB dirB
# smoke-test: list a few subdirs, readlink -f an internal relative link,
# test -f the target of an abs_into_moved link, and check paths the code uses exist.
```

**Git gotcha:** an ignore rule with a trailing slash (`/runs/`) matches only directories, not a
symlink — `runs` showed up as untracked. Change it to `/runs` and confirm with
`git check-ignore -v runs`.

Only when the smoke tests pass: `rm -rf dirA.local-old dirB.local-old`.

## Agent-environment notes

- Claude Code's auto-mode classifier blocked a batched `docker rmi … && docker volume rm …` as
  "unverifiable deletion scope"; the user had to run it with `!`. Expect the same for bulk
  deletions: show exact scope first, and let the user run it or add a permission rule. The final
  `rm -rf *.local-old` here was allowed because verification had passed and the scope was two named dirs.
- Get explicit user confirmation before step 5's deletion (user's global rules).
- Record the move in memory (the image is the sole copy; it must be mounted before runs).

## offload_tools.py

```python
"""Audit, verify and name-fix helpers for moving directory trees onto an external disk image.

  python3 offload_tools.py audit  <base> <root> [<root> ...]
  python3 offload_tools.py verify <src_base> <dst_base> <root> [<root> ...]
  python3 offload_tools.py fixnames <src_base> <dst_base> <root> [<root> ...]
"""
import collections, hashlib, json, os, stat, sys, unicodedata


def inside(path, bases):
    return any(path == b or path.startswith(b + os.sep) for b in bases)


def walk(base, roots):
    for r in roots:
        for dp, dns, fns in os.walk(os.path.join(base, r)):
            for name in dns + fns:
                yield dp, name, os.path.join(dp, name)


def audit(base, roots):
    moving = [os.path.join(base, r) for r in roots]
    kinds, links, risks = collections.Counter(), collections.defaultdict(list), collections.defaultdict(list)
    outside_roots, per_top = collections.Counter(), collections.Counter()
    seen_inodes, nlink = collections.Counter(), {}
    total = 0
    for dp, name, p in walk(base, roots):
        rel = os.path.relpath(p, base)
        st = os.lstat(p)
        m = st.st_mode
        if st.st_uid != os.getuid():
            risks["not_owned_by_me"].append(rel)
        if st.st_flags & ~0x8000:  # 0x8000 = UF_HIDDEN (Finder .DS_Store), harmless
            risks["bsd_flags"].append(f"{rel}: {st.st_flags:#x}")
        if stat.S_ISLNK(m):
            kinds["symlink"] += 1
            t = os.readlink(p)
            lexical = os.path.normpath(t if os.path.isabs(t) else os.path.join(dp, t))
            real = os.path.realpath(p)
            if not os.path.exists(p):
                cls = "broken_already"
            elif os.path.isabs(t):
                cls = "abs_into_moved" if inside(lexical, moving) else "abs_outside"
            else:
                cls = "rel_internal" if inside(lexical, moving) else "rel_ESCAPES_moved_tree"
            if os.path.exists(p) and not inside(real, moving):
                cls += "+data_outside"  # the link is copied, the data it points at is NOT
                outside_roots["/".join(real.split("/")[:6])] += 1
            links[cls].append(f"{rel} -> {t}")
        elif stat.S_ISDIR(m):
            kinds["dir"] += 1
            if not os.access(p, os.R_OK | os.X_OK):
                risks["unreadable_dir"].append(rel)
        elif not stat.S_ISREG(m):
            kinds["special"] += 1
            risks["special_file(fifo/socket/dev)"].append(rel)
        else:
            kinds["file"] += 1
            total += st.st_size
            per_top["/".join(rel.split("/")[:2])] += st.st_size
            if not os.access(p, os.R_OK):
                risks["unreadable_file"].append(rel)
            if st.st_size > 4 * 1024**3 - 1:
                risks["over_4GiB(FAT32 limit)"].append(rel)
            if st.st_nlink > 1:
                seen_inodes[st.st_ino] += 1
                nlink[st.st_ino] = st.st_nlink
        if not name.isascii() and not unicodedata.is_normalized("NFC", name):
            risks["non_NFC_name"].append(rel)
    report = {
        "apparent_bytes": total, "kinds": dict(kinds),
        "largest_subdirs_MB": {k: round(v / 1e6, 1) for k, v in per_top.most_common(15)},
        "symlink_classes": {k: len(v) for k, v in links.items()},
        "symlink_samples": {k: v[:3] for k, v in links.items()},
        "data_outside_roots": dict(outside_roots.most_common()),
        "hardlinked_inodes": len(seen_inodes),
        "hardlinks_shared_with_outside": sum(nlink[i] > c for i, c in seen_inodes.items()),
        "risks": {k: {"count": len(v), "samples": v[:5]} for k, v in risks.items()},
    }
    print(json.dumps(report, indent=1))


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(base, roots):
    entries, groups, hashes = {}, {}, {}
    for dp, name, p in walk(base, roots):
        rel = os.path.relpath(p, base).encode()  # bytes: catches NFC/NFD renames
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            entries[rel] = ("L", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            entries[rel] = ("D",)
        else:
            if st.st_ino not in hashes:
                hashes[st.st_ino] = digest(p)
            entries[rel] = ("F", st.st_size, hashes[st.st_ino])
            if st.st_nlink > 1:
                groups.setdefault(st.st_ino, []).append(rel)
    return entries, {frozenset(v) for v in groups.values()}


def verify(src, dst, roots):
    a, ga = snapshot(src, roots)
    b, gb = snapshot(dst, roots)
    missing, extra = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    print(f"entries src={len(a)} copy={len(b)} | files={sum(v[0] == 'F' for v in a.values())} "
          f"dirs={sum(v[0] == 'D' for v in a.values())} symlinks={sum(v[0] == 'L' for v in a.values())} "
          f"bytes={sum(v[1] for v in a.values() if v[0] == 'F')}")
    print(f"missing={len(missing)} extra={len(extra)} differs={len(differ)} "
          f"hardlink_groups src={len(ga)} copy={len(gb)} same={ga == gb}")
    for label, items in (("MISSING", missing), ("EXTRA", extra), ("DIFFERS", differ)):
        for k in items[:10]:
            print(label, k.decode(errors="replace"))
    ok = not (missing or extra or differ) and ga == gb
    print("VERIFY_OK" if ok else "VERIFY_FAILED")
    return ok


def fixnames(src, dst, roots):
    """Rename copy entries whose name bytes differ from the source only by Unicode normalization."""
    fixed = 0
    for r in roots:
        for dp, dns, fns in os.walk(os.path.join(dst, r)):  # top-down: parents fixed before children
            want = {unicodedata.normalize("NFC", n): n
                    for n in os.listdir(os.path.join(src, os.path.relpath(dp, dst)))}
            for names in (dns, fns):
                for i, n in enumerate(names):
                    orig = want.get(unicodedata.normalize("NFC", n))
                    if orig is not None and orig.encode() != n.encode():
                        tmp = os.path.join(dp, ".__nfc_fix_tmp")
                        os.rename(os.path.join(dp, n), tmp)
                        os.rename(tmp, os.path.join(dp, orig))
                        names[i] = orig  # os.walk descends using the corrected name
                        fixed += 1
    print(f"renamed {fixed} entries to their source byte form")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "audit":
        audit(args[0], args[1:])
    elif cmd == "verify":
        sys.exit(0 if verify(args[0], args[1], args[2:]) else 1)
    elif cmd == "fixnames":
        fixnames(args[0], args[1], args[2:])
    else:
        sys.exit(__doc__)
```
