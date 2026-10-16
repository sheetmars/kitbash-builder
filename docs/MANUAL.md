# Kitbash Builder — Manual

This covers the full workflow. For installation and a quick start, see the [README](../README.md). For a complete list of the built-in lineages, keywords and thresholds, run `python3 kitbash.py rules`, which writes `CLASSIFICATION_RULES.md` from the code.

---

## 1. First-time setup

```bash
python3 -m pip install numpy pillow openpyxl
python3 kitbash.py doctor
```

`doctor` reports which Python interpreter is running, whether numpy and Pillow import *in that interpreter*, every copy of numpy on the import path (a broken copy earlier in the list silently shadows a good one), where the library, data, bundle and sheet paths point, and how many parts are indexed.

---

## 2. Indexing

```bash
python3 kitbash.py index "/path/to/your/stls"
```

The first folder you index becomes the library root and is remembered. Later you can index the whole library again with plain `index`, or one folder inside it with `index "/path/to/your/stls/Faction A"`.

Indexing reads every mesh, measures it, renders a 256 px thumbnail and saves a simplified preview mesh. Reading the files is the slow part, so the time depends mostly on how fast your drive is. On a laptop, stop it sleeping until indexing finishes; on macOS, run this in a second Terminal window:

```bash
caffeinate -ims -w $(pgrep -f "kitbash.py index" | head -1)
```

It returns when indexing finishes. Keep the lid open: an external drive can drop off during sleep.

**Resuming.** Any file already in the database is skipped, so re-running after an interruption continues where it stopped. Files previously recorded as unreadable are retried.

**Why index and classify are separate.** `index` does all the measuring and rendering once and stores the results. `classify` re-applies the naming and sizing rules purely from the database, in seconds. You never need to re-index to change how parts are categorised.

---

## 3. Where everything is written

**Your model library is never modified.** The indexer only reads. The exporter reads your originals and writes new files elsewhere.

| Location | Contents |
|---|---|
| `<data-dir>/library.db` | The parts database |
| `<data-dir>/thumbs/` | Thumbnails (about 20 kB each) |
| `<data-dir>/meshes/` | Preview meshes (about 50–60 kB each) |
| `<data-dir>/config.json` | Remembered library, bundle and sheet paths |
| `<data-dir>/clear_log.txt` | Everything *Clear Output Folder* removed |
| `<data-dir>/review.xlsx` | Review workbook (unless `--sheet` says otherwise) |
| `<bundles>/<name>/` | Exported bundles, each with a `manifest.txt` |

The data directory defaults to `~/.kitbash`. Deleting it loses only derived data and your review verdicts; re-running `index` rebuilds the rest. To keep several libraries apart, give each its own `--data-dir`.

---

## 4. Reading `stats`

```
LIBRARY: 1200 files scanned, 950 are real meshes
  usable single components : 600 (63% of meshes)
  whole figures (hidden)     : 60
  vehicle parts (hidden)     : 120
  sprues/sets (flagged)      : 15
  images mislabelled .stl    : 250
  genuinely unreadable       : 0
  unclassified -> review queue : 155
```

(Illustrative numbers.)

- **usable single components**: parts the builder can offer you.
- **whole figures**: complete miniatures. Hidden from the parts builder, shown in the Characters view.
- **vehicle parts**: turrets, hulls, tracks, sponsons. Hidden from the parts builder since they fit no figure, but offered in the Characters view.
- **sprues/sets**: files holding several parts at once, flagged.
- **images mislabelled .stl**: PNG or JPEG previews that some download sites ship with a `.stl` extension. Detected by content, not by name. Not an error.
- **genuinely unreadable**: corrupt or truncated files. Should be near zero.
- **unclassified**: the review queue, mostly whole figures or parts with unusual names.

Also shown: counts per slot, counts per lineage, and the range of pack scale factors. A very wide scale range is normal; it reflects how inconsistently creators export their files.

---

## 5. The GUI

```bash
python3 kitbash.py serve
```

Opens `http://localhost:8765`. Press Ctrl-C in the terminal to stop it. Python errors appear in that terminal, not in the browser.

### Top bar

The filters are a hierarchy, each narrowing the next:

```
Group  →  Faction  →  Sub-faction
```

- **Group**: broad allegiance above faction, so you can kitbash across a whole side at once. Factions with no group are listed under *Other*.
- **Faction**: from the folder layout, narrowed by group. The parts builder lists only factions with parts it can offer; the Characters view lists every faction, marking those with *(characters only)*.
- **Sub-faction**: a chapter, regiment, clan or similar division of a faction. It never affects scale or fit; it is only for browsing. Each sub-faction can list the alternative names sculptors actually use, so nothing on disk needs renaming.
- **Lineage**: one armour pattern or body type. Only lineages the builder can actually offer under the current filters are listed.
- **Build scale**: the anchor (see *Scaling*). The default is "match torso".
- **Review queue**: parts the classifier couldn't identify.

