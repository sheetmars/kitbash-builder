# Safe archive extraction

Model packs are usually downloaded as archives, often with more archives inside them. `safe_extract.py` unpacks a whole folder of them without trusting their contents.

```bash
python3 safe_extract.py "/path/to/downloads"
```

It prints progress with a running ETA. Press Ctrl-C to stop at any point; running the same command again picks up where it left off and repeats nothing.

---

## What it does

It finds every `.zip`, `.7z`, `.rar` and `.tar` (including `.tgz`, `.tar.gz`, `.tar.bz2`, `.tar.xz`) under the folder and extracts each one into a **new folder of the same name** beside it:

```
Flight_Stand_32mm.zip     <- kept, untouched
Flight_Stand_32mm/        <- new folder with the contents
```

Archives found inside other archives are extracted too, until none are left. **Original archives are never modified or deleted.**

When two archives in the same folder share a name (often a `.zip` and a `.7z` of the same model), each gets its own folder so their contents can't overwrite each other:

```
Model.zip  ->  Model (zip)/
Model.7z   ->  Model (7z)/
```

---

## Safety

**Nothing in any archive is ever run, opened or launched.** The script only decompresses bytes to disk. It writes every file itself, through libarchive, rather than handing the job to another tool, which is what lets it enforce these:

| Threat | What happens |
|---|---|
| **Path traversal**: a member named `../../something` or an absolute path, to escape its folder | Blocked. Every output path is checked to land inside its own folder. |
| **Symlinks and hard links** that redirect writes onto a real file | Never created. Skipped and logged. |
| **Executables**: a file arriving marked runnable | Execute bits stripped. Every file is written as `rw-------`. |
| **Device nodes, FIFOs, sockets** | Skipped. |
| **Truncated or corrupt entries** | Written to a temporary name and only moved into place once complete, so no half-written file is left behind. |
| **Archive bombs**: a few MB that expand to fill the disk | Caught by limits on entry count, total size and compression ratio, and skipped. |
| **Password-protected archives** | Detected and skipped, never prompted for, so the run can't hang. |
| **Disk filling up** | Refuses to start an archive that would leave less than 25 GB free. |

If libarchive cannot read a file at all, an external extractor (`7z` or `unar`, if installed) is tried into a staging folder, which is swept for links and execute bits before anything is moved into place. Containment there depends partly on that tool, so those archives are reported separately.

The limits are constants at the top of the script (`MAX_ENTRIES`, `MAX_TOTAL_UNCOMPRESSED`, `MAX_RATIO`, `FREE_SPACE_BUFFER`).

---

## When it finishes

`_EXTRACTION_REPORT.txt` is written in the folder you extracted, listing anything that needs a look, grouped by status:

| Status | Meaning |
|---|---|
| `OK` | Extracted cleanly |
| `EMPTY` | The archive genuinely had nothing in it. Not an error. |
| `PARTIAL` | Some entries failed and were discarded rather than half-written |
| `SECURITY_BLOCKED` | The archive tried to write outside its folder. Worth a close look. |
| `ENCRYPTED` | Password protected, skipped |
| `SUSPICIOUS_SKIPPED` | Tripped the archive-bomb limits, skipped |
| `OK_VIA_FALLBACK` | Extracted by the external tool rather than libarchive |
| `FAIL_OPEN` | Corrupt or unreadable |
| `FAIL_MKDIR` | The destination folder couldn't be created |
| `FAIL_TIMEOUT` | The external extractor timed out |

The full per-archive manifest and log are kept in `~/.safe_extract_state/` (change it with the `SAFE_EXTRACT_STATE` environment variable).

---

## If something goes wrong

| Problem | Fix |
|---|---|
| `command not found: python3` (macOS) | `xcode-select --install` |
| `Could not load libarchive` | macOS: `brew install libarchive`. Debian/Ubuntu: `sudo apt install libarchive13`. |
| `.rar` or `.7z` files fail to open | Install a fallback extractor: `brew install sevenzip` or `brew install unar` (macOS), `sudo apt install 7zip` or `unar` (Linux). |

---

## A caveat

Extracting safely is not the same as scanning. Once the contents are on disk, your antivirus can see inside what were opaque archives, so **run it over the folder afterwards**, starting with anything the report didn't mark `OK`.

A model pack should contain `.stl`, `.obj`, `.lys`, `.chitubox` and similar print files, images and text. Anything ending in `.app`, `.exe`, `.scr`, `.dmg`, `.pkg`, `.js` or `.command` deserves suspicion. None of it can run on its own, but none of it belongs in a model pack either.
