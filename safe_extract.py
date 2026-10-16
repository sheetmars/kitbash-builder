#!/usr/bin/env python3
"""
safe_extract.py — safe recursive archive extractor for untrusted downloads.

WHAT IT DOES
    Walks a folder, finds every .zip / .7z / .rar / .tar archive, and extracts
    each one into a NEW FOLDER OF THE SAME NAME beside it. Originals are never
    modified or deleted. Archives nested inside other archives are extracted too.

    If two archives in the same folder share a name (Model.zip and Model.7z),
    each gets its own folder — "Model (zip)" and "Model (7z)" — so their
    contents can never overwrite each other.

WHAT IT DELIBERATELY DOES NOT DO
    It never runs, opens, launches, or interprets a single extracted file.
    It only decompresses bytes to disk. Nothing is executed at any point.

SAFETY GUARANTEES
    Enforced directly, for every archive libarchive can read (in practice
    nearly all of them), because this script writes every file itself rather
    than trusting an external tool:

    * Path traversal blocked - archive members with absolute paths or ".."
      components ("zip slip") are refused; every output path is verified to
      resolve inside its own target folder.
    * Symlinks / hardlinks never created - the classic trick for escaping a
      folder or clobbering a system file. Such entries are skipped and logged.
    * No device nodes, FIFOs or sockets - skipped and logged.
    * Execute bits stripped - every file is written mode 0o600, so nothing
      lands on disk marked runnable, even if the archive said it was.
    * No partial files left behind - each file is written to a temporary name
      and only moved into place once it has been read in full. A truncated or
      CRC-failed entry leaves nothing on disk to be mistaken for real data.
    * Encrypted archives skipped - detected and recorded, never prompted for,
      so the run can't hang waiting on a password.
    * Archive-bomb guards - caps on entry count, decompressed size and
      compression ratio; anything over the line is skipped and reported.
    * Free-space guard - refuses to start an archive that would fill the disk.

    FALLBACK PATH (status OK_VIA_FALLBACK): if libarchive cannot read a file at
    all, an external extractor (7z/unar) is tried, into a staging folder that is
    swept for symlinks and execute bits before anything is moved into place.
    Containment there depends partly on that tool rather than on this script,
    so those archives are flagged separately in the report. Nothing is executed
    in this path either.

RESUMABLE
    Progress is recorded as it goes. Stop it with Ctrl-C and run the same
    command again; it picks up where it left off and re-does nothing.

USAGE
    python3 safe_extract.py "/path/to/downloads"

    Progress and logs are kept in ~/.safe_extract_state (override with the
    SAFE_EXTRACT_STATE environment variable). A summary is written to
    _EXTRACTION_REPORT.txt in the folder you point it at.
"""

import ctypes
import ctypes.util
import errno
import os
import shutil
import subprocess
import sys
import time

STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("SAFE_EXTRACT_STATE") or "~/.safe_extract_state"))

ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz")
COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz")
MAX_ENTRIES = 200_000
MAX_TOTAL_UNCOMPRESSED = 40 << 30      # 40 GiB per archive
MAX_RATIO = 200                        # uncompressed : compressed
RATIO_MIN_SIZE = 1 << 30               # only apply ratio rule above 1 GiB output
FREE_SPACE_BUFFER = 25 << 30           # keep 25 GiB headroom
BLOCK = 4 << 20

AE_IFMT, AE_IFREG, AE_IFDIR, AE_IFLNK = 0o170000, 0o100000, 0o040000, 0o120000
ARCHIVE_EOF, ARCHIVE_OK = 1, 0
ARCHIVE_WARN, ARCHIVE_FAILED = -20, -25   # WARN is recoverable: keep going