Each slot header shows how many parts are available **under the current filters**, and the numbers update as you change them.

### Slots

Twelve collapsible slots: head, torso, legs, left arm, right arm, left pauldron, right pauldron, backpack, ranged weapons, melee weapons, base, accessories.

- **Every slot is optional**, and **every slot takes several parts**.
- Click a slot header to expand it, type to search, and use **Load more** for further results.
- **Click a thumbnail** to select or deselect it. Selected parts appear as chips in the slot header.
- **Right-click or shift-click** a thumbnail to open it in 3D.

Parts whose filename shows a side go in the matching arm or pauldron slot; unsided parts appear in both, since most mirror. **Backpack means back-mounted**, so it also holds tails, wings and servo-arms.

### 3D viewer

Drag to rotate, scroll to zoom, click outside to close. Shows the pack, lineage, role, real dimensions in mm, triangle count and the scale being applied. The preview is simplified (about 12,000 triangles); raise `PREVIEW_TRIS` in `kitbash.py` for finer previews, which needs a re-index.

### Bundle panel

Selected parts are listed with their computed scale. Type a bundle name and press **Create Bundle** to write rescaled copies to `<bundles>/<name>/`, with a `manifest.txt` recording each part's source, lineage, scale and final size.

Each part's scale can be overridden in the box beside it; your values survive adding or removing other parts. Clear a box to return it to automatic.

**Clear Output Folder** deletes everything inside the bundle folder after a confirmation, logs each removed file to `clear_log.txt`, and refuses to follow links out of that folder.

### Characters view

The **Characters** tab picks complete models rather than components. Selecting one shows all its pieces, and **Export all pieces** copies them into a bundle at the model's own proportions.

A character is either a single complete STL, or a folder whose parts add up to a figure (at least three of head, torso, legs and arm, including a torso or legs). A folder holding several complete figures is listed as separate characters. Folders of loose components are left out. Vehicles and large walkers and monsters live **only** here, since their parts fit nothing else.

Exporting a character keeps **one copy of each part**:

- the **raw mesh**, not the supported one, because rescaling would rescale the supports too. Add supports in your slicer afterwards;
- the **newest revision** of each part, with older revisions filling gaps, since newer revision folders usually hold only the pieces that changed;
- **no pre-sliced printer plates**.

This applies at export only. Every copy stays visible and searchable in the parts builder.

### Review queue

Parts the classifier couldn't identify, with a thumbnail, faction and size. Click the picture to open it in 3D. Pick a slot from the dropdown, or **full character** for a file that turns out to be a finished model. Tags save immediately and `classify` never overwrites them.

---

## 6. The review sheet workflow

Filename rules get most of a library right; the review sheet handles the rest a folder at a time.

```bash
python3 kitbash.py make-sheet "/path/to/your/stls/Faction A"
# edit the sheet in Excel or LibreOffice, save, close
python3 kitbash.py import-review
python3 kitbash.py classify
```

`make-sheet` reads filenames only, so a whole faction takes seconds. Each row is one model folder, with the classifier's guess, the text that triggered it, and whether the files agree. Vehicles and accessory packs are pre-marked. Running it again for the same folder keeps any verdicts you have already written.

You edit three columns:

- **`your_lineage`**: your verdict. Leave it blank if the classifier was right. Accepts any lineage, plus `vehicle`, `accessory` or `various` (a mixed folder: judge each file on its own). **A folder's lineage applies to every file inside it**, because the folder is the model and the files are its pieces.
- **`faction`**: pre-filled from the folder's location. Correcting it moves the folder to that faction's filters *and* limits its lineage to that faction's own list. Write `Any` for things that belong to no army (bases, flight stands, generic bits); they then show up under every faction.
- **`notes`**: free text, but *"parts"*, *"full character"* and *"each file is a full character"* are read and decide what appears in the Characters view. A faction named in the notes also moves the folder.

A blank verdict records **no override**, so that folder keeps benefiting from later rule improvements. Verdicts are stored against folder paths relative to the library root, so the sheet still works if the drive mounts somewhere else.

**Close the workbook before running `make-sheet` or `dropdowns`.** Excel writes its own copy on save and would overwrite the changes.

**If the dropdowns disappear**, run `python3 kitbash.py dropdowns`. Excel rewrites the dropdown definitions into a format openpyxl can't read back, so any script that saves the file afterwards drops them; `make-sheet` re-applies them on every write.

`content-check` scores the parts-vs-characters prediction against the notes you have written so far. `content-check --apply "Sheet name"` fills only *empty* notes on that sheet, tagged `[predicted]` so you can find and review them.

---

## 7. Scaling

Most tabletop miniatures are **28 mm heroic** scale (about 1:56), measured foot-to-eye. Two corrections are applied.

**1. Export-scale correction, at index time.** Files arrive in wildly different units. Each pack gets one factor from the median of its confidently identified parts. If a part still lands far outside the expected size for its slot, it is re-estimated on its own and marked `*` in the bundle panel, since that usually means the pack mixes scales.

**2. Lineage matching, at bundle time.** Armour patterns and body types differ in **height** and, separately, in **bulk**. Two kinds of armoured trooper can stand at the same height with very different mass. The **torso you pick anchors the build**: other parts are scaled to its lineage, so heavier arms shrink onto a lighter torso. With no torso selected, it falls back to legs, then to the *Build scale* dropdown.

Bulk drives the warnings: a small difference passes quietly, a large one is flagged. Nothing is ever blocked. **Heads and pauldrons are directional**: they may come from equal or lighter armour, never heavier, because a light helmet fits a heavy collar but not the other way round.

To see every lineage with its height, bulk and whether it is characters-only, generate the reference:

```bash
python3 kitbash.py rules
```

---

## 8. How classification works

Every file gets a **slot**, a **faction**, a **lineage** and a few flags, worked out from its name, its folders and its measured size.

- **Slot** comes from keywords. The filename is trusted most, then keywords hidden at the end of run-together names (`Rlowerleg`, `upperarm`), then the parent folders. Negations are honoured, so `nohead` and `headless` never mean *head*. Weapon slots need the **filename** to say so, because a folder name like "squad with heavy weapons" describes what the models carry, not what each file is.
- **Faction** comes from the folder the file sits in (see the README's folder layout). In holding folders such as *Unsorted* or *Presupported*, it is inferred from the lineage instead.
- **Lineage** is the armour pattern or body type, and it is what decides scale. Keyword patterns are checked in order and the first match wins, so specific names sit above generic words. A lineage must also belong to the file's faction; if it doesn't, the faction's default is used, so one stray keyword can't put a part in the wrong army.
- **Flags** mark vehicle parts, sprues, whole figures, and very small-scale models, which are excluded because they would wreck the scale estimates.
- **Size checks** back up the names: a file too small for its lineage can't be a whole figure, and a folder's median part height (as a fraction of its lineage's height) decides whether it is a box of parts or a tray of finished models.

Your review-sheet verdicts and manual tags always win over all of this.

---

## 9. Roles

Role is tracked separately from armour, because a trooper in ordinary armour can still carry specialist gear: an engineer's servo-arm harness or a medic's kit. Six role archetypes (engineer, medic, priest, psyker, standard bearer and commander) are detected from names and are searchable. Their gear still goes in the normal slots: a servo-harness is a backpack, a ceremonial mace is a melee weapon.

---

## 10. Customising the rules

The rules are plain Python tables near the top of `kitbash.py`:

| Table | What it controls |
|---|---|
| `LINEAGE_HEIGHT`, `LINEAGE_BULK` | Reference height (mm) and bulk of each lineage |
| `LINEAGE_RULES` | Keyword patterns for each lineage, checked in order; first match wins |
| `SLOT_WORDS` | Keywords for each part slot |
| `VEHICLE_RULES` | Patterns that mark vehicle parts |
| `FACTION_ALIASES` | Maps your folder names to faction names |
| `FACTION_LINEAGES` | Which lineages each faction may use |
| `DEFAULT_LINEAGE_BY_FACTION` | The fallback when nothing matches |
| `CHARACTER_ONLY_LINEAGES` | Lineages that are always one whole model, kept out of the parts builder |

After editing, run `classify` (no re-index needed) and restart `serve`.

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| "numpy is required" but pip says it's installed | Run `doctor`. Either pip and python3 are different installs (use `python3 -m pip install`), or a broken copy of numpy sits earlier on the import path; doctor flags it. |
| "No library folder set" | Index once with a path, or pass `--library`. |
| "Library folder not found" | The drive isn't connected, or it mounts at a different path. Pass `--library` with the new path. |
| The page loads blank | Check the terminal running `serve` for a Python error. |
| Port already in use | `serve --port 9000`. |
| Indexing was interrupted | Run `index` again. |
| A part is in the wrong slot | Tag it in the review queue, or fix the folder on a review sheet. |
| A database from an older version | Upgraded automatically; you'll see a one-line "database upgraded" note. |