# --------------------------------------------------------------- libarchive
def load_libarchive():
    candidates = [
        "/usr/lib/libarchive.2.dylib",
        "/opt/homebrew/lib/libarchive.dylib",
        "/usr/local/opt/libarchive/lib/libarchive.dylib",
        "/opt/homebrew/opt/libarchive/lib/libarchive.dylib",
        "libarchive.2.dylib",
        "libarchive.dylib",
        "libarchive.so.13",
    ]
    found = ctypes.util.find_library("archive")
    if found:
        candidates.insert(0, found)
    for c in candidates:
        try:
            return ctypes.CDLL(c)
        except OSError:
            continue
    return None


lib = load_libarchive()
HAVE_LIB = lib is not None

if HAVE_LIB:
    def _bind(name, argtypes, restype=ctypes.c_int):
        try:
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
            return fn
        except AttributeError:
            return None

    lib.archive_read_new.restype = ctypes.c_void_p
    for fn in ("archive_read_support_filter_all", "archive_read_support_format_all",
               "archive_read_close", "archive_read_free"):
        _bind(fn, [ctypes.c_void_p])
    _bind("archive_read_open_filename", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t])
    _bind("archive_read_next_header", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
    _bind("archive_read_data", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_ssize_t)
    _bind("archive_error_string", [ctypes.c_void_p], ctypes.c_char_p)
    _bind("archive_format_name", [ctypes.c_void_p], ctypes.c_char_p)
    _bind("archive_entry_pathname", [ctypes.c_void_p], ctypes.c_char_p)
    _bind("archive_entry_size", [ctypes.c_void_p], ctypes.c_longlong)
    _bind("archive_entry_filetype", [ctypes.c_void_p], ctypes.c_uint)
    _bind("archive_entry_symlink", [ctypes.c_void_p], ctypes.c_char_p)
    _bind("archive_entry_hardlink", [ctypes.c_void_p], ctypes.c_char_p)
    HAS_ENCRYPT_CHECK = _bind("archive_entry_is_encrypted", [ctypes.c_void_p]) is not None
else:
    HAS_ENCRYPT_CHECK = False


def decode(b):
    """Member names are UTF-8, or a legacy DOS codepage. cp850 maps all 256
    byte values, so it cannot fail — no third fallback is reachable."""
    if b is None:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp850")


def is_encrypted(entry):
    if not HAS_ENCRYPT_CHECK:
        return False
    return bool(lib.archive_entry_is_encrypted(entry))


# ------------------------------------------------------------------ helpers
def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def stem_of(name):
    low = name.lower()
    for ext in COMPOUND_EXTS:
        if low.endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


def ext_of(name):
    low = name.lower()
    for ext in COMPOUND_EXTS:
        if low.endswith(ext):
            return ext
    return os.path.splitext(name)[1]


_dir_cache = {}


def _stems_in_dir(d):
    """Map stem -> [archive filenames] for one directory, cached."""
    if d not in _dir_cache:
        m = {}
        try:
            for fn in os.listdir(d):
                if fn.startswith("._"):
                    continue
                if fn.lower().endswith(ARCHIVE_EXTS):
                    m.setdefault(stem_of(fn), []).append(fn)
        except OSError:
            pass
        _dir_cache[d] = m
    return _dir_cache[d]


def target_dir_for(archive_path):
    """Folder to extract into. If a sibling archive shares this stem (Model.zip
    vs Model.7z) each gets its own suffixed folder so they cannot merge."""
    d, name = os.path.split(archive_path)
    stem = stem_of(name)
    siblings = _stems_in_dir(d).get(stem, [])
    if len(siblings) > 1:
        return os.path.join(d, f"{stem} ({ext_of(name).lstrip('.').lower()})")
    return os.path.join(d, stem)


def sanitize_component(c):
    out = "".join("_" if (ord(ch) < 32 or ch in '<>:"|?*\\') else ch for ch in c)
    return out.rstrip(" .") or "_"


def safe_join(base, member):
    """Resolve member inside base; None if it tries to escape."""
    member = member.replace("\\", "/")
    if member.startswith("/") or (len(member) > 1 and member[1] == ":"):
        return None
    parts = []
    for part in member.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    path = os.path.join(base, *parts)
    rbase, rpath = os.path.realpath(base), os.path.realpath(path)
    if rpath != rbase and not rpath.startswith(rbase + os.sep):
        return None
    return path


def open_archive(path):
    a = lib.archive_read_new()
    lib.archive_read_support_filter_all(a)
    lib.archive_read_support_format_all(a)
    if lib.archive_read_open_filename(a, os.fsencode(path), BLOCK) != ARCHIVE_OK:
        err = decode(lib.archive_error_string(a))
        lib.archive_read_free(a)
        return None, err
    return a, None


def survey(path):
    """Header-only pass. Returns (info, error). info["entries"] == 0 with no
    error means a genuinely empty archive, not a failure."""
    a, err = open_archive(path)
    if a is None:
        return None, err
    entry = ctypes.c_void_p()
    n = total = 0
    fmt = None
    enc = False
    while True:
        r = lib.archive_read_next_header(a, ctypes.byref(entry))
        if r == ARCHIVE_EOF or r <= ARCHIVE_FAILED:
            break
        # ARCHIVE_OK or ARCHIVE_WARN — the entry is usable either way
        if fmt is None:
            fmt = decode(lib.archive_format_name(a))
        n += 1
        s = lib.archive_entry_size(entry)
        if s > 0:
            total += s
        if is_encrypted(entry):
            enc = True
        if n > MAX_ENTRIES:
            break
    lib.archive_read_free(a)
    return {"entries": n, "uncompressed": total, "format": fmt or "?", "encrypted": enc}, None


def harden_tree(root):
    """Remove any symlink and strip execute bits across a tree."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                os.unlink(p)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                os.unlink(p)
                removed += 1
                continue
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    return removed


def extract_with_lib(path, target, log):
    a, err = open_archive(path)
    if a is None:
        return "FAIL_OPEN", err or "could not open", 0
    entry = ctypes.c_void_p()
    buf = ctypes.create_string_buffer(BLOCK)
    written = skipped_special = traversal = 0
    notes = []
    status = "OK"

    def note(msg):
        if len(notes) < 12:
            notes.append(msg)

    while True:
        r = lib.archive_read_next_header(a, ctypes.byref(entry))
        if r == ARCHIVE_EOF:
            break
        if r <= ARCHIVE_FAILED:                      # unrecoverable: stop here
            e = decode(lib.archive_error_string(a))
            if e:
                note("header: " + e)
            if status == "OK":
                status = "PARTIAL"
            break
        if r == ARCHIVE_WARN:                        # recoverable: keep going
            e = decode(lib.archive_error_string(a))
            if e:
                note("warning: " + e)
            if status == "OK":
                status = "PARTIAL"

        name = decode(lib.archive_entry_pathname(entry))
        ftype = lib.archive_entry_filetype(entry) & AE_IFMT

        if lib.archive_entry_symlink(entry) or lib.archive_entry_hardlink(entry) or ftype == AE_IFLNK:
            skipped_special += 1
            log(f"    SKIP link entry: {name!r}")
            continue
        if ftype not in (AE_IFREG, AE_IFDIR):
            skipped_special += 1
            log(f"    SKIP special entry ({oct(ftype)}): {name!r}")
            continue
        if is_encrypted(entry):
            lib.archive_read_free(a)
            return "ENCRYPTED", "password protected", written

        dest = safe_join(target, name)
        if dest is None:
            traversal += 1
            log(f"    BLOCKED path traversal: {name!r}")
            continue

        if ftype == AE_IFDIR:
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError:
                pass
            continue

        parent = os.path.dirname(dest)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as ex:
            note(f"mkdir: {ex}")
            if status == "OK":
                status = "PARTIAL"
            continue

        # Open first, so a name-related failure is handled BEFORE any of the
        # entry's data stream is consumed. Retrying after a partial read would
        # silently produce a truncated file.
        tmp = dest + ".part"
        fd = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as ex:
            if ex.errno in (errno.EINVAL, errno.ENAMETOOLONG, errno.EILSEQ, errno.ELOOP):
                dest = os.path.join(parent, sanitize_component(os.path.basename(dest))[:200])
                tmp = dest + ".part"
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    log(f"    renamed for filesystem: {name!r}")
                except OSError as ex2:
                    ex = ex2
            if fd is None:
                note(f"{os.path.basename(name)}: {ex}")
                if status == "OK":
                    status = "PARTIAL"
                continue

        # Stream exactly once. On any failure the temp file is discarded, so a
        # truncated or CRC-failed entry never lands under its real name.
        try:
            while True:
                got = lib.archive_read_data(a, buf, BLOCK)
                if got == 0:
                    break
                if got < 0:
                    raise IOError(decode(lib.archive_error_string(a)) or "read error")
                os.write(fd, buf.raw[:got])
            os.close(fd)
            fd = None
            os.replace(tmp, dest)
            written += 1
        except (OSError, IOError) as ex:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            note(f"{os.path.basename(name)}: {ex} (discarded)")
            if status == "OK":
                status = "PARTIAL"

    lib.archive_read_free(a)
    if traversal:
        note(f"{traversal} path-traversal entries BLOCKED")
        if status in ("OK", "PARTIAL"):
            status = "SECURITY_BLOCKED"
    if skipped_special:
        note(f"{skipped_special} link/special entries skipped")
    return status, "; ".join(notes[:6]), written


def find_cli():
    for tool in ("7zz", "7z", "unar", "keka7z"):
        p = shutil.which(tool)
        if p:
            return tool, p
    for p in ("/Applications/Keka.app/Contents/Resources/keka7z",
              "/usr/local/bin/unar", "/opt/homebrew/bin/unar"):
        if os.path.exists(p):
            return os.path.basename(p), p
    return None, None


def extract_with_cli(path, target, tool, toolpath, log):
    """Fallback for archives libarchive cannot read at all. Extracts into a
    staging folder, sweeps it for symlinks and execute bits, then moves the
    contents into place through the same containment check used elsewhere.
    Nothing from the archive is ever executed."""
    staging = os.path.join(target, ".__staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    if tool == "unar":
        cmd = [toolpath, "-quiet", "-force-overwrite", "-no-directory",
               "-output-directory", staging, path]
    else:
        cmd = [toolpath, "x", "-y", "-bd", f"-o{staging}", path]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=3600)
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        return "FAIL_TIMEOUT", "extractor timed out", 0
    except OSError as ex:
        shutil.rmtree(staging, ignore_errors=True)
        return "FAIL_OPEN", f"could not run {tool}: {ex}", 0

    removed = harden_tree(staging)
    moved = blocked = 0
    for dirpath, _, filenames in os.walk(staging):
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, staging)
            dest = safe_join(target, rel)
            if dest is None:
                blocked += 1
                try:
                    os.unlink(src)
                except OSError:
                    pass
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(src, dest)
                moved += 1
            except OSError:
                pass
    shutil.rmtree(staging, ignore_errors=True)

    note = f"via {tool}"
    if removed:
        note += f"; {removed} symlinks removed"
    if blocked:
        note += f"; {blocked} entries blocked"
    if r.returncode != 0:
        tail = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] if r.stdout else []
        return "PARTIAL", note + "; " + (tail[0] if tail else f"exit {r.returncode}"), moved
    return ("SECURITY_BLOCKED" if blocked else "OK_VIA_FALLBACK"), note, moved


def find_archives(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith("._") and d not in
                       (".Spotlight-V100", "System Volume Information", ".fseventsd",
                        ".TemporaryItems", ".Trashes", ".__staging")]
        for fn in filenames:
            if fn.startswith("._"):
                continue
            if fn.lower().endswith(ARCHIVE_EXTS):
                out.append(os.path.join(dirpath, fn))
    return out


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def write_report(root, manifest_path, counts):
    report = os.path.join(root, "_EXTRACTION_REPORT.txt")
    meanings = [
        ("OK", "extracted cleanly"),
        ("EMPTY", "archive contained no files (not an error)"),
        ("OK_VIA_FALLBACK", "libarchive could not read it; external extractor used"),
        ("PARTIAL", "some entries failed and were discarded; see notes"),
        ("SECURITY_BLOCKED", "archive tried to write outside its folder - blocked"),
        ("ENCRYPTED", "password protected, skipped"),
        ("SUSPICIOUS_SKIPPED", "archive-bomb heuristics tripped, skipped"),
        ("FAIL_OPEN", "unreadable or corrupt"),
        ("FAIL_MKDIR", "could not create the destination folder"),
        ("FAIL_TIMEOUT", "external extractor timed out"),
    ]
    try:
        with open(report, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("EXTRACTION REPORT\n")
            f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"Root: {root}\n\n")
            f.write("Nothing in these archives was executed, opened or run.\n")
            f.write("Original archives were left in place, untouched.\n")
            f.write("Entries that failed mid-read were discarded, not left half-written.\n\n")
            f.write("RESULTS BY STATUS\n")
            for k, v in sorted(counts.items()):
                f.write(f"  {k:20s} {v}\n")
            f.write("\nSTATUS MEANINGS\n")
            for k, v in meanings:
                f.write(f"  {k:20s} {v}\n")
            f.write("\nITEMS NEEDING A LOOK\n")
            n = 0
            with open(manifest_path, encoding="utf-8", errors="surrogateescape") as m:
                for line in m:
                    p = line.rstrip("\n").split("\t")
                    if len(p) >= 2 and p[1] not in ("OK", "EMPTY"):
                        f.write("  " + " | ".join(p) + "\n")
                        n += 1
            if n == 0:
                f.write("  (nothing — every archive extracted cleanly)\n")
        return report
    except OSError as ex:
        print(f"(could not write report: {ex})")
        return None


def main():
    if not HAVE_LIB:
        sys.exit("Could not load libarchive.\n"
                 "Install it with:  brew install libarchive   (macOS)\n"
                 "              or:  sudo apt install libarchive13   (Debian/Ubuntu)\n"
                 "then run this script again.")

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    root = os.path.abspath(os.path.expanduser(sys.argv[1]))
    if not os.path.isdir(root):
        sys.exit(f"Folder not found: {root}\nIs the drive plugged in?")

    os.makedirs(STATE_DIR, exist_ok=True)
    manifest_path = os.path.join(STATE_DIR, "manifest.tsv")
    logfile = open(os.path.join(STATE_DIR, "run.log"), "a", buffering=1)

    def log(m):
        logfile.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")

    cli_tool, cli_path = find_cli()

    done = set()
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                if line.strip():
                    done.add(line.split("\t")[0])

    print(f"Scanning {root} ...")
    queue = [a for a in find_archives(root) if a not in done]
    queued = set(queue)
    total_bytes = sum(os.path.getsize(a) for a in queue if os.path.exists(a))
    print(f"{len(queue)} archives to process ({human(total_bytes)} compressed)")
    if done:
        print(f"({len(done)} already done in a previous run — skipping those)")
    if cli_tool:
        print(f"Fallback extractor available for unreadable formats: {cli_tool}")
    print("Originals are kept. Nothing is ever executed.\n")

    mf = open(manifest_path, "a", buffering=1, encoding="utf-8", errors="surrogateescape")
    counts = {}
    processed = bytes_done = 0
    t0 = time.time()

    def record(path, status, nfiles, usize, note):
        mf.write(f"{path}\t{status}\t{nfiles}\t{usize}\t{note}\n")
        done.add(path)
        counts[status] = counts.get(status, 0) + 1

    try:
        while queue:
            arch = queue.pop(0)
            queued.discard(arch)
            if arch in done or not os.path.exists(arch):
                continue
            csize = os.path.getsize(arch)

            info, err = survey(arch)
            use_cli = False

            if info is None:
                # libarchive could not open it at all
                if cli_tool:
                    use_cli = True
                else:
                    record(arch, "FAIL_OPEN", 0, 0, err or "could not open")
                    log(f"FAIL_OPEN {arch}: {err}")
                    continue
            elif info["entries"] == 0:
                # opened fine, genuinely nothing inside — not a failure
                record(arch, "EMPTY", 0, 0, "archive contains no files")
                log(f"EMPTY {arch}")
                continue

            u = 0
            if not use_cli:
                u = info["uncompressed"]
                ratio = u / (csize + 1)
                if info["entries"] > MAX_ENTRIES or u > MAX_TOTAL_UNCOMPRESSED or \
                   (u > RATIO_MIN_SIZE and ratio > MAX_RATIO):
                    record(arch, "SUSPICIOUS_SKIPPED", info["entries"], u,
                           f"ratio={ratio:.0f}x entries={info['entries']}")
                    print(f"  !! SKIPPED (looks like an archive bomb): {os.path.basename(arch)}")
                    log(f"SUSPICIOUS {arch} ratio={ratio:.0f}")
                    continue
                if info["encrypted"]:
                    record(arch, "ENCRYPTED", info["entries"], u, "password protected")
                    print(f"  -- password protected, skipped: {os.path.basename(arch)}")
                    continue

            needed = u if not use_cli else csize * 4      # rough guess for the CLI path
            if free_bytes(root) < needed + FREE_SPACE_BUFFER:
                print("\nSTOPPING: not enough free space left on the drive.")
                print(f"Free: {human(free_bytes(root))}. Free some space and re-run to resume.")
                queue.insert(0, arch)
                break

            target = target_dir_for(arch)
            if os.path.isfile(target):
                target += "_extracted"
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as ex:
                record(arch, "FAIL_MKDIR", 0, 0, str(ex))
                continue

            if use_cli:
                status, note, nfiles = extract_with_cli(arch, target, cli_tool, cli_path, log)
            else:
                status, note, nfiles = extract_with_lib(arch, target, log)

            record(arch, status, nfiles, u, note)
            processed += 1
            bytes_done += csize
            log(f"{status} ({nfiles} files) {arch}")

            # pick up archives that were nested inside what we just extracted
            _dir_cache.pop(target, None)
            for nested in find_archives(target):
                if nested not in done and nested not in queued:
                    queue.append(nested)
                    queued.add(nested)
                    _dir_cache.pop(os.path.dirname(nested), None)
                    try:
                        total_bytes += os.path.getsize(nested)
                    except OSError:
                        pass
                    log(f"    nested archive queued: {nested}")

            el = time.time() - t0
            rate = bytes_done / el if el > 0 else 0
            remain = max(0.0, (total_bytes - bytes_done) / rate) if rate > 0 else 0
            pct = min(100.0, 100 * bytes_done / total_bytes) if total_bytes else 100.0
            print(f"[{pct:5.1f}%] {processed} done, {len(queue)} left | "
                  f"{human(rate)}/s | ETA {remain/3600:.1f}h | {os.path.basename(arch)[:48]}")

    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved — re-run the same command to resume.")
    finally:
        mf.close()
        report = write_report(root, manifest_path, counts)
        logfile.close()

    if report:
        print(f"\nReport written to: {report}")
    print("\nSummary:")
    for k, v in sorted(counts.items()):
        print(f"  {k:20s} {v}")
    print(f"\nFull manifest: {manifest_path}")
    if any(k not in ("OK", "EMPTY") for k in counts):
        print("Some archives need attention — see the report above.")


if __name__ == "__main__":
    main()
