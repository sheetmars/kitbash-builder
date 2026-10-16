#!/usr/bin/env python3
"""
kitbash.py — a parts library and kitbash builder for 3D-printable miniatures.

    python3 kitbash.py index "/path/to/stls"   # one-time heavy pass (reads meshes)
    python3 kitbash.py serve                   # the kitbash GUI, in your browser
    python3 kitbash.py classify                # fast, re-runnable from the database
    python3 kitbash.py stats                   # what the library currently looks like
    python3 kitbash.py doctor                  # diagnose a broken Python setup

OPTIONS (accepted by every command)
    --library DIR    root folder of your STL library   (or env KB_LIBRARY)
    --data-dir DIR   database, thumbnails, previews    (or env KB_DATA_DIR;
                                                        default ~/.kitbash)
    --bundles DIR    where exported bundles are written (or env KB_BUNDLES;
                                                        default <data-dir>/bundles)
    --sheet FILE     the review workbook (.xlsx)        (or env KB_SHEET;
                                                        default <data-dir>/review.xlsx)
    --port N         port for the GUI                   (or env KB_PORT; default 8765)

    The library, bundle and sheet locations are remembered in
    <data-dir>/config.json, so each only has to be given once.

DESIGN
    Reading a large mesh library is the expensive part, so `index` does all the
    measuring and rendering in a SINGLE pass and stores the results. `classify`
    then works purely from the database, so classification rules can be tuned
    and re-run in seconds without ever touching the drive again.

SCALE MODEL
    Tabletop miniatures are usually 28mm "heroic" scale (~1:56), measured
    foot-to-EYE rather than to the top of the head. Two independent
    corrections are applied:

    1. Export-scale correction (here, at index time). Files arrive in wildly
       different units: one sculptor's arm can measure several times larger
       than another sculptor's whole figure. Each pack gets one factor derived
       from its most confidently identified parts, putting every mesh at its
       true size in mm.

    2. Lineage matching (at bundle time). Armour patterns differ in height AND,
       separately, in bulk: Gravis and Tacticus stand alike but are nothing like
       the same mass. The chosen TORSO anchors the build and other parts scale
       to its lineage, so Gravis arms shrink onto a Firstborn torso. Heads and
       pauldrons may only come from equal or lighter armour, never heavier.

Nothing here modifies the source library. Everything is written to the data
directory (database + thumbnails + preview meshes) and the bundle folder.
"""

import collections
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Python puts this script's own folder first on the import path, so a stray
# file named numpy.py or PIL/ sitting next to it would shadow the
# real installed packages. Drop it — nothing here imports local modules.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path
               if os.path.abspath(p or os.getcwd()) != _HERE] or sys.path

# ------------------------------------------------------------------- paths
# Nothing about any particular drive or folder layout is baked in. Every path
# is resolved by configure() before a command runs: command-line flag first,
# then environment variable, then the value remembered in config.json, then a
# default inside the data directory.
DATA_DIR = LIBRARY_ROOT = BUNDLE_DIR = REVIEW_SHEET = ""
DB_PATH = THUMB_DIR = MESH_DIR = CLEAR_LOG = CONFIG_PATH = ""
PORT = 8765
GLOBAL_FLAGS = {"--library": "library", "--data-dir": "data_dir",
                "--bundles": "bundles", "--sheet": "sheet", "--port": "port"}


def _abs(p):
    return os.path.abspath(os.path.expanduser(str(p))) if p else ""


def configure(argv):
    """Resolve every path, strip the global options and return what is left."""
    global DATA_DIR, LIBRARY_ROOT, BUNDLE_DIR, REVIEW_SHEET, PORT
    global DB_PATH, THUMB_DIR, MESH_DIR, CLEAR_LOG, CONFIG_PATH
    opts, rest, i = {}, [], 0
    while i < len(argv):
        key, val = argv[i], None
        if "=" in key and key.split("=", 1)[0] in GLOBAL_FLAGS:
            key, val = key.split("=", 1)
        if key in GLOBAL_FLAGS:
            if val is None:
                if i + 1 >= len(argv):
                    sys.exit(f"{key} needs a value")
                i += 1
                val = argv[i]
            opts[GLOBAL_FLAGS[key]] = val
        else:
            rest.append(argv[i])
        i += 1

    DATA_DIR = _abs(opts.get("data_dir") or os.environ.get("KB_DATA_DIR")
                    or "~/.kitbash")
    CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
    saved = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        pass

    def pick(key, env, default=""):
        return _abs(opts.get(key) or os.environ.get(env) or saved.get(key)
                    or default)

    LIBRARY_ROOT = pick("library", "KB_LIBRARY")
    BUNDLE_DIR = pick("bundles", "KB_BUNDLES", os.path.join(DATA_DIR, "bundles"))
    REVIEW_SHEET = pick("sheet", "KB_SHEET", os.path.join(DATA_DIR, "review.xlsx"))
    PORT = int(opts.get("port") or os.environ.get("KB_PORT") or 8765)
    DB_PATH = os.path.join(DATA_DIR, "library.db")
    THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
    MESH_DIR = os.path.join(DATA_DIR, "meshes")
    CLEAR_LOG = os.path.join(DATA_DIR, "clear_log.txt")
    # Remember what was given, but never overwrite a good saved library path
    # with one that does not exist (a typo, or a drive that is not plugged in).
    if any(k in opts for k in ("library", "bundles", "sheet")) and (
            "library" not in opts or os.path.isdir(LIBRARY_ROOT)):
        remember_paths()
    return rest


def remember_paths():
    """Save the library, bundle and sheet locations so later runs need no flags."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"library": LIBRARY_ROOT, "bundles": BUNDLE_DIR,
                   "sheet": REVIEW_SHEET}, f, indent=2)


def require_library():
    """Stop with a clear message when no usable library folder is configured."""
    if not LIBRARY_ROOT:
        sys.exit('No library folder set. Give it once, e.g.\n'
                 '  python3 kitbash.py index "/path/to/your/stls"\n'
                 'or pass --library "/path/to/your/stls" (or set KB_LIBRARY).')
    if not os.path.isdir(LIBRARY_ROOT):
        sys.exit(f"Library folder not found: {LIBRARY_ROOT}\n"
                 "Is the drive connected? Use --library to point somewhere else.")


MESH_EXTS = (".stl",)
THUMB_PX = 256
PREVIEW_TRIS = 12000      # good silhouette in the 3D view; roughly 60 kB per part

# ---------------------------------------------------------------- reference
# Total model height in mm for each lineage, and each slot's share of it.
# Armour patterns differ in HEIGHT and, independently, in BULK. Gravis and
# Tacticus stand about the same height but are nothing like the same mass,
# which is why bulk is tracked separately: an Aggressor sits far closer to a
# Terminator than to an Intercessor, and compatibility warnings key off that.
LINEAGE_HEIGHT = {
    "firstborn": 32.0,        # Mk2-Mk8 power armour
    "phobos": 38.0,           # Reiver, Infiltrator, Incursor, Eliminator
    "tacticus": 38.0,         # Intercessor, Hellblaster, Bladeguard
    "gravis": 42.0,           # Aggressor, Eradicator, Heavy Intercessor, Inceptor
    "terminator": 42.0,       # Indomitus, Cataphractii, Tartaros, Aquilon
    "centurion": 44.0,        # Centurion warsuit
    "dreadnought": 65.0,      # Contemptor, Leviathan, Redemptor
    "bike": 40.0,
    "plague_marine": 34.0,    # Death Guard infantry - bloated, bulkier
    "tyranid_swarm": 15.0,    # Rippers, Spore Mines - many creatures per base
    "canoptek_scarab": 15.0,  # scarab swarms - many tiny creatures per base
    "gaunt": 28.0,            # Termagants, Hormagaunts, Gargoyles
    "genestealer": 32.0,      # Genestealers, Purestrains, Broodlord
    "acolyte": 30.0,          # Acolyte Hybrids, Metamorphs - hunched, four-armed
    "aberrant": 36.0,         # Aberrants, Abominant - hulking failed hybrids
    # A Harlequin is a Guardian in a costume: the same slender eldar frame.
    # Shared by Craftworlds, Drukhari and Harlequins - costume is identity,
    # which the faction filter already carries, not scale.
    "aeldari": 32.0,          # Guardians, Harlequins, Wyches, Kabalites
    # Wraithbone constructs, spirit-driven rather than piloted. None of them
    # may use the bare word "wraith": that belongs to the Necron Canoptek
    # Wraiths, so every one is spelled out in full.
    "aeldari_wraith": 45.0,     # Wraithguard, Wraithblades
    "aeldari_construct": 75.0,  # Wraithlord, Wraithseer, Drukhari Talos
    "aeldari_titanic": 150.0,   # Wraithknight
    "imperial_agent": 32.0,   # Assassins, interrogators, unarmoured retinue
    # T'au. The faction is mostly machines. Battlesuits are graded by CHASSIS,
    # since a Broadside is a Crisis suit carrying heavier guns rather than a
    # bigger machine.
    "drone": 18.0,            # MV/MB/DX floating discs
    "fire_warrior": 30.0,     # Strike Team, Breacher, Pathfinder, Fireblade
    "vespid": 30.0,           # winged insectoid auxiliaries
    "kroot": 34.0,            # bird-like mercenary auxiliaries, hunched
    "battlesuit_light": 40.0,   # XV25 Stealth, Shadowsun's XV22
    "battlesuit": 55.0,         # XV8 Crisis, XV86 Coldstar, XV88 Broadside
    "battlesuit_heavy": 75.0,   # XV95 Ghostkeel
    "battlesuit_super": 115.0,  # XV104 Riptide, XV107 R'varna
    "battlesuit_titanic": 220.0,  # KV128 Stormsurge, KX139 Ta'unar
    # An Inquisitor in power armour is a human wearing something heavier and
    # far more ornate than a Sister's: tabards, halo, rosarius, storm shield.
    # Taller than a bare agent, bulkier than Sororitas, short of Gravis.
    "inquisitor": 34.0,       # Coteaz, Hector Rex, Greyfax, Karamazov
    "sororitas": 32.0,        # Battle Sisters - power armour on a human frame
    # Custodes are genuinely taller than marines: above Gravis and Terminator
    # at 42, below a dreadnought at 65.
    # Guardsmen are numerically identical to `cultist` (28mm, bulk 0.8). They
    # get their own name anyway: the lineage list is a thing you READ, and
    # filing Imperial Guard under "cultist" is misleading in a way the
    # Genestealer Cult Neophytes are not - those really are cultists.
    "guardsman": 28.0,        # Infantry squads, Commissars, officers, psykers
    "scion": 30.0,            # Militarum Tempestus, Elysian storm troopers
    "ogryn": 45.0,            # Ogryns and Bullgryns
    "skitarii": 32.0,         # Rangers, Vanguard, Sicarians, Electro-Priests
    "tech_priest": 35.0,      # Dominus, Enginseer, Manipulus, Magos
    "kataphron": 40.0,        # Kataphron servitor-hulks on tracks
    "battle_automata": 60.0,  # Kastelan, Castellax, Scyllax, Thallax, Domitar
    "custodian": 45.0,        # Custodian Guard, Wardens, Shield-Captains
    "allarus": 50.0,          # Allarus and Aquilon Terminators
    "telemon": 85.0,          # Telemon Heavy Dreadnought
    # A walker with a condemned sister strapped to the front. Same class as a
    # dreadnought: piloted, and its parts fit nothing else.
    "penitent_engine": 75.0,  # Penitent Engine, Mortifier, Anchorite
    "tyranid_warrior": 45.0,  # Warriors, Raveners, Lictors, Zoanthropes, Hive Guard
    "tyranid_monstrous": 70.0,  # Carnifex, Trygon, Hive Tyrant, Exocrine
    "tyranid_gargantuan": 150.0,  # Hierophant, Harridan, Hierodule, Dominatrix
    "grot": 20.0,             # Grots, Gretchin, Runtherds
    "necron_warrior": 32.0,   # Warriors, Flayed Ones - slender skeletal
    "necron_immortal": 35.0,  # Immortals, Deathmarks, Lords, Crypteks
    "necron_lychguard": 38.0,  # Lychguard, Praetorians, Triarch
    "necron_destroyer": 42.0,  # Destroyers, Skorpekh, Ophydian, Wraiths
    "canoptek_construct": 60.0,  # Spyder, Tomb Stalker, Tomb Sentinel
    "ctan": 85.0,             # C'tan shards - star gods
    "cultist": 28.0,          # human traitors, poxwalkers, mutants
    "ork_boy": 32.0,          # Boyz, Kommandos, Stormboyz, Lootas, Weirdboyz
    "ork_nob": 36.0,          # Nobz, Meks, Painboyz
    "meganob": 42.0,          # mega armour, Big Mek in mega armour, Warbosses
    # Split from one 60mm bucket that held a Killa Kan (~55mm) and a Megadread
    # (~110mm) together, which also left Ghazghkull "taller" than every Ork
    # walker. Sizes are the tabletop models, not measured here: raw STL units
    # are per-sculptor and not comparable without a scale factor.
    "ork_walker": 70.0,       # Killa Kan, Deff Dread
    "ork_naut": 115.0,        # Megadread, Meka-Dread, Gorkanaut, Morkanaut
    "ork_stompa": 250.0,      # Stompa, Gargant
    # Knights and titans, graded by CHASSIS CLASS.
    "armiger": 110.0,          # Helverin, Warglaive, Moirax
    "knight_questoris": 150.0,  # Paladin, Errant, Warden, Gallant, Magaera
    "knight_cerastus": 165.0,   # Lancer, Castigator, Acheron, Atrapos
    "knight_dominus": 185.0,    # Castellan, Valiant, Porphyrion, Asterius
    "titan_scout": 280.0,       # Warhound, Wardog
    "titan_battle": 400.0,      # Reaver, Phantom, Revenant
    "titan_god": 600.0,         # Warlord, Emperor
    "ork_warlord": 70.0,      # Ghazghkull and other oversized named bosses
    "daemon_lesser": 34.0,    # Bloodletter, Plaguebearer, Horror, Daemonette
    "chaos_spawn": 45.0,      # amorphous mutated mass, 50mm base
    "daemon_engine": 75.0,    # Helbrute, Defiler, Maulerfiend, Forgefiend
    "daemon_prince": 70.0,    # ascended champion - above terminator, below greater
    "daemon_greater": 110.0,  # Bloodthirster, Great Unclean One, Lord of Change
    "dreadknight": 95.0,      # Nemesis Dreadknight - piloted GK walker
    "primarch": 60.0,         # mortal primarchs - Guilliman, Sanguinius, Dorn
    "daemon_primarch": 110.0,  # ascended - Magnus, Mortarion, Angron, Fulgrim
}
LINEAGE_BULK = {
    "firstborn": 1.0, "phobos": 0.9, "tacticus": 1.0, "gravis": 1.5,
    "terminator": 1.8, "centurion": 1.9, "dreadnought": 3.0, "bike": 1.2,
    "tyranid_swarm": 0.4, "gaunt": 0.7, "genestealer": 1.0,
    "acolyte": 1.0, "aberrant": 1.8,
    "aeldari": 0.8, "imperial_agent": 0.9, "sororitas": 1.0,
    "aeldari_wraith": 2.0, "aeldari_construct": 3.0, "aeldari_titanic": 5.0,
    "inquisitor": 1.4, "penitent_engine": 2.8,
    "custodian": 2.0, "allarus": 2.6, "telemon": 3.5,
    "skitarii": 0.9, "tech_priest": 1.2, "kataphron": 1.8, "battle_automata": 2.8,
    "guardsman": 0.8, "scion": 1.0, "ogryn": 2.2,
    "drone": 0.5, "fire_warrior": 0.9, "vespid": 0.8, "kroot": 1.0,
    "battlesuit_light": 1.6, "battlesuit": 2.2, "battlesuit_heavy": 3.0,
    "battlesuit_super": 4.0, "battlesuit_titanic": 6.5,
    "tyranid_warrior": 1.5, "tyranid_monstrous": 3.0, "tyranid_gargantuan": 5.0,
    "canoptek_scarab": 0.4, "necron_warrior": 0.9, "necron_immortal": 1.2,
    "necron_lychguard": 1.4, "necron_destroyer": 1.5,
    "canoptek_construct": 2.5, "ctan": 3.5,
    "grot": 0.5, "ork_boy": 1.2, "ork_nob": 1.6, "meganob": 2.2,
    "ork_walker": 2.8, "ork_warlord": 3.0,
    "ork_naut": 4.0, "ork_stompa": 7.0,
    "armiger": 4.0, "knight_questoris": 6.0, "knight_cerastus": 6.5,
    "knight_dominus": 8.0, "titan_scout": 12.0, "titan_battle": 18.0,
    "titan_god": 30.0,
    "plague_marine": 1.4, "cultist": 0.8, "daemon_lesser": 1.1,
    "chaos_spawn": 2.0, "daemon_engine": 3.2, "daemon_prince": 2.8,
    "daemon_greater": 4.0,
    "dreadknight": 3.5, "primarch": 2.5, "daemon_primarch": 4.0,
}
# What an unrecognised file is assumed to be. Unlabelled loyalist marine
# sculpts are most often Primaris-era, so "tacticus" is the safest guess.
# Change this if your own collection leans another way.
DEFAULT_LINEAGE = "tacticus"
# The fallback differs by faction. Chaos legions wear ten-thousand-year-old
# marks, so an unknown Chaos sculpt is firstborn.
DEFAULT_LINEAGE_BY_FACTION = {
    "Chaos Space Marines": "firstborn",
    # The Heresy is ten thousand years before Primaris armour existed, so an
    # unlabelled 30k marine is a Mk2-Mk4 firstborn by definition. Without this
    # the whole _30k tree inherited the loyalist default of `tacticus`.
    "Space Marines (Heresy)": "firstborn",
    "Chaos Daemons": "daemon_lesser",
    "Orks": "ork_boy",
    "Necrons": "necron_warrior",
    "Tyranids": "tyranid_warrior",
    # Acolyte hybrids are the commonest cult sculpt, so an unlabelled one is
    # more likely an acolyte than a purestrain.
    "Genestealer Cult": "acolyte",
    # Aeldari share one frame across all three factions, so the fallback is the
    # same for each.
    "Harlequins": "aeldari",
    "Aeldari Craftworlds": "aeldari",
    "Drukhari": "aeldari",
    "Imperial Auxilia": "imperial_agent",
    "Adepta Sororitas": "sororitas",
    "Adeptus Custodes": "custodian",
    "Adeptus Mechanicus": "skitarii",
    "Astra Militarum": "guardsman",
    "Knights": "knight_questoris",
    "Titans": "titan_battle",
    # The Crisis chassis is the commonest T'au sculpt, and packs that match
    # nothing are usually mech accessory sets.
    "Tau": "battlesuit",
}
# Lineages that are ALWAYS a complete model and never components, whatever the
# review notes say. A folder of dreadnought pieces is one dreadnought, not a
# bitz pack — so these never reach the parts builder and always reach Characters.
CHARACTER_ONLY_LINEAGES = {"dreadnought", "dreadknight", "daemon_engine",
                           "daemon_prince", "daemon_greater", "primarch",
                           "daemon_primarch", "chaos_spawn",
                           "ork_walker", "ork_naut", "ork_stompa", "ork_warlord",
                           "armiger", "knight_questoris", "knight_cerastus",
                           "knight_dominus", "titan_scout", "titan_battle",
                           "titan_god",
                           "canoptek_construct", "ctan",
                           "tyranid_gargantuan",
                           # Riptide-and-above sit at or past the dreadnought
                           # line. Smaller suits stay in the parts builder:
                           # battlesuits are modular by design, and swapping
                           # arms and weapon pods between them is how they are
                           # meant to be built.
                           "battlesuit_super", "battlesuit_titanic",
                           "penitent_engine",
                           "aeldari_construct", "aeldari_titanic",
                           "allarus", "telemon", "battle_automata"}
# EPIC scale is ~6mm for titan-scale battles. Mixing it into a 28mm library
# wrecks pack scale estimation, so it is excluded outright.
EPIC_PATTERN = re.compile(r"epic[ _-]?scale|\bepic\b|\b6\s?mm\b|\b8\s?mm\b|titanicus",
                          re.I)
# How far bulk may differ across one build before it is flagged. Gravis beside
# Terminator spans 0.3 and passes quietly; Firstborn beside Terminator spans
# 0.8 and is called out.
BULK_MILD, BULK_STRONG = 0.3, 0.8


def is_epic(path):
    """EPIC scale test, immune to the separator the folder happens to use.

    "\\bepic\\b" never matches "_Epic", because underscore is a WORD character
    so there is no boundary between them, and a folder such as "_Epic/" would
    sail straight through the filter. Normalise separators first, then test.
    """
    norm = re.sub(r"[_+\-]+", " ", str(path or ""))
    # A stated EPIC scale is decisive and beats everything below. An Ork Mega
    # Gargant may be labelled "Epic 40k proxy model 6mm" — it claims 40k and
    # is plainly 6mm, so the millimetres win.
    if re.search(r"epic[ _-]?scale|\b6\s?mm\b|\b8\s?mm\b|titanicus",
                 norm, re.I):
        return True
    # Otherwise an explicit tabletop scale outranks the bare word "epic". A
    # pack called "high res 40k scale_Epic size tank" uses "Epic" to mean
    # impressive rather than 6mm.
    if re.search(r"\b40k\b|\b28\s?mm\b|\b32\s?mm\b", norm, re.I):
        return False
    return bool(EPIC_PATTERN.search(norm))


# Lineages kept out of the parts builder. These are still indexed and still in
# the database — they are simply not offered as infantry components. Add "bike"
# here to hide bikes too; remove an entry to bring it back. No re-index needed.
# Hidden from the parts builder == character-only. One source of truth.
HIDDEN_LINEAGES = set(CHARACTER_ONLY_LINEAGES)

# Faction comes from the folder layout: the first folder under the library
# root names the faction (see faction_of). These aliases map common folder
# spellings onto the canonical faction names used everywhere else. Add your own
# folder names here; anything not listed is shown exactly as it is spelled.
FACTION_ALIASES = {
    "30k": "Space Marines (Heresy)",
    "Horus Heresy": "Space Marines (Heresy)",
    "Imperial Guard": "Astra Militarum",
    "Sisters of Battle": "Adepta Sororitas",
    "Custodes": "Adeptus Custodes",
    "Admech": "Adeptus Mechanicus",
    "Craftworlds": "Aeldari Craftworlds",
    "Eldar": "Aeldari Craftworlds",
    "Dark Eldar": "Drukhari",
    "Genestealer Cults": "Genestealer Cult",
    "T'au": "Tau",
    "Tau Empire": "Tau",
    "Imperial Knights": "Knights",
    "Chaos Knights": "Knights",
}


# ------------------------------------------------------- characters (item 4)
# Folder names that are packaging, not identity. Stripped when working out
# which folder represents one miniature.
CONTAINER_DIRS = {"files", "images", "image", "stl", "stls", "supported",
                  "presupported", "pre-supported", "unsupported", "lys",
                  "chitubox", "preview", "previews", "parts"}
# Some creators ship their whole print pipeline: CAD source, supported STLs and
# pre-sliced printer plates, times "Old" and "New" revisions. Those folders are
# packaging, not models — without this, a single big model shipped that way
# reads as a dozen separate packs. Spaces are REQUIRED around the step dash, because
# "6-8mm-extra-accessories-..." and "1234567 - sentry pylon proxy" are
# download folder names, not steps 6 and 1,234,567.
PIPELINE_DIR = re.compile(
    r"^\d(\.\d)?\s+[-–]\s+\S.*$"                       # "1 - Fusion", "1.5 - Dimensioned Parts"
    r"|^(old|new)([ _-](parts?|files?|new[ _-]parts?))?$"
    r"|^photon( files?)?$"
    r"|^(3d model parts?|modeled parts|fusion( 360)?( models| parts)?)$"
    r"|^(dimensioned( fusion)? parts?|parts with supports?|stl with supports?)$"
    r"|^(finished models|parts for printing|on_?build_?plates|pre_?assembled)$"
    r"|^all[_ ]files$"
    # "Plate 1".."Plate 5" and "Ready to Print" are arranged printer layouts,
    # not parts of the model.
    r"|^plate ?\d+$|^ready to print$|^print ?plates?$", re.I)


# Misspellings of the support markers ("Presuported") are common enough to be
# worth matching by shape.
SUPPORT_DIRS = {"supported", "unsupported", "presupported", "pre-supported"}
SUPPORT_DIR_RE = re.compile(r"^(un|pre)?[ _-]?su?p+orted$", re.I)


def strip_packaging(parts):
    """Drop packaging folders from a split path, returning the model folder.

    Two passes, because the two kinds of packaging nest differently. Pipeline
    folders are cut at the FIRST one found — "4 - Photon/Arms" has a perfectly
    ordinary leaf name, so trimming only from the right would keep it. Container
    folders are then popped from the right as before.

    This is the single definition of "which folder is one model". It used to be
    written out three times — model_folder, character_key and pack_of — which is
    exactly how they drifted apart.
    """
    parts = list(parts)
    for i, seg in enumerate(parts):
        seg = str(seg).strip()
        # A support marker is packaging wherever it sits, not only at the end.
        # "Kataphrons/Supported/Body" would otherwise leave "Body" as the model
        # folder and push the unit name out of the classifier's view entirely.
        if PIPELINE_DIR.match(seg) or seg.lower() in SUPPORT_DIRS \
                or SUPPORT_DIR_RE.match(seg):
            parts = parts[:i]
            break
    while parts and str(parts[-1]).lower() in CONTAINER_DIRS:
        parts.pop()
    return parts


# Which copy of a duplicated file to keep. Creators ship the same part several
# times over: raw mesh, the same mesh with support scaffolding fused into it,
# and a pre-sliced printer plate. This tool RESCALES every part, and rescaling
# a supported mesh rescales its supports too — they end up the wrong size for
# whatever orientation you finally print at. Supported copies are noticeably
# bigger on disk than the raw ones, which is the scaffolding. So the raw mesh
# wins, supports are a fallback, and slicer output is dropped outright.
SUPPORTED_DIR = re.compile(r"^(pre[- _]?)?supported$|with supports?$|"
                           r"^\d\s+[-–]\s+supported|supports?$", re.I)
PHOTON_DIR = re.compile(r"^\d\s+[-–]\s+photon|^photon( files?)?$", re.I)
# Revision folders, newest first. A creator who revises a model ships "Old",
# then "New Parts", then "New New Parts" — and the newer sets are PARTIAL, only
# the pieces that changed. So the newest copy of each part wins and older
# revisions fill the gaps, rather than one revision being taken wholesale.
REVISION_ORDER = ((re.compile(r"^new new", re.I), 0),
                  (re.compile(r"^new\b|^new$", re.I), 1),
                  (re.compile(r"^old\b|^old$", re.I), 3))


def variant_rank(path):
    """Sort key for competing copies of one part. Lower wins.

    Returns None for a file that should never be exported at all.
    """
    segs = [s.strip() for s in os.path.dirname(str(path)).split(os.sep)]
    if any(PHOTON_DIR.match(s) for s in segs):
        return None                      # pre-sliced printer output, not a model
    supported = any(SUPPORTED_DIR.match(s) for s in segs)
    revision = 2
    for seg in segs:
        for pat, score in REVISION_ORDER:
            if pat.match(seg):
                revision = min(revision, score)
    # raw mesh first, then newest revision; shallowest path breaks the tie, so a
    # file at the pack root beats a nested copy of the same thing
    return (1 if supported else 0, revision, len(segs), str(path).lower())


def variant_name(name):
    """Filename reduced to the part it depicts, ignoring revision decoration.

    Creators who ship revisions re-export the WHOLE model each time and prefix
    the filenames with underscores so a file browser sorts them: `Head.stl`,
    `_Head.stl`, `__Head.stl`. File sizes show these are one part: the raw
    copies are near-identical in size, and only the supported version, with
    scaffolding fused in, stands out. A trailing `_V2` is the same story: a
    revision of one part rather than two parts.
    """
    n = os.path.splitext(str(name).lower())[0]
    n = n.lstrip("_ -")
    n = re.sub(r"[ _-]*v\d+$", "", n)
    return re.sub(r"[ _-]+", " ", n).strip()


def prefer_variants(rows, path_of=lambda r: r["path"]):
    """Keep one copy of each part, choosing by variant_rank.

    Returns (kept_rows, dropped_count). Applied when a whole model is exported,
    never when browsing — every copy stays visible and searchable, so a
    supported version can still be picked deliberately.

    Revision-tolerant matching is used ONLY inside packs that actually carry
    pipeline folders. In an ordinary pack, `_arm.stl` beside `arm.stl` is far
    more likely to be two real parts than one part twice, so there the match
    stays exact.
    """
    rows = list(rows)
    fuzzy = any(PIPELINE_DIR.match(s.strip())
                for r in rows
                for s in os.path.dirname(str(path_of(r))).split(os.sep))
    best = {}
    dropped = 0
    for r in rows:
        p = path_of(r)
        rank = variant_rank(p)
        if rank is None:
            dropped += 1                 # pre-sliced printer output
            continue
        base = os.path.basename(str(p))
        key = variant_name(base) if fuzzy else base.lower()
        if key not in best:
            best[key] = (rank, r)
        else:
            dropped += 1
            if rank < best[key][0]:
                best[key] = (rank, r)
    return [r for _, r in best.values()], dropped
# Folders that hold loose components rather than a figure.
ACCESSORY_FOLDER = re.compile(
    r"accessor|bitz|\bbits\b|greeble|shoulder ?pads?|shoulder ?shield|"
    # "extras" plural only: the singular is a descriptor ("Battle Sister EXTRA
    # PIOUS" is a whole model), not a bitz pack.
    r"pauldron|conversion|spare|\bextras\b|decor|banner|^bases?$|marking|"
    r"iconograph|symbol|insignia|transfer|\bpads?\b|\bheads\b|\bweapons\b|"
    r"\barms\b|\blegs\b|\btorsos?\b|\bbodies\b|\bhelmets\b|upgrade|"
    r"weapon ?pack|decorations?|flight ?stands?|base ?toppers?|\bbasses\b|"
    r"\bbases\b|battle ?effects?", re.I)
# "Boyz ... with melee weapons" and "Neophytes ... including heavy weapons"
# describe what the models CARRY, not what the folder holds, and folders named
# that way are almost always whole models. The phrase is deleted before the
# accessory test rather than suppressing the whole test, so "Bitz with
# weapons" still reads as a bitz pack while "Boyz with weapons" no longer does.
ACCESSORY_LOADOUT = re.compile(r"\b(?:with|including|w)\b[^,]*\bweapons?\b", re.I)
# A banner BEARER is a model holding a banner, not a banner in a bitz box.
# "\bbearer\b" deliberately does not fire on "Plaguebearers", which is one word.
BEARER_WORD = re.compile(r"\bbearer\b", re.I)
CARRIED_ICON = re.compile(r"\b(?:banner|icons?|standards?)\b", re.I)


def strip_loadout(text):
    """Remove phrases that describe a model's wargear from a folder name.

    The accessory test reads folder names only — by design, because name-blind
    tests mislabel real characters — so the words have to be cleaned of loadout
    descriptions before they are trusted.
    """
    text = ACCESSORY_LOADOUT.sub(" ", text)
    if BEARER_WORD.search(text):
        text = CARRIED_ICON.sub(" ", text)
    return text


def norm_name(text):
    """Folder names separate words with spaces, hyphens, underscores and plus
    signs interchangeably. Normalise once rather than teaching every pattern
    about every separator — "shoulder-shields" must match "shoulder shield"."""
    return re.sub(r"\s+", " ", re.sub(r"[-_+.]+", " ", str(text or ""))).strip()


PRIMARCH_HINTS = {
    "mortal": (r"guilliman|gilbert prime|sanguinius|rogal dorn|\bdorn\b|leman russ|"
               r"jaghatai|khan|lion el|el'?jonson|ferrus manus|corvus corax|"
               r"alpharius|omegon|perturabo|lorgar|konrad curze|night haunter|"
               r"\bhorus\b|abaddon|vulkan|primarch"),
    "daemon": r"magnus|mortarion|angron|fulgrim",
}


def primarch_hint(text):
    """Return 'daemon'/'mortal' if a folder name looks like a primarch.

    Only ever a HINT written into the review sheet — never an assigned lineage.
    Names mislead constantly: "Vulkan" can be a backpack, "Russ" a tank, and
    "Horus" usually means the era rather than the primarch.
    """
    t = norm_name(text).lower()
    for kind in ("daemon", "mortal"):
        if re.search(PRIMARCH_HINTS[kind], t):
            return kind
    return None


CORE_SLOTS = {"head", "torso", "legs", "arm"}
# A figure needs most of its core parts. Three of four allows for a pack that
# ships, say, no separate head without disqualifying the whole character.
MIN_CORE_SLOTS = 3
# A complete miniature stands at least this fraction of its lineage's height.
# Names lie constantly ("Culverins Supported.stl"), but a 12mm weapon cannot
# pass for a 32mm marine, so measured size is the reliable test.
MIN_FIGURE_FRACTION = 0.45
# Any halfway-confident part match rules a file out as a character. Only files
# nothing recognised as a component can stand alone as a miniature.
COMPONENT_CONF = 0.45


def looks_like_figure(row):
    """Is this single file plausibly a whole miniature rather than a part?

    Filenames are unreliable in both directions — "Culverins Supported.stl" is a
    weapon, while a genuine figure may be called "grav-captin4.stl" — so this
    leans on measured size plus whether anything recognised it as a component.
    """
    sc = row["scale_factor"] or 1.0
    mx = max(row["dim_x"] or 0, row["dim_y"] or 0, row["dim_z"] or 0) * sc
    exp = LINEAGE_HEIGHT.get(row["lineage"] or "firstborn", 32.0)
    if exp and mx < MIN_FIGURE_FRACTION * exp:
        return False                       # far too small to be a figure
    if row["is_sprue"]:
        return False                       # a rack of parts, not a miniature
    slot = row["user_slot"] or row["slot"]
    if slot and (row["slot_conf"] or 0) >= COMPONENT_CONF:
        return False                       # recognised as a component
    return True


def character_key(path, root):
    """The folder that represents one miniature, ignoring packaging folders."""
    try:
        rel = os.path.relpath(path, root).split(os.sep)[:-1]
    except ValueError:
        return ""
    return os.sep.join(strip_packaging(rel))


def pretty_label(key):
    """Human-readable name for a model folder or file.

    Some packs are stored under a folder whose whole name is the download id —
    ".../Assassin_Callidus/43007135". Stripping the id leaves nothing, so fall
    back to the PARENT segment, which is where the real name lives. Display
    only: the folder key that review verdicts are stored against is untouched.
    """
    parts = str(key).split(os.sep) if key else []
    for seg in reversed(parts[-2:]) if parts else ["(root)"]:
        last = re.sub(r"\.(stl|obj)$", "", seg, flags=re.I)
        last = last.replace("+", " ").replace("_", " ")
        last = re.sub(r"\s*\d{8}[-\w]*$", "", last)  # trailing download ids
        last = re.sub(r"\s+", " ", last).strip()
        if last:
            return last
    return key


# Broad allegiance above faction, so you can kitbash across everything Imperial
# without losing the ability to isolate one faction. Derived from faction rather
# than stored, so editing this table and re-running `classify` is enough.
#
# NOTE: the Chaos side needs refining when we get there - Death Guard, Thousand
# Sons, World Eaters and Emperor's Children are Chaos Marine LEGIONS, not peers
# of "Chaos Space Marines", even though the folders list them side by side.
FACTION_GROUPS = {
    # Imperium
    "Space Marines": "Imperium", "Space Marines (Heresy)": "Imperium",
    "Grey Knights": "Imperium", "Adeptus Custodes": "Imperium",
    "Adepta Sororitas": "Imperium", "Astra Militarum": "Imperium",
    "Adeptus Mechanicus": "Imperium", "Imperial Auxilia": "Imperium",
    "Knights": "Imperium",              # includes Chaos Knights - see SUBFACTIONS
    "Titans": "Imperium",               # Aeldari and Necron titans move by faction column
    # Chaos — the legions are SUB-factions of Chaos Space Marines, not peers
    "Chaos Space Marines": "Chaos", "Chaos Daemons": "Chaos", "Chaos": "Chaos",
    # Xenos
    "Orks": "Xenos", "Necrons": "Xenos", "Tyranids": "Xenos", "Tau": "Xenos",
    "Aeldari Craftworlds": "Xenos", "Drukhari": "Xenos",
    "Harlequins": "Xenos", "Genestealer Cult": "Xenos",
}
UNGROUPED = "Other"


# Which lineages a faction may legitimately use. A faction absent from this
# table is UNCONSTRAINED — that is deliberate, so a faction nobody has
# reviewed yet is not silently forced into a default.
#
# This is what stops the overlap: without it a single stray filename can put
# `firstborn` or `daemon_lesser` in the T'au list, and a Necron "Seeker" or an
# Ork "Mk2" can reach for a Space Marine lineage. A lineage outside its
# faction's set falls back to that faction's default rather than standing.
#
# `bike` is shared on purpose across Space Marine, Ork, Necron and Genestealer
# Cult, because a rider sizes to the machine.
_MARINE = {"firstborn", "phobos", "tacticus", "gravis", "terminator",
           "centurion", "dreadnought", "bike", "primarch"}
FACTION_LINEAGES = {
    "Space Marines": _MARINE,
    "Space Marines (Heresy)": _MARINE,
    "Grey Knights": _MARINE | {"dreadknight"},
    # `dreadnought` stays in the set: the Custodes Achillus and Galatus really
    # are Contemptor-pattern, and share that chassis and size. Only the heavy
    # Telemon is split out. `sororitas` covers the Sisters of Silence helmets.
    "Astra Militarum": {"guardsman", "scion", "ogryn", "bike", "cultist",
                        "imperial_agent", "inquisitor", "skitarii",
                        "tech_priest", "battle_automata"},
    "Adeptus Mechanicus": {"skitarii", "tech_priest", "kataphron",
                           "battle_automata", "cultist", "imperial_agent", "bike"},
    "Adeptus Custodes": {"custodian", "allarus", "telemon", "dreadnought",
                         "bike", "sororitas", "imperial_agent"},
    "Imperial Auxilia": {"imperial_agent", "inquisitor", "sororitas",
                         "penitent_engine", "cultist", "bike"},
    "Adepta Sororitas": {"sororitas", "penitent_engine", "imperial_agent",
                         "inquisitor", "cultist", "bike"},
    "Chaos Space Marines": _MARINE | {
        "plague_marine", "cultist", "chaos_spawn", "daemon_lesser",
        "daemon_prince", "daemon_greater", "daemon_engine", "daemon_primarch"},
    "Chaos Daemons": {"daemon_lesser", "daemon_greater", "daemon_prince",
                      "daemon_engine", "daemon_primarch", "chaos_spawn", "cultist"},
    # Warhound titans are often filed alongside Knights, so titan_scout is here.
    "Knights": {"armiger", "knight_questoris", "knight_cerastus",
                "knight_dominus", "titan_scout"},
    "Titans": {"titan_scout", "titan_battle", "titan_god"},
    "Orks": {"grot", "ork_boy", "ork_nob", "meganob", "ork_walker",
             "ork_naut", "ork_stompa", "ork_warlord", "bike"},
    "Necrons": {"canoptek_scarab", "necron_warrior", "necron_immortal",
                "necron_lychguard", "necron_destroyer", "canoptek_construct",
                "ctan", "titan_battle", "bike"},
    "Tyranids": {"tyranid_swarm", "gaunt", "genestealer", "tyranid_warrior",
                 "tyranid_monstrous", "tyranid_gargantuan"},
    "Genestealer Cult": {"acolyte", "aberrant", "cultist", "genestealer", "bike"},
    "Tau": {"drone", "fire_warrior", "vespid", "kroot", "battlesuit_light",
            "battlesuit", "battlesuit_heavy", "battlesuit_super",
            "battlesuit_titanic", "bike"},
    "Harlequins": {"aeldari", "bike"},
    # titan_battle covers the Phantom and Revenant, which dwarf a Wraithknight
    "Aeldari Craftworlds": {"aeldari", "aeldari_wraith", "aeldari_construct",
                            "aeldari_titanic", "titan_battle", "bike"},
    "Drukhari": {"aeldari", "aeldari_wraith", "aeldari_construct", "bike"},
}


# Folders that are a holding pen rather than a faction. Their location tells
# you nothing about what a model IS, so the faction column cannot be pre-filled
# from the path the way it is everywhere else.
# Write this in a review sheet's faction column for something that belongs to
# no army in particular — bases, flight stands, generic bitz. Every faction and
# group filter includes it alongside its own, so a base shows up whatever you
# are building.
ANY_FACTION = "Any"

HOLDING_FOLDERS = {"Presupported", "Unsorted", "Uploads", "Proxies", "Misc",
                   "Forge World", "Accessories", "Terrain", "Specialist Games"}
# When a lineage belongs to exactly one faction, it identifies the faction on
# its own — a `battlesuit` is T'au wherever the file happens to sit. Marine
# patterns are shared with Chaos and the Heresy, so those fall back to the
# commonest owner and you correct them on the sheet.
_AMBIGUOUS_OWNER = {
    "firstborn": "Space Marines", "phobos": "Space Marines",
    "tacticus": "Space Marines", "gravis": "Space Marines",
    "terminator": "Space Marines", "centurion": "Space Marines",
    "dreadnought": "Space Marines", "primarch": "Space Marines",
    "bike": "Space Marines", "genestealer": "Tyranids",
    "cultist": "Chaos Space Marines", "imperial_agent": "Imperial Auxilia",
    "inquisitor": "Imperial Auxilia", "sororitas": "Adepta Sororitas",
    "aeldari": "Aeldari Craftworlds", "aeldari_wraith": "Aeldari Craftworlds",
    "aeldari_construct": "Aeldari Craftworlds",
    "aeldari_titanic": "Aeldari Craftworlds",
    # attached to Guard regiments as well as their own faction
    "skitarii": "Adeptus Mechanicus", "tech_priest": "Adeptus Mechanicus",
    "battle_automata": "Adeptus Mechanicus",
}


def faction_from_lineage(lineage):
    """Which faction a lineage implies, or None when it says nothing useful."""
    if not lineage:
        return None
    owners = [f for f, s in FACTION_LINEAGES.items() if lineage in s]
    if len(owners) == 1:
        return owners[0]
    return _AMBIGUOUS_OWNER.get(lineage)


def lineage_for_faction(faction, lineage):
    """Keep a lineage only if it belongs to the faction; otherwise the default."""
    allowed = FACTION_LINEAGES.get(faction or "")
    if not allowed or lineage in allowed:
        return lineage
    return DEFAULT_LINEAGE_BY_FACTION.get(faction, DEFAULT_LINEAGE)


def group_of(faction):
    if not faction:
        return None
    return FACTION_GROUPS.get(faction, UNGROUPED)


def factions_in_group(group):
    return [f for f, g in FACTION_GROUPS.items() if g == group]


# The third level: chapters for loyalists, legions for Chaos. These are real
# categories, not residuals — every World Eater IS a Chaos Space Marine, so the
# legion belongs BELOW the faction rather than beside it. Sub-faction never
# affects scale or fit; it exists purely for browsing.
SUBFACTIONS = {
    "Space Marines": [
        "Space Wolves", "Dark Angels", "Blood Angels", "Salamanders",
        "Black Templars", "Iron Hands", "Raven Guard", "Ultramarines",
        "Imperial Fists", "Deathwatch", "Minotaurs", "Carcharodons",
        "Flesh Tearers", "Legion of the Damned", "White Scars",
        "Crimson Fists", "Howling Griffons", "Grey Slayers",
    ],
    "Chaos Space Marines": [
        "Death Guard", "Thousand Sons", "Emperor's Children", "Iron Warriors",
        "World Eaters", "Alpha Legion", "Night Lords", "Word Bearers",
        "Black Legion", "Red Corsairs",
    ],
    # Regiments, which behave exactly like chapters do for the marines.
    "Astra Militarum": [
        "Death Korps of Krieg", "Cadian", "Elysian", "Steel Legion",
        "Valhallan", "Militarum Tempestus",
    ],
    # Same chassis, different allegiance — a sub-faction, not a faction.
    "Knights": ["Imperial Knights", "Chaos Knights"],
    # Imperial Auxilia is a holding pen for the Imperium's non-marine humans.
    # These three are the real organisations inside it. Adepta Sororitas is
    # also a faction of its own — writing that faction name in a review note
    # moves a pack there, which is the mechanism for anything filed in the
    # wrong tree.
    "Imperial Auxilia": [
        "Officio Assassinorum", "Inquisition", "Adepta Sororitas",
    ],
}
# A sub-faction is almost never spelled out in a folder name: sculptors write
# "Vindicare" or "Coteaz", never "Officio Assassinorum". Aliases let a
# sub-faction be recognised from the names actually used, so the browsing
# hierarchy works without renaming a single folder. Craftworlds (Ulthwé,
# Biel-Tan, Saim-Hann) and Sororitas Orders can be added the same way.
SUBFACTION_ALIASES = {
    "Chaos Knights":   r"chaos ?knight|\bnurgle\b|\bchaos\b|unordnung|unordentlich",
    "Imperial Knights": r"imperial ?knight|questoris|cerastus|armiger|moirax",
    "Death Korps of Krieg": r"\bkrieg\b|\bdkok\b|\btrench|\bshovel\b|expendable",
    "Cadian":               r"cadian|spaceguard",
    "Elysian":              r"elysian",
    "Steel Legion":         r"steel ?legion|legion of metal",
    "Valhallan":            r"\bvalhalla",
    "Militarum Tempestus":  r"tempestus|\bscion|storm ?trooper",
    "Officio Assassinorum": r"vindicare|callidus|culexus|culista|eversor|"
                            r"assassin|\bcali ?girl\b",
    "Inquisition":          r"inquisitor|interrogator|coteaz|"
                            r"ordo (?:malleus|hereticus|xenos)|daemonhunter|"
                            r"demonhunter",
    "Adepta Sororitas":     r"sororitas|\bsister\b|\bsisters\b|repentia|"
                            r"seraphim|celestian|canoness|\bpious\b",
}


def _is_faction_folder(name):
    name = FACTION_ALIASES.get(name, name)
    return name in FACTION_GROUPS or name in FACTION_LINEAGES or name in HOLDING_FOLDERS


def faction_of(path):
    """Faction name derived from where the file sits in the library.

    The first folder under the library root names the faction. A library split
    across container folders ("Drive A/Orks/...") also works: when the first
    folder is not a recognised faction but the one inside it is, that one wins.
    """
    if not LIBRARY_ROOT:
        return None
    try:
        rel = os.path.relpath(path, LIBRARY_ROOT).split(os.sep)
    except ValueError:
        return None
    if not rel or rel[0] in ("", ".") or rel[0].startswith(".."):
        return None
    name = rel[0]
    if (len(rel) > 2 and not _is_faction_folder(rel[0])
            and _is_faction_folder(rel[1])):
        name = rel[1]
    # The Chaos folder mixes two real factions with several legions. Collapse it
    # so the legions become sub-factions rather than peers of their own parent.
    if name == "Chaos":
        seg = [s.lower() for s in rel]
        return "Chaos Daemons" if "daemons" in seg else "Chaos Space Marines"
    return FACTION_ALIASES.get(name, name)


def subfaction_of(path, faction=None):
    """Chapter or legion, matched per path segment so names cannot bleed across
    folder boundaries."""
    faction = faction or faction_of(path)
    names = SUBFACTIONS.get(faction or "", [])
    if not names:
        return None
    segs = [norm_name(s).lower() for s in str(path).split(os.sep)]
    for sf in names:
        n = norm_name(sf).lower()
        if any(n in s for s in segs):
            return sf
    # Fall back to the names sculptors actually use. Matched per segment for
    # the same reason as above: a name must not bleed across folder boundaries.
    for sf in names:
        pat = SUBFACTION_ALIASES.get(sf)
        if pat and any(re.search(pat, s) for s in segs):
            return sf
    return None
# Bulk compatibility is DIRECTIONAL for some slots. A Tacticus helmet sits fine
# inside a Gravis or Terminator collar, but a Terminator helmet on a Tacticus
# body is obviously oversized — so heads may come from equal or lighter armour
# only. Weapons and accessories are handheld or bolted on, so they go anywhere.
SLOT_BULK_POLICY = {
    "head": "down_only", "pauldron": "down_only",
    "weapon_ranged": "any", "weapon_melee": "any", "accessory": "any", "base": "any",
    "torso": "match", "legs": "match", "arm": "match", "backpack": "match",
}
# Expected longest dimension of a part, as a fraction of total model height.

# Expected longest dimension of a part, as a fraction of total model height.
SLOT_FRACTION = {
    "head": 0.22, "torso": 0.44, "legs": 0.50, "arm": 0.36,
    "backpack": 0.34, "weapon_ranged": 0.38, "weapon_melee": 0.45,
    "pauldron": 0.25, "base": 1.00, "accessory": 0.15,
}


def expected_mm(slot, lineage):
    h = LINEAGE_HEIGHT.get(lineage, 32.0)
    if slot == "base":
        return 32.0 if LINEAGE_BULK.get(lineage, 1.0) <= 1.2 else 40.0
    return h * SLOT_FRACTION.get(slot, 0.3)


# --------------------------------------------------------------- vocabulary
def tokens(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)      # camelCase  -> camel Case
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)   # LArm/RArm  -> L Arm
    s = re.sub(r"[^A-Za-z]+", " ", s).lower()
    out = []
    for t in s.split():
        if t.endswith("ies"):
            t = t[:-3] + "y"
        elif t.endswith("es") and len(t) > 4:
            t = t[:-2]
        elif t.endswith("s") and len(t) > 3:
            t = t[:-1]
        out.append(t)
    return out


SLOT_WORDS = {
    "head": "head helm helmet face skull hood mask".split(),
    "torso": "torso body chest cuirass plastron abdomen".split(),
    # "wrist" and "bicep" join the arm; "palm" deliberately does NOT — in a
    # library with terrain it matches palm TREES far more often than hands.
    "arm": "arm shoulder hand fist gauntlet forearm elbow wrist bicep".split(),
    # shin/calf/ankle were missing, which left battlesuit and dreadnought legs
    # unclassified: "Left Shin", "Redemptor calf", "_KX139_AnkleJoint".
    "legs": "leg thigh boot feet foot knee waist hip shin calf ankle".split(),
    # "harness" catches the Techmarine servo-harness, which is worn on the back
    # Tails and wings mount on the back, so they share this slot: a Tyranid
    # has no backpack but its tail occupies the same place on the model.
    # Mechanicus models wear their servo-arms and mechadendrites on the back,
    # the same place a Techmarine wears a servo-harness. "mechandrit" is a
    # common sculptor misspelling.
    "backpack": "backpack jumppack jetpack powerpack harness tail wing "
                "wings tails mechadendrite mechandrite mechandrit dendrite "
                "servo".split(),
    "pauldron": "pauldron shoulderpad spaulder spad shldr".split(),
    "weapon_ranged": ("bolter gun rifle pistol cannon melta plasma flamer launcher "
                      "sniper lascannon volkite stubber autocannon bolt storm weapon "
                      "gunarm weaponarm").split(),
    # "fist" deliberately excluded: a bare fist is a hand, not a weapon
    "weapon_melee": ("sword axe hammer claw blade chainsword staff spear talon glaive "
                     "mace lance halberd knife dagger crozius shield buckler").split(),
    # "pillar" is how some packs ship their scenic plinth
    "base": "base plinth scenic pillar column".split(),
    # The second line is machine greeblery — fins, vents, sensors and the
    # connectors that hold a battlesuit together. Without it, mech kits have no
    # slot at all and fill the review queue.
    "accessory": ("pouch grenade purity seal banner icon tabard cape cloak scroll "
                  "bit bitz greeble crest tilt holster sheath ammo narthecium "
                  "reductor rosarius hood halo book rivet chain laurel wreath tassel parchment "
                  "fin vent antenna anntena antennae sensor connector joint cover "
                  "spinner vane cable piston hose canister nozzle gizmo").split(),
}
# Sculptors routinely run words together with no separator the tokeniser can
# see — "Minertorso001", "GKHEAD1", "piousvhead", "COMBIBOLTER". Exact token
# matching misses every one of these, and the file then falls through to folder
# evidence, which is usually the model's loadout rather than the part.
# Matching a slot word as a SUFFIX recovers most of them and is right for all
# but the exclusions below. Suffix-only is deliberate:
# substring matching would read "armour" as an arm and "based" as a base.
# Three-letter keywords are allowed only where a 6+ character token ending in
# them is reliably that part: "Rlowerleg", "upperarm", "railgun". "hip", "bit"
# and "axe" are excluded — they would read warship as a hip and rabbit as a bit.
_SUFFIX_SHORT_OK = {"arm", "leg", "gun"}
_SUFFIX_SLOT = sorted(
    ((w, slot) for slot, words in SLOT_WORDS.items() for w in words
     if len(w) >= 4 or w in _SUFFIX_SHORT_OK),
    key=lambda x: -len(x[0]))
# Words that merely END with a slot word and mean something else entirely
# ("detail" is not a tail).
_SUFFIX_FALSE_FRIENDS = {"detail", "retail", "cocktail", "portrait", "ponytail"}


def suffix_slot_words(toks):
    """Slot keywords hidden at the end of a run-on token.

    Returns a pseudo-token list so the caller can score it exactly like a real
    one. Only tokens of 6+ characters are considered, so a token that IS the
    keyword is left to ordinary matching.
    """
    out = []
    for t in toks:
        if len(t) < 6 or t in _SUFFIX_FALSE_FRIENDS:
            continue
        for w, _slot in _SUFFIX_SLOT:
            if t != w and t.endswith(w):
                out.append(w)
                break
    return out


def _vocab(words):
    """Store each keyword in BOTH raw and stemmed form.

    Filenames get stemmed before matching, so a vocabulary holding only the raw
    word silently fails on anything the stemmer alters: "harness" becomes
    "harnes" and "crozius" becomes "croziu", which would leave a Techmarine
    servo-harness unclassified.
    """
    out = set()
    for w in words:
        out.add(w)
        out.update(tokens(w))
    return out


SLOT_WORDS = {k: _vocab(v) for k, v in SLOT_WORDS.items()}

# Words meaning "this file is a complete model". Deliberately excludes
# "supported"/"presupported" (that describes print supports, not completeness —
# it was marking every supported weapon as a finished figure) and "sprue",
# "preassembled", "all", which described the pack rather than the file.
WHOLE_WORDS = _vocab(("full complete whole assembled bundle collection merged "
                      "miniature figure combined").split())
SPRUE_WORDS = _vocab("pair set kit rack plate sheet lot batch multipack sprue "
                     "preassembled".split())
# negations: "nohead", "no head", "headless", "bodiless"
NEG_PATTERNS = [r"no[_ -]?%s", r"%sless", r"without[_ -]?%s", r"missing[_ -]?%s"]

# Checked IN ORDER — most specific first. Ordering carries real meaning:
# "Heavy Intercessor" must reach the gravis rule before the tacticus rule sees
# the word "intercessor" and mis-sizes it.
LINEAGE_RULES = [
    ("dreadknight",  r"dreadknight|nemesis dread"),
    # hel+ tolerates both spellings — GW writes Heldrake, sculptors often
    # double the L. Blight-hauler and Plagueburst are Nurgle daemon engines.
    ("daemon_engine", r"helbrute|hellbrute|maulerfiend|forgefiend|venomcrawler|"
                      r"defiler|da?emon ?engine|hellforged|discordant|helstalker|"
                      r"hel+drake|hel+talon|hel+blade|blight.?hauler|plagueburst|"
                      r"myphitic|brass scorpion|decimator|blood ?slaughterer|"
                      r"slaughterer|decay carrier"),
    # ---- Titans and Knights ----------------------------------------------
    # Every one is qualified with its class word. Bare forms are landmines:
    # "warlord" is also Ghazghkull, "emperor" is Emperor's Children, "reaver"
    # catches Chaos Reavers, "nemesis" is a Grey Knight force weapon, and
    # "imperator" is common in terrain packs.
    ("titan_god",   r"warlord ?titan|emperor ?titan|imperator ?titan|"
                    r"lucius ?(?:pattern|warlord)|warbringer"),
    ("titan_battle", r"reaver ?titan|phantom ?titan|revenant ?titan|"
                     r"\brevenant\b|\bphantom\b"),
    ("titan_scout", r"warhound|\bwardog\b"),
    ("knight_dominus", r"knight ?dominus|dominus ?knight|castellan ?knight|"
                       r"knight ?castellan|knight ?valiant|porphyrion|asterius|"
                       r"acastus"),
    # "TDC" / "Tall Dominion" is a sculptors' Cerastus proxy name, often the
    # only identifying word near the file; without it those files hit the
    # power-armour MK rule on "MK3". The fuller "Dominion Crusader" is NOT
    # usable — the same sculptor uses it for Knights in general.
    ("knight_cerastus", r"cerastus|\bacheron\b|\batrapos\b|knight ?lancer|"
                        r"\bcastigator\b|tall ?dominion|\btdc\b"),
    ("knight_questoris", r"questoris|\bmagaera\b|\bstyrix\b|knight ?paladin|"
                         r"knight ?errant|knight ?warden|knight ?gallant|"
                         r"knight ?preceptor"),
    # "armour bearer" / "armored bear" / "pretty squire" are proxy names for
    # an Armiger.
    ("armiger",     r"\barmiger\b|helverin|warglaive|\bmoirax\b|"
                    r"armou?r.?bearer|armou?red ?bear|pretty.?squire"),
    # Before `dreadnought`, or the Telemon reads as a Contemptor. The generic
    # Custodes rule sits much lower, so "Interstellar Janitorial Contemptor"
    # still lands on `dreadnought` rather than on `custodian`.
    # "castle pattern" is a proxy name for a Castellax.
    ("battle_automata", r"kastelan|castellax|scyllax|thallax|domitar|\bferrum\b|"
                        r"vulturax|\bambot\b|\bautomata\b|robo ?warrior|"
                        r"castle ?pattern|robo ?trooper|\brobut\b"),
    ("kataphron",   r"kataphron"),
    # "golden tower of power mausoleum walker" is a proxy name for the Telemon.
    ("telemon",     r"\btelemon\b|golden ?tower ?of ?power|mausoleum ?walker"),
    ("allarus",     r"\ballarus\b|\baquilon\b"),
    ("dreadnought", r"dreadnought|contemptor|leviathan|redemptor|deredeo|mortis|"
                    r"furioso|sarcophag|invictor"),
    # daemon primarchs first: "fulgrim-daemon-prince" must not read as a
    # generic greater daemon
    ("daemon_primarch", r"magnus|mortarion|angron|fulgrim"),
    # a Daemon Prince is an ascended champion: bigger than a terminator,
    # far short of a Bloodthirster, and smaller than a daemon primarch
    ("daemon_prince", r"da?emon ?prince|prince ?da?emon|da?emon ?lord"),
    ("daemon_greater", r"bloodthirster|great unclean|lord of change|"
                       r"keeper of secrets|greater da?emon"),
    ("tyranid_gargantuan", r"hierophant|harridan|hierodule|hyrodule|dimachaeron|"
                           r"\bnorn\b|dominatrix"),
    ("tyranid_monstrous", r"carnifex|screamer ?killer|trygon|mawloc|exocrine|"
                          r"haruspex|tyrannofex|tervigon|toxicrene|maleceptor|"
                          r"hive ?tyrant|swarmlord|neurotyrant|\bharpy|hive ?crone|"
                          r"harrifex|hive ?lord"),
    ("tyranid_warrior", r"tyranid ?warrior|ravener|lictor|zoan?trope|venomthrope|"
                        r"hive ?guard|hiveguard|tyrant ?guard|biovore|pyrovore|"
                        r"malanthrope|neurothrope|\bthrope\b"),
    # "alien thief" and "gene steal" are sculptor codewords for a purestrain.
    # They sit HERE rather than with the acolyte rules because packs using
    # them often nest the word "Genestealer" too far up the tree for the
    # name+parent+grandparent window to see it.
    ("genestealer",    r"genestealer|purestrain|broodlord|\bstealer|"
                       r"alien.?thief|gene.?steal"),
    # "ga+unt" needs the trailing boundary or it eats GAUNTLET, and every
    # armoured hand in every faction reads as a Termagant.
    ("gaunt",          r"termagant|hormagaunt|ga+unts?\b|gargoyle|\bgant\b"),
    ("tyranid_swarm",  r"ripper|spore ?mine"),
    # ABOVE the Necron block on purpose. A Wraithguard pack folder called
    # something like "guard-of-wraiths" contains the plural, which the Necron
    # rule matches, and the faction constraint would then quietly demote it to
    # plain `aeldari`. A named Aeldari construct must win over a generic word.
    ("aeldari_titanic",   r"wraith ?knight"),
    ("aeldari_construct", r"wraith ?lord|wraith ?seer|\btalos\b|\bcronos\b"),
    ("aeldari_wraith",    r"wraith ?guard|wraith ?blade"),
    ("ctan",            r"c'?tan|transcendent|nightbringer|deceiver|void ?dragon|"
                        r"outsider|\bstar ?god"),
    ("canoptek_construct", r"canoptek ?spyder|\bspyder\b|tomb ?stalker|"
                           r"tomb ?sentinel|doomstalker|triarch ?stalker"),
    ("canoptek_scarab", r"scarab|\bswarm\b"),
    # "\bwraiths?\b" and not "\bwraith": the bare prefix swallows every
    # Aeldari Wraithguard, Wraithlord, Wraithseer and Wraithknight. Canoptek
    # Wraiths still match, because they are written as whole words.
    # "destroyer" is a Necron unit AND a generic aircraft word (Marauder
    # Destroyer). The keyword stays, because Necron packs are often named
    # nothing but "Destroyer"; the aircraft are vehicle-flagged instead, and a
    # vehicle pack never lends its lineage to the faction guess.
    ("necron_destroyer", r"destroyer|destructor|lokhust|skorpekh|ophydian|"
                         r"\bwraiths?\b|hexmark"),
    ("necron_lychguard", r"lychguard|lych ?guard|praetorian|triarch"),
    ("necron_immortal", r"immortal|deathmark|cryptek|\w+mancer\b|overlord|"
                        r"nemesor|szeras|orikan|imotekh|trazyn|anrakyr|obyron|"
                        r"royal ?warden|\bnecron ?lord"),
    ("necron_warrior", r"necron ?warrior|flayed ?one|flayed|cryptothrall|"
                       r"\bnecron\b"),
    ("ork_warlord", r"ghaz\w*|thraka|gazgh\w*|ghazkul\w*"),
    # Biggest first. "megadread" contains "dread" but not "deff dread", so the
    # two walker tiers cannot poach each other.
    ("ork_stompa",  r"\bstompa|\bgargant"),
    ("ork_naut",    r"gorkanaut|morkanaut|mega ?dre[ad]d|meka.?dre[ad]d|"
                    r"krumpin|\bnaut\b"),
    # Sculptors write "Deff Dread", "Def Dread" and "Deff Dred"; the Killa Kan
    # turns up as "Kans", "Goblin Kans" and "Canopeners".
    ("ork_walker",  r"\bdef+ ?dre[ad]d|killa ?kan|killakan|\bkans?\b|"
                    r"goblin ?kan|canopener|dreaded ?robot"),
    ("meganob",     r"meganob|mega ?nob|mega ?armou?r|warboss|war ?boss|\bboss\b"),
    ("ork_nob",     r"\bnob\b|\bnobz\b|painboy|\bmek\b|big ?mek|weirdboy|"
                    r"weird ?boy|warphead|runtherd"),
    ("grot",        r"\bgrot\b|\bgrots\b|gretchin|\bsnotling"),
    ("ork_boy",     r"\bboy\b|\bboyz\b|shoota|slugga|burna|loota|kommando|"
                    r"stormboy|tankbusta|flash ?git|\bork\b|\borc\b"),
    ("centurion",   r"centurion"),
    # Mutilators and Obliterators wear Terminator armour fused with flesh —
    # 40mm bases, Terminator bulk — so they belong to that scale family even
    # though their names say nothing about armour.
    ("terminator",  r"terminator|tartaros|cataphractii|indomitus|"
                    r"blightlord|deathshroud|scarab occult|mutilator|obliterator|"
                    # "aquilon" belongs to `allarus`: it is a CUSTODES
                    # Terminator pattern.
                    r"\btermie|\btermi\b|\btermis\b|gal vorbak"),
    ("gravis",      r"gravis|aggressor|eradicator|heavy ?intercessor|inceptor|biologis"),
    ("phobos",      r"phobos|reiver|infiltrator|incursor|eliminator|suppressor"),
    # "atalan"/"jackal" are the Genestealer Cult's bike unit and its Alphus
    # character; both ride, so they size to the bike lineage rather than to the
    # acolyte on the saddle. Note "outridger" is a Ridgerunner variant spelling
    # and deliberately does NOT match "outrider" here.
    ("bike",        r"\bbike|jetbike|outrider|warbike|squighog|squig ?hopper|"
                    r"tomb ?blade|atalan|jackal ?alphus|jackal.?biker|"
                    # a rider sizes to its mount, horse or machine
                    r"rough ?rider|\bhorse\b|death ?rider|todesreiter|"
                    r"\bcavalry\b|\bmounted\b|horsem[ae]n"),
    # ---- Genestealer Cult -------------------------------------------------
    # This faction names almost nothing plainly: proxy sculptors write "denim"
    # (genes -> jeans), "thief" (gene-stealer), "miner" and "zerg", usually in
    # FOLDER names rather than filenames. These rules sit ABOVE "cultist" on
    # purpose, or a file like "FollowersArmsCultistKnife.stl" in a cult pack
    # would read as a Chaos cultist.
    # ---- T'au -------------------------------------------------------------
    # Designations are unambiguous and do the work here: XV = battlesuit,
    # KV/KX = the huge ones, MV/MB/DX = drone, TX/TY = vehicle. Most packs
    # classify on these alone.
    ("battlesuit_titanic", r"\bkv ?128|stormsurge|\bkx ?139|ta'?unar|"
                           r"supremacy ?armou?r"),
    ("battlesuit_super",   r"\bxv ?104|riptide|\bxv ?107|r'?varna"),
    ("battlesuit_heavy",   r"\bxv ?95|ghost ?keel"),
    # "commandant" is a common proxy word for a Crisis commander. It matters
    # because weapon packs are often nested a level below the suit folder
    # ("...Coldstar/Commandant Mech Weapons Pack"), so the suit's own name is
    # out of scope by the time they classify. A bare "mech" is NOT safe —
    # terrain and Ork packs use it too.
    ("battlesuit",         r"\bxv ?8\b|\bxv ?86|\bxv ?88|crisis|cold ?star|"
                           r"broadside|farsight|commandant"),
    # "stealth" alone matches Adeptus Mechanicus units, so it is qualified.
    ("battlesuit_light",   r"\bxv ?25|\bxv ?22|stealth ?(?:suit|battlesuit)|"
                           r"shadowsun"),
    # Pathfinder drones are checked BEFORE the infantry rule, and the Firesight
    # Marksman after it: both packs name a T'au and a drone, and this is the
    # order that reads each one correctly.
    ("drone",              r"pathfinder ?drone"),
    # "breacher" is deliberately absent — it matches Space Marines (Heresy
    # Breacher Squads). The T'au packs carry their own codewords.
    ("fire_warrior",       r"fire ?warrior|strike ?team|pathfinder|fireblade|"
                           r"firesight|fish ?people|breach.?boys|tuna ?beef|"
                           r"tunabeef|fire.?roasted|tau ?infantry|shas'?o"),
    ("kroot",              r"kroot|knarloc|cannibal ?chicken|ouch ?puppie"),
    ("vespid",             r"vespid|stingwing"),
    # A bare "drone" would swallow Nurgle's Plague Drones, so every T'au drone
    # is matched by its designation or its full name instead.
    ("drone",              r"\bmv ?\d+|\bmb ?\d+|\bdx ?\d+|frisbee|liquidator|"
                           r"(?:gun|shield|marker|sniper|recon|missile|combat|"
                           r"grav.?inhibitor|shielded missile) ?drones?|"
                           r"drone ?(?:squad|command|squadron)|various drones"),
    # ---- Aeldari ----------------------------------------------------------
    # "guardian" is deliberately absent: it matches Chaos packs ("Horus
    # Guardian Armor") and some T'au. "wraith" is absent for the same reason -
    # Necron Canoptek Wraiths already own it, so Aeldari wraith constructs are
    # spelled out in full above.
    ("aeldari",     r"aeldari|\beldar\b|craftworld|harle|\bclown\b|troupe|"
                    r"solitaire|shadowseer|death ?jester|starweaver|skyweaver|"
                    r"voidweaver|dire ?avenger|howling ?banshee|striking ?scorpion|"
                    r"fire ?dragon|warp ?spider|swooping ?hawk|\bwych\b|kabalite|"
                    r"\bincubi\b|\bmandrake|\bfarseer|\bautarch|"
                    r"eldar ?guardian|craftworld ?guardian|"
                    # the sculptors' codeword, and it is safe: outside the
                    # three Aeldari factions it only names Aeldari titans
                    r"space ?el(?:f|ves)|bone ?singer|kabalite|windrider|"
                    r"wind ?rider|shining ?spear|dark ?reaper|striking ?scorpion|"
                    r"\bwarlock\b|spiritseer|\bexarch\b"),
    # ---- Imperium, non-marine humans --------------------------------------
    # Inquisitorial retinues are called acolytes, but that word is already the
    # Genestealer Cult's, so the Inquisition is matched on "interrogator" and
    # named characters instead.
    # Before both sororitas and imperial_agent, because Penitent Engine proxies
    # are often sculpted and named as spiders ("... Pious ... Arachnid") and
    # hold a file named Repentia.stl — they would otherwise read as a sister
    # on "pious", or as a Repentia. "arachnid" also matches some Aeldari
    # files, which the faction constraint sends back to `aeldari`.
    ("penitent_engine", r"penitent|mortifier|anchorite|arachnid"),
    # Checked BEFORE imperial_agent: an armoured Inquisitor is a heavier frame
    # than the unarmoured retinue around him. Most sculpts named "inquisitor"
    # wear armour, which is why the plain word lands here rather than with the
    # agents. "inquisition", "inquisitive" and "inquisitin" are different words
    # and do not match: they turn up on Sororitas bases and as puns.
    ("inquisitor",  r"inquisitor|coteaz|karamazov|greyfax|draxus|quixos|"
                    r"eisenhorn|hector ?rex"),
    # Interrogators, acolytes and the Officio Assassinorum wear no power
    # armour, so they stay on the ordinary human frame.
    # Ecclesiarchy priests, and the unarmoured penitents, sit on the same human
    # frame as an assassin: no power armour, so not `sororitas`. A bare "priest"
    # is deliberately avoided — the Mechanicus is full of tech-priests — and so
    # is "purifier", which is a Grey Knight unit.
    ("imperial_agent", r"vindicare|callidus|culexus|culista|eversor|assassin|"
                       r"interrogator|\bcali ?girl\b|repentia|arco.?flagellant|"
                       r"ministorum|missionary|confessor|preacher|"
                       # a plain "priest" but never a TECH-priest. Space
                       # Marine hits are Sanguinary Priests, which the faction
                       # constraint sends back to a marine lineage.
                       r"(?<!tech )(?<!tech)\bpriest|"
                       r"ordo (?:malleus|hereticus|xenos)"),
    # "dominion" and "crusader" are real Sororitas squads but are NOT here:
    # both are common Knight proxy names ("Tall Dominion Crusader"). Sisters
    # are caught by their other names instead, including "nun", which is how
    # proxy sculptors usually write it.
    ("sororitas",   r"sororitas|\bsister\b|\bsisters\b|seraphim|"
                    r"celestian|celestine|cann?oness|zephyrim|retributor|"
                    r"dialogus|hospitaller|novitiate|\bpious\b|battle ?sister|"
                    r"\bnuns?\b|imagifier|sacresanct?|sacresant"),
    ("aberrant",    r"aberrant|abominant"),
    ("acolyte",     r"acolyte|metamorph|\bdenim\b|miner(?!al)|\bmining\b|zerg|"
                    r"\bhybrid\b|star prophet"),
    # Chaos infantry sits above the loyalist marks so a Plague Marine is not
    # read as a generic legionary, but below terminator so a Blightlord is.
    # Death Guard characters and Nurgle-afflicted marines all wear the same
    # bloated plague armour, so they share the lineage.
    ("plague_marine", r"plague ?marine|plaguemarine|\bplague ?brethren|blightspawn|"
                      r"\bplague ?surgeon|tallyman|plague ?champion|typhus|typhon|"
                      r"biologus|putrifier|plagued|\bfoetid|\bnoxious"),
    # "chaos spawn" only — a bare "spawn" would swallow Foul Blightspawn,
    # which is a Death Guard character in plague armour.
    ("chaos_spawn", r"chaos ?spawn|spawn of chaos"),
    # Neophyte Hybrids and Brood Brothers are ordinary humans in mining gear -
    # the same 28mm/0.8 frame as a Chaos cultist, so they share the lineage
    # rather than get a near-identical one of their own. "neofyte" is a
    # sculptor misspelling that appears as often as the correct one.
    ("cultist",     r"cultist|traitor guard|poxwalker|\bmutant|renegade militia|"
                    r"neophyte|neofyte|brood ?brother"),
    ("daemon_lesser", r"bloodletter|plaguebearer|pink horror|blue horror|"
                      # "seeker" must name the Slaanesh unit explicitly: on its
                      # own it matches T'au wargear (Seeker Missile, Seeker Pod).
                      r"da?emonette|nurgling|seekers? of slaanesh|"
                      r"seeker ?chariot|\bfuries\b"),
    # Deliberately LOW (see `custodian` below). "janitor" is a common proxy
    # word for Custodes - often more common than "custodes" itself - but it
    # also decorates their dreadnoughts and grav-tanks, so every more specific
    # rule must get first refusal. "chonky" is NOT used: it is applied to
    # Space Marines just as often.
    ("ogryn",       r"\bogryn|bullgryn|\bogre\b"),
    # "tempsetus" is a common misspelling, and "kasrkin"/"kasa-kin" are how
    # proxy sculptors often label Militarum Tempestus.
    ("scion",       r"tempestus|tempsetus|\bscion|storm ?trooper|\bhotshot|"
                    r"hellgun|kasrkin|kasa.?kin"),
    # Regiment names and the sculptors' codewords. "russ" on its own is the
    # PRIMARCH (and the odd punning tank name), so only the full "leman russ"
    # is used, and that lives in the vehicle rules anyway.
    ("guardsman",   r"\bkrieg\b|\bdkok\b|cadian|elysian|steel ?legion|"
                    r"\bvalhalla|commissar|komissar|astropath|\bpsyker|"
                    r"guardsm[ae]n|infantry ?squad|command ?squad|voxcaster|"
                    r"\btrench|spaceguard|\bshovel\b|expendable|grenadier|"
                    r"astra ?militarum|imperial ?guard"),
    # Tech-priests before skitarii: a Dominus is also "martian" and "mechanicus".
    ("tech_priest", r"tech.?priest|\bdominus\b|enginseer|manipulus|\bmagos\b|"
                    r"archmagos|anacharis|scoria|datasmith"),
    ("skitarii",    r"skitarii|sicarian|pteraxii|electro.?priest|fulgurite|"
                    r"corpuscarii|\bthrall|arquebus|cog ?ranger|"
                    r"mechanicus|mechanicum|martian|\badmech\b|\bcyborg\b"),
    ("custodian",   r"custodes|custodian|custard|janitor|santodes|"
                    r"sagittar|vexilus|trajann?|misericordia|pyrithite"),
    ("tacticus",    r"tacticus|intercessor|hellblaster|bladeguard|primaris"),
    ("firstborn",   r"firstborn|tactical|devastator|sternguard|vanguard|legionar|"
                    r"heresy|\bmk ?[ivx0-9]|\bmarine|berzerker|berserker|rubric|"
                    r"\bchosen\b|\bhavoc|raptor|warp talon|possessed|\bnoise\b"),
]

# Vehicle and tank components. They are real parts, but never fit a figure, so
# they are flagged and hidden rather than filling the review queue with turrets.
VEHICLE_RULES = (r"repulsor|impulsor|rhino|razorback|predator|land ?raider|whirlwind|"
                 r"vindicator|gladiator|drop ?pod|stormraven|stormtalon|thunderhawk|"
                 r"land ?speeder|turret|\bhull\b|chassis|sponson|\btrack\b|"
                 r"\btread\b|fuselage|cockpit|rhino ?door|"
                 r"trukk|battlewagon|\bwagon\b|wartrakk|dragsta|bommer|dakkajet|"
                 r"\bkopta\b|deff ?kopta|warkopta|land ?speeder|monolith|ghost ?ark|"
                 r"doomsday ?ark|tesseract ?ark|annihilation ?barge|"
                 r"command ?barge|night ?scythe|doom ?scythe|doomscythe|"
                 r"\bobelisk\b|\bpylon\b|convergence of dominion|"
                 # Genestealer Cult machinery, including the sculptor names the
                 # packs actually use. "rock ?grinder" is spelled out because a
                 # bare "grinder" is an Acolyte weapon (Acolytegrinder.stl).
                 # "mining sentinel" is qualified so it cannot catch the Necron
                 # Tomb Sentinel, which is a canoptek construct, not a vehicle.
                 r"ridgerunner|ridge ?runner|outridger|rock ?grinder|goliath|"
                 r"galyat|\binari\b|\bcrift\b|\blimo\b|exo.?suit|"
                 r"mining ?sentinel|"
                 # T'au grav-tanks, skimmers, flyers and terrain
                 r"devilfish|hammer ?head|sky ?ray|piranha|\borca\b|remora|"
                 r"tiger ?shark|barracuda|tidewall|shieldwall|dropship|gunship|"
                 # Sororitas armour and fortifications. "castigator" is also a
                 # Knight, which is harmless here — both readings are vehicles.
                 # The Exorcist is a pipe organ on a tank, and its packs often
                 # split the organ into its own subfolder where "Exorcist" is
                 # no longer in view.
                 r"exorcist|immolator|castigator|battle ?sanctum|piano|\borgan\b|"
                 # Aeldari grav-tanks and skimmers. "raider" is deliberately
                 # absent - it matches Land Raiders and Custodes packs.
                 # "scorpion" is qualified so it cannot eat Striking Scorpions.
                 r"fire ?prism|night ?spinner|wave ?serpent|\bfalcon\b|\bvyper\b|"
                 r"war ?walker|firestorm|scorpion ?grav|grav ?tank|\bhornet\b|"
                 r"eldar ?titan|space ?elf ?titan|"
                 # Heresy armour. "30k tanks" and the sculptor name "Woddish"
                 # catch whole packs at once, which matters because the Typhon
                 # and Sicarian cannot be named directly: `typhon` is also
                 # Typhus of the Death Guard, and `sicarian` is Mechanicus
                 # INFANTRY.
                 r"mast[ao]don|stormbird|30k ?tanks|wo[o]?ddish|"
                 # Custodes grav-armour and flyers
                 r"caladius|coronus|\bpallas\b|\borion\b|gyrfalcon|"
                 r"grav.?carrier|grav.?attack|"
                 # Mechanicus walkers and transports. Ironstriders and Sydonian
                 # Dragoons are riders on tall walkers - characters either way,
                 # and their parts fit nothing else.
                 r"onager|dune ?crawler|dune ?walker|sand ?walker|skorpius|"
                 r"macrocarid|terrax|termite|ironstrider|sydonian|ballistarii|"
                 # Astra Militarum armour. "leman russ" in full - never "russ".
                 # "kimera" is the trademark-dodging spelling sculptors use
                 r"leman ?russ|chimera|\bkimera\b|basilisk|hellhound|manticore|taurox|"
                 r"valkyrie|vendetta|\bsentinel\b|earthshaker|\bchimedon\b|"
                 # Guard artillery, super-heavies and aircraft
                 r"\bmedusa\b|carriage ?battery|shadowsword|shadowblade|"
                 r"\barvus\b|\bcentaur\b|breaching ?drill|lightning ?strike|"
                 r"\beinherjer\b|\bgigawatt\b|"
                 # A bare "tank" is a vehicle everywhere except a FUEL tank.
                 # "marauder"/"bomber" are the Forge World aircraft that the
                 # old destroyer rule was mislabelling as Necron.
                 r"(?<!fuel )(?<!gas )(?<!water )\btanks?\b|\bbomber\b|"
                 r"\bmarauder\b|assault ?drill")


# Role is ORTHOGONAL to armour lineage: a Techmarine wears ordinary power
# armour but carries a servo-harness, and an Apothecary Biologis wears Gravis.
# Tracking role separately keeps "which armour is this" and "what job does this
# marine do" from fighting each other, and makes role gear searchable.
ROLE_RULES = [
    ("techmarine", r"techmarine|tech marine|servo ?harness|servo ?arm|omnissiah"),
    ("apothecary", r"apothecary|narthecium|reductor|medicae|biologis"),
    ("chaplain",   r"chaplain|crozius|rosarius|skull ?helm"),
    ("librarian",  r"librarian|force (stave|staff|sword|axe)|psychic hood"),
    ("ancient",    r"ancient|standard bearer|banner bearer"),
    ("commander",  r"captain|chapter master|lieutenant|champion|master of"),
]

CLASS_OF_LINEAGE = {
    "firstborn": "infantry", "phobos": "infantry", "tacticus": "infantry",
    "gravis": "infantry", "terminator": "terminator", "centurion": "terminator",
    "dreadnought": "dreadnought", "bike": "bike_jump",
}


SIDE_LEFT = set("left l lh lft".split())
SIDE_RIGHT = set("right r rh rgt".split())


def detect_side(name):
    """Left/right for arms and pauldrons. Returns 'L', 'R' or None."""
    t = tokens(os.path.splitext(name)[0])
    if any(x in SIDE_LEFT for x in t) and not any(x in SIDE_RIGHT for x in t):
        return "L"
    if any(x in SIDE_RIGHT for x in t) and not any(x in SIDE_LEFT for x in t):
        return "R"
    return None


def sniff_kind(path):
    """Some downloads ship PNG/JPEG previews carrying a .stl extension."""
    try:
        with open(path, "rb") as f:
            magic = f.read(12)
    except OSError:
        return "unreadable"
    if magic[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if magic[:3] == b"\xff\xd8\xff":
        return "image"
    if magic[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if magic[:4] == b"%PDF":
        return "document"
    if magic[:2] == b"PK":
        return "archive"
    return "mesh"


def negated(word, text):
    for pat in NEG_PATTERNS:
        if re.search(pat % word, text):
            return True
    return False


def classify_row(name, parent, grandparent, dims, tris, faction=None,
                 model_name=""):
    """Return (slot, confidence, lineage, is_whole, is_sprue, role, is_vehicle).

    `model_name` is the model folder's own name, which is NOT always the parent
    or grandparent. Creators who ship a print pipeline bury files several levels
    down — ".../XV 95 GhostKeel/1.5 - Dimensioned Fusion Parts/New Parts/x.stl"
    leaves "New Parts" as the parent and the pipeline step as the grandparent,
    pushing the only identifying word out of view — and a characters-only model
    would then leak into the parts builder.
    """
    raw = re.sub(r"[_+\-]+", " ",
                 f"{name} {parent} {grandparent} {model_name}".lower())
    raw = re.sub(r"\s+", " ", raw)
    fn_t = tokens(os.path.splitext(name)[0])
    ctx_t = fn_t + tokens(parent) + tokens(grandparent) + tokens(model_name)

    lineage = DEFAULT_LINEAGE_BY_FACTION.get(faction, DEFAULT_LINEAGE)
    for lin, pattern in LINEAGE_RULES:
        if re.search(pattern, raw):
            lineage = lin
            break
    # A keyword can only ever suggest — the faction decides what is possible.
    lineage = lineage_for_faction(faction, lineage)

    role = None
    for r, pattern in ROLE_RULES:
        if re.search(pattern, raw):
            role = r
            break

    is_vehicle = bool(re.search(VEHICLE_RULES, raw))
    # Completeness is judged from the FILENAME only. Reading it from the folder
    # made every file in a pack named "...bundle" or "preassembled" look like a
    # finished miniature, including its bases and loose claws.
    is_whole = bool(set(fn_t) & WHOLE_WORDS)
    is_sprue = bool(set(ctx_t) & SPRUE_WORDS)

    # "shoulder pad" is a pauldron, not a generic arm — check before the word rules
    if re.search(r"shoulder[_ -]?pad|pauldron|spaulder", raw):
        return "pauldron", 0.9, lineage, is_whole, is_sprue, role, is_vehicle

    # filename evidence is stronger than folder evidence
    best, conf = None, 0.0
    for source, weight, from_folder in ((fn_t, 1.0, False),
                                        (suffix_slot_words(fn_t), 0.9, False),
                                        (ctx_t, 0.6, True)):
        hits = {}
        for slot, words in SLOT_WORDS.items():
            # A folder name says what the model CARRIES, not what a file IS.
            # "chaos-space-warrior-with-gatling-gun" would make merged.stl —
            # the whole marine — a ranged weapon. When the folder name is the
            # only slot evidence, weapon guesses are almost always wrong, so
            # weapon slots require the FILENAME to say so.
            if from_folder and slot.startswith("weapon_"):
                continue
            n = sum(1 for t in source if t in words)
            if n and not any(negated(w, raw) for w in words if w in source):
                hits[slot] = n
        if hits:
            # Specific beats generic. "arm" outranks weapons on purpose: a file
            # naming both ("right_arm_sword") is an arm assembly holding a
            # weapon, whereas a bare "sword" has no arm word and still lands
            # in the weapon slot.
            order = ["pauldron", "head", "torso", "legs", "backpack", "arm",
                     "weapon_melee", "weapon_ranged", "base", "accessory"]
            best = sorted(hits, key=lambda s: (-hits[s], order.index(s)))[0]
            conf = weight * min(1.0, 0.5 + 0.25 * hits[best])
            break

    # a "whole model" word plus no clear part word means it is a full figure
    if best is None and is_whole:
        return None, 0.0, lineage, True, is_sprue, role, is_vehicle

    # geometry sanity: a part taller than ~80% of a whole model is suspect
    if best and dims:
        mx = max(dims)
        if mx > 0 and best not in ("base",) and tris and tris > 400000 and mx > 0:
            conf *= 0.9
    return best, round(conf, 2), lineage, is_whole, is_sprue, role, is_vehicle


# ------------------------------------------------------------------- meshes
def require_deps():
    """Check dependencies against THIS interpreter.

    On macOS `pip3` and `python3` frequently resolve to different Python
    installs, so "Requirement already satisfied" can be true while the running
    interpreter still cannot import it. Always install via `python3 -m pip`,
    which targets the interpreter actually executing this file.
    """
    missing = []
    for mod, pkg in (("numpy", "numpy"), ("PIL", "pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        exe = sys.executable
        sys.exit(
            f"\nMissing: {', '.join(missing)}\n"
            f"This interpreter is:\n  {exe}\n\n"
            f"Install into THIS interpreter (not whichever python pip3 points at):\n"
            f'  "{exe}" -m pip install {" ".join(missing)}\n\n'
            f"If that reports an externally managed environment, use:\n"
            f'  "{exe}" -m pip install --break-system-packages {" ".join(missing)}\n')


def cmd_doctor(argv):
    """Report exactly which interpreter is running and what it can see."""
    import importlib.util
    print(f"python executable : {sys.executable}")
    print(f"python version    : {sys.version.split()[0]}")
    print(f"PYTHONPATH        : {os.environ.get('PYTHONPATH') or '(not set)'}")
    print(f"working directory : {os.getcwd()}")

    for mod in ("numpy", "PIL"):
        try:
            m = __import__(mod)
            print(f"  {mod:6s} OK   {getattr(m, '__version__', '?'):10s} "
                  f"{getattr(m, '__file__', '')}")
        except Exception as ex:
            print(f"  {mod:6s} FAILS  ({str(ex)[:70]})")
            try:
                spec = importlib.util.find_spec(mod)
                print(f"         resolves to: {spec.origin if spec else 'nothing'}")
            except Exception:
                pass

    # Find every copy on the path — a broken one earlier in the list wins.
    print("\nCOPIES ON THE IMPORT PATH (first match wins):")
    for mod in ("numpy",):
        found = 0
        for entry in sys.path:
            d = entry or os.getcwd()
            for cand in (os.path.join(d, mod), os.path.join(d, mod + ".py")):
                if os.path.exists(cand):
                    found += 1
                    compiled = os.path.isdir(cand) and (
                        os.path.isdir(os.path.join(cand, "_core"))
                        or os.path.isdir(os.path.join(cand, "core")))
                    tag = "  <-- USED" if found == 1 else ""
                    state = "looks complete" if compiled or cand.endswith(".py") \
                        else "NO COMPILED CODE - this is the broken one"
                    print(f"  {found}. {cand}\n       {state}{tag}")
        if found == 0:
            print(f"  no copy of {mod} found on the path at all")
        elif found > 1:
            print(f"\n  {found} copies of numpy found. The first one shadows the rest.")
            print("  Remove the broken copy, then re-run doctor:")
            print(f'    "{sys.executable}" -m pip uninstall -y numpy')
            print(f'    "{sys.executable}" -m pip install numpy')
    if LIBRARY_ROOT:
        print(f"\nlibrary root      : {LIBRARY_ROOT}  "
              + ("[found]" if os.path.isdir(LIBRARY_ROOT)
                 else "[NOT FOUND - drive plugged in?]"))
    else:
        print("\nlibrary root      : (not set - give it with --library or index)")
    print(f"data dir          : {DATA_DIR}")
    print(f"bundle folder     : {BUNDLE_DIR}")
    print(f"review sheet      : {REVIEW_SHEET}")
    if os.path.exists(DB_PATH):
        db = sqlite3.connect(DB_PATH)
        n = db.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        print(f"database          : {n} rows indexed")
    else:
        print("database          : not created yet")
    print(f'\nTo install deps:\n  "{sys.executable}" -m pip install numpy pillow')


def read_stl(path, want_arrays=True):
    """Return dict with tris, dims, and optionally the triangle array."""
    import numpy as np
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(84)
        if len(head) < 84:
            return None
        ntri = struct.unpack("<I", head[80:84])[0]
        binary = ntri > 0 and abs(84 + 50 * ntri - size) <= 4
        if binary:
            f.seek(84)
            raw = f.read(50 * ntri)
            if len(raw) < 50 * ntri:
                ntri = len(raw) // 50
                raw = raw[: 50 * ntri]
            if ntri == 0:
                return None
            a = np.frombuffer(raw, dtype=np.uint8).reshape(ntri, 50)
            v = a[:, 12:48].copy().view("<f4").reshape(ntri, 3, 3)
        else:
            # ASCII STL. Vertex lines are indented ("      vertex 1.0 2.0 3.0"),
            # so the leading whitespace must be stripped before matching —
            # testing line[:7] directly silently reads zero vertices and makes
            # every ASCII file look corrupt.
            f.seek(0)
            vs = []
            for line in f:
                s = line.lstrip()
                if s[:7] == b"vertex ":
                    try:
                        vs.append([float(x) for x in s.split()[1:4]])
                    except ValueError:
                        pass
            if len(vs) < 3:
                return None
            v = np.array(vs[: (len(vs) // 3) * 3], dtype="<f4").reshape(-1, 3, 3)
            ntri = len(v)
    if not np.isfinite(v).all():
        v = v[np.isfinite(v).all(axis=(1, 2))]
        if len(v) == 0:
            return None
    lo = v.reshape(-1, 3).min(axis=0)
    hi = v.reshape(-1, 3).max(axis=0)
    out = {"tris": int(ntri), "dims": (hi - lo).tolist(), "binary": binary}
    if want_arrays:
        out["v"] = v
        out["lo"] = lo
        out["hi"] = hi
    return out


def _cluster(v, grid):
    """Snap vertices onto a grid, welding each cell to its centroid, then drop
    triangles whose corners collapsed into the same cell."""
    import numpy as np
    flat = v.reshape(-1, 3)
    lo = flat.min(axis=0)
    ext = np.maximum(flat.max(axis=0) - lo, 1e-9)
    q = np.clip(((flat - lo) / ext * grid).astype(np.int64), 0, grid)
    key = (q[:, 0] * (grid + 1) + q[:, 1]) * (grid + 1) + q[:, 2]
    uniq, inv = np.unique(key, return_inverse=True)
    sums = np.zeros((len(uniq), 3), dtype=np.float64)
    np.add.at(sums, inv, flat)                      # centroid per occupied cell
    counts = np.bincount(inv, minlength=len(uniq)).reshape(-1, 1)
    centers = (sums / counts).astype(np.float32)
    ids = inv.reshape(-1, 3)
    keep = (ids[:, 0] != ids[:, 1]) & (ids[:, 1] != ids[:, 2]) & (ids[:, 0] != ids[:, 2])
    return centers[ids[keep]]


def decimate(v, target=PREVIEW_TRIS):
    """Vertex-clustering simplification.

    Vertices are genuinely welded to cell centroids so the surface stays
    connected. (Subsampling triangles instead leaves a cloud of disconnected
    slivers that renders as spikes and holes.) The grid is searched for the
    finest setting that still lands at or under the triangle budget.
    """
    if len(v) <= target:
        return v
    best = None
    for grid in (12, 16, 24, 32, 48, 64, 96, 128, 192, 256):
        out = _cluster(v, grid)
        if len(out) == 0:
            continue
        if len(out) <= target:
            best = out
        else:
            break
    if best is None:                                 # even the coarsest overshoots
        best = _cluster(v, 12)
        if len(best) > target:
            import numpy as np
            best = best[np.linspace(0, len(best) - 1, target).astype(int)]
    return best


def render_thumb(v, out_path, px=THUMB_PX):
    """Painter's-algorithm render: sort triangles by depth, fill with Lambert shading."""
    import numpy as np
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    # 3/4 view: rotate around Z then tilt
    az, el = np.radians(35), np.radians(20)
    Rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    Rx = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
    R = Rx @ Rz
    p = v.reshape(-1, 3) @ R.T
    p = p.reshape(-1, 3, 3)
    lo, hi = p.reshape(-1, 3).min(axis=0), p.reshape(-1, 3).max(axis=0)
    ext = np.maximum(hi - lo, 1e-6)
    s = (px * 0.86) / max(ext[0], ext[2])
    cx, cz = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2
    xs = (p[:, :, 0] - cx) * s + px / 2
    ys = px / 2 - (p[:, :, 2] - cz) * s
    depth = p[:, :, 1].mean(axis=1)

    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = n / np.maximum(ln, 1e-9)

    # Backface culling. Camera sits on +Y looking back along -Y, so a triangle
    # is visible only when its normal points towards the viewer. Without this,
    # interior faces get painted over the surface and the model looks shredded.
    facing = n[:, 1] > 0
    if facing.sum() < 8:                    # inconsistent winding: keep everything
        facing = np.ones(len(n), dtype=bool)

    light = np.array([0.3, 0.8, 0.5])
    light = light / np.linalg.norm(light)
    lam = np.clip(n @ light, 0, 1)
    shade = (45 + 195 * lam).astype(np.uint8)

    idx = np.nonzero(facing)[0]
    order = idx[np.argsort(depth[idx])[::-1]]
    img = Image.new("RGB", (px, px), (28, 30, 34))
    d = ImageDraw.Draw(img)
    for i in order:
        c = int(shade[i])
        d.polygon([(xs[i, 0], ys[i, 0]), (xs[i, 1], ys[i, 1]), (xs[i, 2], ys[i, 2])],
                  fill=(c, c, min(255, c + 12)))
    img.save(out_path, "PNG", optimize=True)
    return True


def save_preview_mesh(v, path):
    """Quantised + gzipped preview mesh, ~15x smaller than raw float32 STL.

    Layout: 'KBM1' | uint32 ntris | float32 lo[3] | float32 ext[3] | int16 xyz...
    Positions are normalised into 0..32767 across the bounding box, which is far
    finer than a 256px preview can show while cutting storage from ~700KB to
    ~50KB per part (matters across thousands of parts).
    """
    import gzip
    import numpy as np
    a = np.ascontiguousarray(v, dtype=np.float32)
    lo = a.reshape(-1, 3).min(axis=0)
    ext = np.maximum(a.reshape(-1, 3).max(axis=0) - lo, 1e-6)
    q = np.clip((a - lo) / ext * 32767.0, 0, 32767).astype("<i2")
    with gzip.open(path, "wb", compresslevel=6) as f:
        f.write(b"KBM1")
        f.write(struct.pack("<I", len(a)))
        f.write(lo.astype("<f4").tobytes())
        f.write(ext.astype("<f4").tobytes())
        f.write(q.tobytes())


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS parts(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE, name TEXT, pack TEXT, parent TEXT, grandparent TEXT,
  size INTEGER, tris INTEGER,
  dim_x REAL, dim_y REAL, dim_z REAL,
  slot TEXT, slot_conf REAL, user_slot TEXT, side TEXT, role TEXT, faction TEXT,
  subfaction TEXT,
  kind TEXT DEFAULT 'mesh',
  lineage TEXT, unit_class TEXT,
  is_whole INT DEFAULT 0, is_sprue INT DEFAULT 0, is_vehicle INT DEFAULT 0,
  excluded INT DEFAULT 0,
  scale_factor REAL DEFAULT 1.0,
  thumb TEXT, mesh TEXT, indexed_at REAL
);
CREATE INDEX IF NOT EXISTS i_slot ON parts(slot);
CREATE INDEX IF NOT EXISTS i_pack ON parts(pack);
CREATE INDEX IF NOT EXISTS i_name ON parts(name);
CREATE TABLE IF NOT EXISTS packs(
  pack TEXT PRIMARY KEY, scale_factor REAL, lineage TEXT, method TEXT, n_ref INT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS folder_rules(
  folder TEXT PRIMARY KEY,   -- absolute model-folder path
  lineage TEXT,              -- armour lineage to force on every file inside
  kind TEXT,                 -- 'vehicle' | 'accessory' | NULL
  content TEXT,              -- 'parts' | 'characters' | NULL
  faction TEXT,
  notes TEXT, source TEXT
);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(MESH_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    migrate(db)
    return db


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# alter an existing table, so a database built by an earlier version is missing
# them and every query naming one fails. Add any that are absent.
LATER_COLUMNS = [
    ("side", "TEXT"), ("role", "TEXT"), ("kind", "TEXT DEFAULT 'mesh'"),
    ("user_slot", "TEXT"), ("scale_factor", "REAL DEFAULT 1.0"),
    ("is_sprue", "INT DEFAULT 0"), ("unit_class", "TEXT"),
    ("is_vehicle", "INT DEFAULT 0"), ("faction", "TEXT"),
    ("subfaction", "TEXT"),
]


def migrate(db):
    # folder_rules gained a faction column after first release
    try:
        fr = {r[1] for r in db.execute("PRAGMA table_info(folder_rules)")}
        if fr and "faction" not in fr:
            db.execute("ALTER TABLE folder_rules ADD COLUMN faction TEXT")
            db.commit()
    except sqlite3.Error:
        pass
    have = {r[1] for r in db.execute("PRAGMA table_info(parts)")}
    added = []
    for col, decl in LATER_COLUMNS:
        if col not in have:
            db.execute(f"ALTER TABLE parts ADD COLUMN {col} {decl}")
            added.append(col)
    if added:
        db.commit()
        print(f"(database upgraded: added {', '.join(added)})")
    return added


def walk_meshes(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith("._") and d not in
                 (".Spotlight-V100", "System Volume Information", ".fseventsd",
                  ".TemporaryItems", ".Trashes", ".__staging")]
        for f in fn:
            if f.startswith("._"):
                continue
            if f.lower().endswith(MESH_EXTS):
                out.append(os.path.join(dp, f))
    return out


def pack_of(path, root):
    """The model pack a file belongs to — the unit scale is estimated over.

    Shares strip_packaging with model_folder, so a model shipped with
    pipeline folders is one scale pack rather than one per folder, each with
    its own median factor computed from a fraction of the model's parts.
    """
    rel = os.path.relpath(path, root).split(os.sep)
    for i, p in enumerate(rel[:-1]):
        if p == "files" and i > 0:
            return os.sep.join(strip_packaging(rel[:i]))
    return os.sep.join(strip_packaging(rel[:-1])) if len(rel) > 1 else "."


def cmd_index(argv):
    """Index the library, or one folder inside it, reading every mesh once."""
    global LIBRARY_ROOT
    require_deps()
    if argv:
        root = _abs(argv[0])
        if not os.path.isdir(root):
            sys.exit(f"Not a folder: {root}")
        if not LIBRARY_ROOT:
            # The first folder indexed becomes the library root.
            LIBRARY_ROOT = root
            remember_paths()
        elif not (root + os.sep).startswith(LIBRARY_ROOT.rstrip(os.sep) + os.sep):
            sys.exit(f"{root}\nis outside your library ({LIBRARY_ROOT}).\n"
                     "Index a folder inside it, or pass --library to change it.")
    else:
        require_library()
        root = LIBRARY_ROOT
    limit = int(os.environ.get("KB_LIMIT", "0"))
    do_render = os.environ.get("KB_NO_RENDER") != "1"
    db = connect()
    cur = db.cursor()
    # Anything previously recorded as unreadable is retried rather than skipped,
    # so a parser fix heals the library on the next run without a full re-index.
    have = {r[0] for r in cur.execute(
        "SELECT path FROM parts WHERE kind IS NULL OR kind != 'unreadable'")}
    retry = cur.execute("SELECT COUNT(*) FROM parts WHERE kind='unreadable'").fetchone()[0]
    if retry:
        print(f"({retry} previously unreadable files will be retried)")

    print(f"Scanning {root} ...")
    files = walk_meshes(root)
    todo = [f for f in files if f not in have]
    if limit:
        todo = todo[:limit]
    print(f"{len(files)} meshes found, {len(todo)} to index"
          + (f" ({len(have)} already done)" if have else ""))
    if not todo:
        print("Nothing to do. Run `classify` to re-tag, or `stats` to review.")
        return

    t0 = time.time()
    done = failed = 0
    for i, p in enumerate(todo, 1):
        kind = sniff_kind(p)
        info = None
        if kind == "mesh":
            try:
                info = read_stl(p)
            except Exception as ex:
                info = None
                if i < 5:
                    print(f"  ! {os.path.basename(p)}: {ex}")
            if info is None:
                kind = "unreadable"
        if info is None:
            # not a mesh (usually a PNG preview shipped with a .stl extension)
            cur.execute("INSERT OR REPLACE INTO parts(path,name,pack,parent,grandparent,"
                        "size,tris,kind,excluded,indexed_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                        (p, os.path.basename(p), pack_of(p, root),
                         os.path.basename(os.path.dirname(p)), "", os.path.getsize(p),
                         0, kind, time.time()))
            failed += 1
            continue

        parent = os.path.basename(os.path.dirname(p))
        gp = os.path.basename(os.path.dirname(os.path.dirname(p)))
        slot, conf, lineage, whole, sprue, role, vehicle = classify_row(
            os.path.basename(p), parent, gp, info["dims"], info["tris"],
            faction_of(p), os.path.basename(model_folder(p)))

        thumb = mesh = None
        if do_render:
            # hashlib, not hash(): Python randomises str hashing per process,
            # so hash() would name the same file differently on every run and
            # orphan the previous thumbnail and preview mesh.
            key = hashlib.blake2b(os.fsencode(p), digest_size=8).hexdigest()
            try:
                small = decimate(info["v"])
                mesh = os.path.join(MESH_DIR, key + ".bin")
                save_preview_mesh(small, mesh)
                tp = os.path.join(THUMB_DIR, key + ".png")
                if render_thumb(small, tp):
                    thumb = tp
            except Exception:
                pass

        cur.execute(
            "INSERT OR REPLACE INTO parts(path,name,pack,parent,grandparent,size,tris,"
            "dim_x,dim_y,dim_z,slot,slot_conf,side,role,faction,subfaction,kind,"
            "lineage,unit_class,is_whole,is_sprue,is_vehicle,thumb,mesh,"
            "indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p, os.path.basename(p), pack_of(p, root), parent, gp,
             os.path.getsize(p), info["tris"],
             info["dims"][0], info["dims"][1], info["dims"][2],
             slot, conf, detect_side(os.path.basename(p)), role,
             faction_of(p), subfaction_of(p), "mesh",
             lineage, CLASS_OF_LINEAGE.get(lineage, "infantry"),
             int(whole), int(sprue), int(vehicle), thumb, mesh, time.time()))
        done += 1
        if i % 25 == 0 or i == len(todo):
            db.commit()
            el = time.time() - t0
            rate = i / el if el else 0
            eta = (len(todo) - i) / rate / 60 if rate else 0
            print(f"  [{100*i/len(todo):5.1f}%] {i}/{len(todo)}  "
                  f"{rate:.1f}/s  ETA {eta:.0f} min", end="\r", flush=True)
    db.commit()
    print()
    print(f"Indexed {done}, unreadable {failed}, in {(time.time()-t0)/60:.1f} min")
    estimate_pack_scales(db)
    cmd_stats([])


def estimate_pack_scales(db):
    """One export-scale factor per pack, from its confidently classified parts."""
    cur = db.cursor()
    rows = cur.execute(
        "SELECT pack,slot,lineage,dim_x,dim_y,dim_z FROM parts "
        "WHERE slot IS NOT NULL AND slot_conf>=0.6 AND excluded=0 AND is_whole=0"
    ).fetchall()
    by_pack = {}
    for pack, slot, lineage, dx, dy, dz in rows:
        mx = max(dx or 0, dy or 0, dz or 0)
        if mx <= 0:
            continue
        exp = expected_mm(slot, lineage or "firstborn")
        by_pack.setdefault(pack, []).append(exp / mx)
    n = 0
    for pack, factors in by_pack.items():
        factors.sort()
        med = factors[len(factors) // 2]
        lin = cur.execute("SELECT lineage,COUNT(*) c FROM parts WHERE pack=? "
                          "GROUP BY lineage ORDER BY c DESC LIMIT 1", (pack,)).fetchone()
        cur.execute("INSERT OR REPLACE INTO packs(pack,scale_factor,lineage,method,n_ref)"
                    " VALUES(?,?,?,?,?)",
                    (pack, med, lin[0] if lin else "firstborn", "median_of_parts", len(factors)))
        cur.execute("UPDATE parts SET scale_factor=? WHERE pack=?", (med, pack))
        n += 1
    db.commit()
    print(f"Estimated export scale for {n} packs")


VALID_LINEAGES = set(LINEAGE_HEIGHT)
# Faction corrections are written in the NOTES, not the lineage column — a
# faction says nothing about scale, so the two stay separate. Longest names are
# matched first so "Chaos Space Marines" wins over "Space Marines".
# "Chaos" is a GROUP, not a faction — the legacy key stays in FACTION_GROUPS
# for old data but must never be assigned, or the part drops out of every
# faction filter. Real Chaos factions are the two below.
KNOWN_FACTIONS = ((set(FACTION_GROUPS) - set(FACTION_GROUPS.values()) - {"Chaos"})
                  | {"Chaos Space Marines", "Chaos Daemons"})
_FACTION_LOOKUP = {norm_name(f).lower(): f for f in KNOWN_FACTIONS}
_FACTION_BY_LENGTH = sorted(_FACTION_LOOKUP, key=len, reverse=True)


def faction_in_notes(note):
    """A faction named anywhere in your notes reassigns the folder."""
    low = norm_name(note).lower()
    for key in _FACTION_BY_LENGTH:
        if re.search(r"\b" + re.escape(key) + r"\b", low):
            return _FACTION_LOOKUP[key]
    return None


def model_folder(path):
    """Absolute folder representing one model, ignoring packaging folders.

    This is the key folder rules are stored against, so it must not depend on
    which root an index run happened to use.
    """
    return os.sep.join(strip_packaging(os.path.dirname(path).split(os.sep)))


SHEET_FIELDS = ["folder", "label", "files", "lineage_assigned",
                "lineage_from_folder_name", "matched_text", "agreement",
                "lineage_mix", "slots_found", "faction", "your_lineage", "notes"]
# The three columns you edit. Everything else is regenerated from the library.
SHEET_EDITABLE = ["faction", "your_lineage", "notes"]
# Files whose names mark them as site-supplied preview images rather than meshes.
PREVIEW_NAME = re.compile(r"(large|small|tiny|medium)?[_ ]?(preview|thumb|display|card)", re.I)


def why_lineage(text):
    """Which rule fired and on what text — the column that makes a verdict reviewable."""
    raw = re.sub(r"\s+", " ", re.sub(r"[_+\-]+", " ", (text or "").lower()))
    for lin, pattern in LINEAGE_RULES:
        mt = re.search(pattern, raw)
        if mt:
            return lin, mt.group(0)
    return DEFAULT_LINEAGE, "(default - nothing matched)"


def survey_folder_names(root):
    """Group a folder tree into model folders using FILENAMES ONLY.

    Deliberately reads no mesh data: a review sheet only needs names, and
    reading geometry for a whole faction costs hours. The full `index` can run
    later, or overnight, without blocking the review.
    """
    groups = collections.defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith("._") and d not in
                       (".Spotlight-V100", "System Volume Information", ".fseventsd",
                        ".TemporaryItems", ".Trashes", ".__staging")]
        for fn in filenames:
            if fn.startswith("._") or not fn.lower().endswith(MESH_EXTS):
                continue
            if PREVIEW_NAME.search(fn) or is_epic(os.path.join(dirpath, fn)):
                continue
            groups[model_folder(os.path.join(dirpath, fn))].append(fn)

    rows = []
    for folder, files in groups.items():
        lin = collections.Counter()
        slots = collections.Counter()
        vehicles = 0
        for fn in files:
            parent = os.path.basename(folder)
            gp = os.path.basename(os.path.dirname(folder))
            slot, conf, lineage, whole, sprue, role, veh = classify_row(
                fn, parent, gp, [0, 0, 0], 0, faction_of(folder),
                os.path.basename(folder))
            lin[lineage] += 1
            vehicles += bool(veh)
            if slot:
                slots[slot] += 1
        label = pretty_label(folder)
        is_vehicle = vehicles > len(files) / 2
        tot = sum(slots.values())
        dom = max(slots.values()) if slots else 0
        # A part set is NAMED like one, or is many copies of a single slot.
        # It is not merely "a folder with no recognised torso" — that only means
        # the filenames were unreadable, which mislabels real characters.
        is_acc = (not is_vehicle) and bool(
            ACCESSORY_FOLDER.search(strip_loadout(norm_name(label))))
        kind = "vehicle" if is_vehicle else ("accessory" if is_acc else None)
        fl, ft = why_lineage(os.path.basename(folder))
        hint = primarch_hint(label)
        note = ""
        if hint and not kind:
            note = f"POSSIBLE {hint.upper()} PRIMARCH - please verify"
        try:
            rel_folder = os.path.relpath(folder, LIBRARY_ROOT)
        except ValueError:
            rel_folder = folder
        rows.append({
            # Relative to the library root so the sheet works on any machine —
            # an absolute path recorded on one computer matches nothing on another.
            "folder": rel_folder, "label": label, "files": len(files),
            "lineage_assigned": kind or lin.most_common(1)[0][0],
            "lineage_from_folder_name": kind or fl,
            "matched_text": f"{kind} - no armour lineage" if kind else ft,
            "agreement": "consistent" if len(lin) == 1 else "MIXED",
            "lineage_mix": "; ".join(f"{k}:{v}" for k, v in lin.most_common()),
            "slots_found": "; ".join(f"{k}:{v}" for k, v in slots.most_common()),
            # Pre-filled from where the folder sits. Correct it and the lineage
            # is constrained to that faction's own list — which is how a T'au
            # folder stops being able to read as firstborn.
            # In a holding folder the faction is inferred from the lineage —
            # but never from a VEHICLE's lineage, which is noise: a tank named
            # "Destroyer" would drag an Imperial bomber into the Necrons.
            "faction": (faction_of(folder)
                        if faction_of(folder) not in HOLDING_FOLDERS
                        else ("" if is_vehicle else
                              (faction_from_lineage(
                                  lin.most_common(1)[0][0] if lin else None)
                               or faction_of(folder) or ""))) or "",
            "your_lineage": kind or "", "notes": note, "_kind": kind,
        })
    order = {None: 0, "accessory": 1, "vehicle": 2}
    rows.sort(key=lambda r: (order.get(r["_kind"], 0), r["label"].lower()))
    return rows



# Why each rule is the way it is. Kept beside the rules so a future change can
# be weighed against the reasoning that produced the current behaviour.
RATIONALE = [
    ("Lineage means scale, not identity", """
Lineage exists to do two jobs: normalise size, and warn about mismatched
proportions. It deliberately does **not** encode who a model is — that is what
faction and sub-faction are for. This is why Chaos Space Marines reuse the
loyalist lineages wholesale, and why Grey Knights need only one extra lineage
(`dreadknight`). A lineage per faction would produce dozens of near-duplicate
entries sharing identical numbers."""),

    ("Height and bulk are separate axes", """
Gravis and Tacticus stand at about the same height but are nothing like the
same mass. Height alone cannot express that an Aggressor sits closer to a
Terminator than to an Intercessor. Warnings therefore key off **bulk
difference**, not lineage names differing: Gravis beside Terminator spans 0.3
and passes quietly, Firstborn beside Terminator spans 0.8 and is flagged."""),

    ("The fallback is tacticus, and firstborn for Chaos", """
When no keyword matches, an unlabelled loyalist marine is most often a
Primaris-era sculpt, so `tacticus` is the default. Chaos legions wear
ten-thousand-year-old marks, so that faction falls back to `firstborn` instead
— one table, two answers. Both are easy to change if a collection leans
another way."""),

    ("Accessory detection is name-driven only", """
An earlier version also marked any folder with no recognised torso as an
accessory. That mostly caught real characters whose filenames simply weren't
readable. Name-matching alone mislabels far fewer characters, at the cost of
sending a few genuine accessory packs to the review queue. A "many copies of
one slot" heuristic did no better and wrongly caught squad builders, which hold
five arms by nature."""),

    ("Loadout phrasing is stripped before the accessory test", """
"Boyz with melee weapons" and "Neophytes including heavy weapons" are whole
models, not weapon packs. The discriminator is clean: *with* or *including*
introduces what a model carries, while a trailing subject ("Weapons Pack")
names what a folder holds. The phrase is deleted before the name is tested
rather than suppressing the test outright, so "Bitz with weapons" still reads
as a bitz pack. `banner` gets the same treatment beside `bearer` — a banner
bearer is a model, not a banner."""),

    ("Some factions are named in code", """
Proxy sculptors avoid trademarked names, and some factions are almost never
named plainly. Genestealer Cult packs use `denim` (genes → jeans), `thief` and
`miner`, mostly in folder names rather than filenames. The rules lean on these
codewords where they are unambiguous, and keep the cult rules **above**
`cultist` so a cult file mentioning a cultist is not read as Chaos. `cult`
itself is deliberately not a signal: Chaos packs use it too."""),

    ("Lookalike frames share a lineage", """
A Neophyte Hybrid is a human in mining gear, numerically identical to the Chaos
`cultist`, so it reuses that lineage. A Harlequin is a Guardian in a costume
and a Wych is a Kabalite with less armour, so all three Aeldari factions share
`aeldari`. `acolyte` **is** separate, not for its size but because the torso
carries a second pair of arms — exactly the kind of fit constraint lineage
exists to gate."""),

    ("Weapon slots require the filename to say so", """
A folder name says what a model **carries**; only a filename says what a file
**is**. A folder called `chaos-space-warrior-with-gatling-gun` would turn the
whole marine inside it into a ranged weapon. Head, torso, legs, arm and
backpack still accept folder evidence; weapons do not."""),

    ("Slot keywords are matched as suffixes too", """
Sculptors run words together with no separator the tokeniser can see:
`Minertorso001`, `GKHEAD1`, `COMBIBOLTER`, `Rlowerleg`, `railgun`. Exact token
matching misses every one, and each then falls through to folder evidence.
Matching a slot word at the END of a 6+ character token recovers them, with
two classes of false positive excluded: `detail` is not a tail, and the
three-letter keywords are limited to arm, leg and gun so that warship is not a
hip and rabbit is not a bit. Suffix-only is deliberate — substring matching
would read "armour" as an arm and "based" as a base."""),

    ("Specific names outrank generic words", """
Rules are checked in order and the first match wins, so a named unit must sit
above any generic word it contains. `Heavy Intercessor` must reach `gravis`
before `tacticus` sees *intercessor*; a Wraithguard pack folder containing the
word *wraiths* must reach the Aeldari rules before the Necron Canoptek Wraith
rule. Generic keywords that collide across factions (`guardian`, `raider`,
`breacher`, a bare `stealth` or `drone`) are left out and replaced by full unit
names or designations."""),

    ("The faction constrains the lineage", """
A keyword can only ever *suggest* a lineage; the faction decides what is
possible. Without that, one stray filename is enough to put a foreign lineage
into a faction's filter list — `WraithboneBase.stl` would read as a Necron
Wraith, and an Ork `boy` could reach into Space Marine bodies. A lineage
outside its faction's set falls back to that faction's default.

A faction absent from the table is UNCONSTRAINED on purpose, so a faction
nobody has reviewed is not silently forced into a default. `bike` is shared
across factions deliberately, because a rider sizes to its machine."""),

    ("Parts vs characters is a question of HEIGHT", """
Whether a folder is a bitz box or a tray of finished models cannot be read from
filenames, and file sizes are only a weak signal. Measured height, as a
fraction of the lineage's reference height, separates them cleanly: parts sit
well under half the reference, finished figures at or above it.

That only works on SCALED dimensions. Raw STL units are whatever the sculptor
modelled in, so comparing them to a height in millimetres reads almost
everything as a finished figure. The rule declines to answer in the ambiguous
middle, and errs toward "parts" when it does answer: a wrong "parts" leaves
pieces visible in the builder, a wrong "character" hides them."""),

    ("Characters-only is about fit, not recognisability", """
Dreadnoughts classify into slots as well as infantry do and are still
characters-only — because a Contemptor arm fits nothing else, not because its
parts are unrecognisable. Battlesuits below Riptide size stay in the parts
builder: they are modular by design, and swapping arms and weapon pods between
them is how they are meant to be built."""),

    ("Print-pipeline folders are packaging, not models", """
Some creators ship their whole workflow: CAD source, supported STLs, pre-sliced
printer plates, times "Old" / "New Parts" / "New New Parts" revisions. Without
special handling, one model reads as a dozen separate packs, each with its own
scale estimate. Treating those folders as packaging collapses them back into
one model.

Two traps are excluded by requiring spaces around the step dash:
`6-8mm-extra-accessories-...` and `1234567 - sentry pylon proxy` are download
folder names, not steps. The cut is made at the FIRST pipeline segment rather
than trimming from the right, because `4 - Photon/Arms` has an ordinary leaf
name."""),

    ("Exports keep the raw mesh, not the supported one", """
The same part often ships several times: raw, supported, and as a sliced
printer plate. A supported copy has its scaffolding fused into the mesh. This
tool RESCALES every part, and rescaling a supported mesh rescales its supports
too, leaving them the wrong size for whatever orientation you finally print
at. So the raw mesh wins, supports are a fallback, and printer plates are
dropped.

Revisions are overlaid rather than picked wholesale, because newer sets are
usually PARTIAL — only the pieces that changed. The newest copy of each part
wins and older revisions fill the gaps. Matching tolerates revision decoration
(`Head.stl`, `_Head.stl`, `Front Plate_V2.stl`) but ONLY inside packs that
carry pipeline folders; in an ordinary pack `_arm.stl` beside `arm.stl` is more
likely two real parts. De-duplication happens at EXPORT, never in browsing, so
every copy stays visible and a supported version can still be picked by
hand."""),

    ("Sub-factions are matched by alias, not by name", """
A sub-faction is almost never spelled out in a folder: sculptors write
"Vindicare" or "Coteaz", never "Officio Assassinorum" or "Inquisition".
Matching a sub-faction's aliases as well as its name puts those in the right
place without renaming anything on disk."""),

    ("Completeness is read from the filename only", """
Reading it from the folder makes every file in a pack named "...bundle" or
"preassembled" look like a finished miniature — including its bases and loose
claws. `supported` and `presupported` are not completeness words at all: they
describe print supports, and would mark every supported weapon as a finished
figure."""),

    ("A figure is 45% of its lineage height", """
Names lie in both directions, so measured size decides. Genuine complete
figures can measure well under their lineage's reference height, so the cut-off
is generous. The companion rule, that a file inside a multi-file folder must be
**1.4x** the median, stops one oversized weapon bit in a dreadnought folder
from being listed as a character while the dreadnought itself vanishes."""),

    ("Lineage overrides the parts/characters note", """
For a marine, a "parts" folder is a bitz pack serving many models; for a
dreadnought it is one model in pieces. Combined with being hidden from the
parts builder, a "parts" note would make a dreadnought invisible in both views.
So lineage decides — anything in CHARACTER_ONLY_LINEAGES is always one complete
model."""),

    ("Scale is corrected per pack, not per file", """
Files arrive in wildly inconsistent units. Per-file guessing is fragile, so
each pack gets one factor from the median of its confidently classified parts.
A per-part sanity check catches packs that mix scales, such as a head that
would otherwise export at a few millimetres."""),

    ("Heads travel up in bulk, never down", """
A Tacticus helmet sits fine inside a Gravis or Terminator collar; a Terminator
helmet on a Tacticus body is obviously oversized. The rule is directional for
heads and pauldrons, and unrestricted for weapons and accessories, which are
handheld or bolted on."""),

    ("Sub-faction is a third level, not a peer", """
Flattening legions into factions produces a residual bucket: "Chaos Space
Marines" would mean "whatever isn't filed under a legion", while every World
Eater is also a Chaos Space Marine. Three levels — faction, then chapter,
legion or regiment — resolve it."""),

    ("EPIC scale is excluded outright", """
Those models are ~6mm for titan-scale battles; mixing them into a 28mm library
corrupts pack scale estimation. The pattern needs care: a naive `8mm` matches
inside `28mm`, and `\\bepic\\b` misses `_Epic` because underscore is a word
character, so the test runs on a separator-normalised path."""),

    ("Primarchs are flagged, never auto-assigned", """
Name matching cannot be trusted here: "Vulkan" can be a backpack, `russ` can be
a tank, and `horus` usually means the era. Candidates are therefore written
into the review sheet for confirmation."""),

    ("Review sheets store relative paths", """
A sheet that records absolute paths stops matching as soon as the drive mounts
somewhere else, and importing it would silently discard every verdict. Paths
are relative to the library root, and the importer also re-tries the tail of a
foreign absolute path under the current root."""),
]


def cmd_rules(argv):
    """Write every classification rule to a markdown file, read from the live
    code so it cannot drift out of date the way a hand-written doc would."""
    out = argv[0] if argv else os.path.join(os.getcwd(), "CLASSIFICATION_RULES.md")
    L = []
    add = L.append
    add("# Classification Rules — generated reference\n")
    add(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} from "
        "`kitbash.py`. Regenerate with `rules` after any change._\n")
    add("Everything below is read out of the running code, so it always matches "
        "actual behaviour.\n")

    add("\n## 1. Precedence — what wins\n")
    add("1. **Your review verdicts** (`folder_rules`, from `import-review`) — a "
        "folder's lineage applies to every file inside it.")
    add("2. **Character-only lineages** override the parts/characters note.")
    add("3. **Filename rules** below.")
    add("4. **Faction default** when nothing matches.\n")

    add("\n## 2. Lineages\n")
    add("| Lineage | Height | Bulk | Characters-only |")
    add("|---|---|---|---|")
    for lin in sorted(LINEAGE_HEIGHT, key=lambda x: LINEAGE_HEIGHT[x]):
        only = "yes" if lin in CHARACTER_ONLY_LINEAGES else ""
        add(f"| `{lin}` | {LINEAGE_HEIGHT[lin]:.0f} mm | "
            f"{LINEAGE_BULK.get(lin, 1.0)} | {only} |")
    add("\nCharacter-only lineages never appear in the parts builder and are "
        "always treated as one complete model, whatever the notes say.\n")

    add("\n## 3. Lineage detection — checked in this order\n")
    add("Order matters: the first match wins. `Heavy Intercessor` must reach "
        "`gravis` before `tacticus` sees the word *intercessor*.\n")
    add("| # | Lineage | Pattern |")
    add("|---|---|---|")
    for i, (lin, pat) in enumerate(LINEAGE_RULES, 1):
        add(f"| {i} | `{lin}` | `{pat}` |")

    add("\n### Default when nothing matches\n")
    add(f"- Everything else: `{DEFAULT_LINEAGE}`")
    for fac, lin in sorted(DEFAULT_LINEAGE_BY_FACTION.items()):
        add(f"- {fac}: `{lin}`")
    add("\nUnlabelled loyalist sculpts are most often Primaris-era, hence "
        "`tacticus`. Chaos legions wear old marks, so they default to "
        "`firstborn` instead.\n")

    add("\n## 4. Part slots\n")
    add("| Slot | Expected size | Keywords |")
    add("|---|---|---|")
    for slot in sorted(SLOT_WORDS):
        frac = SLOT_FRACTION.get(slot)
        size = f"{frac:.0%} of height" if frac else "—"
        words = " ".join(sorted(w for w in SLOT_WORDS[slot] if len(w) > 2)[:14])
        add(f"| `{slot}` | {size} | {words} |")
    add("\nFilename evidence outranks folder evidence. Negations are honoured, so "
        "`nohead`, `headless` and `without head` never match *head*.\n")
    add("Specificity order when several slots match: "
        + " → ".join(["pauldron", "head", "torso", "legs", "backpack", "arm",
                      "weapon_melee", "weapon_ranged", "base", "accessory"]))
    add("\n`arm` deliberately outranks weapons: `right_arm_sword.stl` is an arm "
        "assembly, while a bare `sword.stl` is a weapon.\n")

    add("\n## 5. Roles (independent of armour)\n")
    add("| Role | Pattern |")
    add("|---|---|")
    for role, pat in ROLE_RULES:
        add(f"| `{role}` | `{pat}` |")
    add("\nRole is orthogonal to lineage — a Techmarine wears ordinary power "
        "armour but carries a servo-harness.\n")

    add("\n## 6. Exclusions\n")
    add(f"- **Vehicles** — `{VEHICLE_RULES.pattern if hasattr(VEHICLE_RULES,'pattern') else VEHICLE_RULES}`")
    add(f"- **EPIC scale** — `{EPIC_PATTERN.pattern}` (≈6 mm, wrecks scale "
        "estimation). Tested against a separator-normalised path, so `_Epic` "
        "is caught — underscore is a word character, which defeats a plain "
        "word boundary.")
    add(f"- **Accessory folders** — `{ACCESSORY_FOLDER.pattern}`")
    add("- **Preview images** — PNG/JPEG shipped with a `.stl` extension, detected "
        "by magic bytes not filename")
    add("- **AppleDouble** `._*` files\n")
    add("Accessory detection is **name-driven only**. An earlier rule also marked "
        "any folder with no recognised torso, which mislabelled real characters "
        "whose filenames simply weren't readable.\n")

    add("\n## 7. Whole models vs parts\n")
    add(f"- Completeness words (filename only): {' '.join(sorted(WHOLE_WORDS)[:18])}")
    add(f"- Sprue/set words: {' '.join(sorted(SPRUE_WORDS)[:14])}")
    add("\nDeliberately excluded: *supported*, *presupported* — those describe "
        "print supports, not completeness, and were marking every supported "
        "weapon as a finished figure.\n")
    add("A file counts as a standalone miniature when it:\n")
    add(f"1. measures at least **{MIN_FIGURE_FRACTION:.0%}** of its lineage height;")
    add(f"2. is not confidently a component (slot confidence ≥ {COMPONENT_CONF});")
    add("3. is not a sprue; and")
    add("4. inside a multi-file folder, is at least **1.4×** the median file size.\n")
    add("A folder becomes a multi-part character when it covers at least "
        f"**{MIN_CORE_SLOTS} of {len(CORE_SLOTS)}** core slots "
        f"({', '.join(sorted(CORE_SLOTS))}) **and** has a torso or legs — "
        "or whenever its lineage is character-only.\n")

    add("\n## 8. Scale\n")
    add("Tabletop is 28 mm heroic (~1:56), measured foot-to-eye. Two corrections:\n")
    add("**Export correction (index time).** One factor per pack, the median of "
        "`expected size ÷ measured size` across its confidently classified parts. "
        f"If a part still lands outside **{SANE_LO}×–{SANE_HI}×** its expected "
        "size, it is re-estimated individually and flagged `*`.\n")
    add("**Lineage matching (bundle time).** The chosen torso anchors the build; "
        "other parts scale by `anchor height ÷ part height`. With no torso it "
        "falls back to legs, then the Build Scale dropdown.\n")
    add(f"Bulk differences are warned at **{BULK_MILD}** and called out loudly at "
        f"**{BULK_STRONG}**. Nothing is ever blocked.\n")
    add("| Slot | Directional rule |")
    add("|---|---|")
    for slot, pol in sorted(SLOT_BULK_POLICY.items()):
        human = {"down_only": "equal or lighter armour only",
                 "any": "any bulk", "match": "same bulk preferred"}[pol]
        add(f"| `{slot}` | {human} |")
    add("\nHeads and pauldrons travel **up** in bulk, never down: a Tacticus "
        "helmet fits a Gravis collar, a Terminator helmet on a Tacticus body "
        "does not.\n")

    add("\n## 9. Faction hierarchy\n")
    add("```\nGroup  →  Faction  →  Sub-faction  →  Lineage\n```\n")
    add("| Group | Factions |")
    add("|---|---|")
    for g in sorted(set(FACTION_GROUPS.values())):
        fl = sorted(f for f in factions_in_group(g) if f != "Chaos")
        add(f"| {g} | {', '.join(fl)} |")
    add("\n| Faction | Sub-factions |")
    add("|---|---|")
    for fac, subs in SUBFACTIONS.items():
        add(f"| {fac} | {', '.join(subs)} |")
    add("\nSub-faction never affects scale or fit — every chapter and legion "
        "shares the same lineages. It exists purely for browsing.\n")
    add("Faction is derived from the folder path. A faction named in your review "
        "**notes** reassigns the folder, which is how Chaos packs sitting in the "
        "Space Marine tree reach the Chaos filters.\n")

    add("\n### Which lineages each faction may use\n")
    add("A lineage outside its faction's set falls back to that faction's "
        "**default**, which is what stops a keyword collision putting a T'au "
        "suit in the Space Marines. Sorted by height within each faction.\n")
    add("Legend: **bold** = the faction's default · `*` = characters-only, "
        "never offered in the parts builder.\n")
    for g in sorted(set(FACTION_GROUPS.values())):
        facs = sorted(f for f in factions_in_group(g)
                      if f != "Chaos" and f in FACTION_LINEAGES)
        if not facs:
            continue
        add(f"\n#### {g}\n")
        add("| Faction | Default | Lineages (height · bulk) |")
        add("|---|---|---|")
        for f in facs:
            dflt = DEFAULT_LINEAGE_BY_FACTION.get(f, DEFAULT_LINEAGE)
            items = []
            for l in sorted(FACTION_LINEAGES[f], key=lambda x: LINEAGE_HEIGHT.get(x, 0)):
                txt = (f"**{l}**" if l == dflt else l)
                if l in CHARACTER_ONLY_LINEAGES:
                    txt += "\\*"
                items.append(f"{txt} ({LINEAGE_HEIGHT.get(l, 0):.0f}mm · "
                             f"{LINEAGE_BULK.get(l, 1.0)})")
            add(f"| {f} | `{dflt}` | {' · '.join(items)} |")

    loose = sorted(f for f in set(FACTION_GROUPS) - set(FACTION_LINEAGES)
                   if f != "Chaos")
    if loose:
        add("\n#### Unconstrained\n")
        add("These have no lineage set defined, so any lineage may stand. That "
            "is deliberate — a faction nobody has reviewed should not be forced "
            "into a default that was never checked against its contents.\n")
        add("- " + ", ".join(loose) + "\n")

    add("\n**Shared lineages.** A few belong to more than one faction on "
        "purpose, because lineage encodes scale and bulk rather than identity:\n")
    shared = collections.defaultdict(list)
    for f, s in FACTION_LINEAGES.items():
        for l in s:
            shared[l].append(f)
    for l, fs in sorted(shared.items()):
        if len(fs) > 1:
            add(f"- `{l}` — {', '.join(sorted(fs))}")
    add("")

    add("\n## 10. Why these rules\n")
    add("The reasoning behind the less obvious decisions, recorded so a future "
        "change can be weighed against it.\n")
    for heading, body in RATIONALE:
        add(f"### {heading}\n")
        add(body.strip() + "\n")

    add("\n## 11. Known traps\n")
    add("- a *Vulkan* backpack is an accessory, not the primarch")
    add("- `russ` can match a tank with a punning name")
    add("- `horus` usually means the **era**, not the primarch")
    add("- `alpharius` can appear in ancestor folders of unrelated troop parts")
    add("- `disco` matches both a Lord Discordant and *panic-at-the-disco* Noise Marines")
    add("- `paladin` is used loosely as a Grey Knight synonym, not only for "
        "Terminator-armoured Paladins")
    add("- Sculptors write **Demon** as often as **Daemon**")
    add("- Genestealer Cult sculpts are named in code — **denim** (genes → "
        "jeans), **thief**, **zerg**, **miner** — and every one of those words "
        "appears in FOLDER names only, never in a filename")
    add("- `outridger` is a *Ridgerunner* variant and must not be read as the "
        "Space Marine **outrider** bike")
    add("- a bare `grinder` is an Acolyte weapon (`Acolytegrinder.stl`), so "
        "only `rock grinder` marks the Goliath vehicle")
    add("- `jackal` alone matches Tzaangor *undying-jackal-cultists*; only "
        "`atalan`, `jackal alphus` and `jackal biker` mean the cult bike")
    add("- `Mk2` on a cult truck matches the power-armour mark rule and reads "
        "as `firstborn`. Harmless — those files are vehicle-flagged and hidden "
        "— but it is why a vehicle's lineage column can look odd")
    add("- \"with/including **weapons**\" describes a loadout, not a bitz pack")
    add("- `guardian` matches Chaos packs (*Horus Guardian Armor*), so it "
        "is **not** an Aeldari keyword")
    add("- `wraith` belongs to the Necron Canoptek Wraiths — Aeldari "
        "Wraithguard and Wraithlords must be spelled out in full")
    add("- `dominion` is a common Knight proxy name (*Tall Dominion Crusader*), "
        "so it never marks the Sororitas Dominion squad")
    add("- `acolyte` is the Genestealer Cult's, so Inquisitorial acolytes are "
        "matched on `interrogator` instead")
    add("- `detail` ends with *tail* but is not a backpack")
    add("- `6-8mm-extra-accessories-...` and `1234567 - sentry pylon "
        "proxy` are download folder names, **not** pipeline steps — which is "
        "why a step number needs spaces around its dash")
    add("- a *supported* STL has its scaffolding fused into the mesh, so it "
        "must never be the copy that gets rescaled")
    add("- `New Parts` is usually a PARTIAL revision — taking it alone loses "
        "most of the model")
    add("- `breacher` is a **Space Marine** unit too, not only a T'au one")
    add("- a bare `stealth` matches Adeptus Mechanicus units, and a bare "
        "`drone` matches Nurgle's Plague Drones")
    add("- `palm` matches palm **trees** in terrain packs")
    add("- *cannibal chicken* means Kroot, but also names a Genestealer Cult "
        "Ridgerunner pack — that one is vehicle-flagged, so the odd lineage "
        "on it is hidden")
    add("- models filed in holding folders (uploads, presupported, proxies) "
        "take their faction from their lineage, so check them on the sheet")
    add("- a bare `seeker` matches T'au *Seeker Missiles*, so the Slaanesh "
        "rule names the unit in full")
    add("- T'au weapon packs often nest a level BELOW the suit folder "
        "(`...Coldstar/Commandant Mech Weapons Pack`), which is why "
        "`commandant` earns its own keyword")
    add("- a filter option is only worth offering if the view can show it: "
        "check `is_whole`, `is_vehicle` and slot, not just `excluded`")
    add("- `inquisition`, `inquisitive` and `inquisitin` are NOT `inquisitor` "
        "— they are Sororitas bases and sculptors' puns")
    add("- **Hector Rex** wears *Terminator* armour, not power armour, so "
        "he needs a manual lineage on the review sheet")
    add("- Primarchs are **flagged for verification**, never auto-assigned\n")

    text = "\n".join(L) + "\n"
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as ex:
        sys.exit(f"Could not write {out}: {ex}")
    print(f"Wrote {out}  ({len(L)} lines, {len(LINEAGE_RULES)} lineage rules, "
          f"{len(LINEAGE_HEIGHT)} lineages)")


def _pack_content_features(db):
    """Per model folder: how many of its files are figure-sized.

    The discriminator for "is this a tray of finished models or one model in
    pieces" is HEIGHT, not name and not file size. A 55mm mesh is a whole Crisis
    suit; a 12mm one is a vent. Filenames barely help, and file sizes mislead —
    a folder of shoulder pads has uniform sizes and looks like a squad.
    Geometry is the honest way to do it.
    """
    packs = collections.defaultdict(
        lambda: {"n": 0, "fig": 0, "slots": collections.Counter(),
                 "heights": [], "lin": collections.Counter()})
    for path, dx, dy, dz, lin, slot, sc in db.execute(
            "SELECT path,dim_x,dim_y,dim_z,lineage,slot,scale_factor FROM parts "
            "WHERE excluded=0 AND kind='mesh'"):
        key = model_folder(path or "")
        f = packs[key]
        f["n"] += 1
        if slot:
            f["slots"][slot] += 1
        f["lin"][lin or "?"] += 1
        # RAW dimensions are in whatever units the sculptor modelled in, which
        # is the whole reason scale_factor exists. Comparing them to a lineage
        # height without it reads most parts folders as trays of finished
        # figures.
        tall = max(dx or 0, dy or 0, dz or 0) * (sc or 1.0)
        want = LINEAGE_HEIGHT.get(lin or "", 32.0)
        f["heights"].append(tall / want if want else 0)
        if want and tall >= 0.75 * want:
            f["fig"] += 1
    return packs


CORE_SLOTS = {"head", "torso", "legs", "arm"}


# Measured on the median file height as a FRACTION of the folder's lineage
# reference height, the three shapes separate cleanly:
#
#     class                          median height   core slots
#     parts (a bitz box)             ~0.4            some
#     one model, one file            above 1         none
#     a tray of finished models      above 1         none
#
# The gap between those is the whole signal. Run `content-check` to score these
# thresholds against the notes on your own review sheet.
CONTENT_TALL = 1.00      # at or above its lineage's full height -> a finished model
CONTENT_SHORT = 0.55     # around half -> a component


def predict_content(f):
    """'characters' (each file a finished model) or 'parts'. None = no opinion."""
    hs = sorted(f.get("heights") or [])
    if not hs:
        return None
    h = hs[len(hs) // 2]
    # A folder holding ONE file is one thing: a model if it is model-sized, a
    # loose component otherwise.
    if f["n"] == 1:
        return "characters" if h >= CONTENT_TALL else "parts"
    # Pieces covering most of a body mean one model in bits, whatever their size.
    if len(CORE_SLOTS & set(f["slots"])) >= 3:
        return "parts"
    if h >= CONTENT_TALL:
        return "characters"
    if h <= CONTENT_SHORT:
        return "parts"
    return None          # the middle ground is genuinely ambiguous — say nothing


def cmd_content_check(argv):
    """Score the parts-vs-characters guess against the notes you already wrote.

    Read-only unless you pass --apply <sheet>, and even then it only fills rows
    whose notes are EMPTY, so nothing you decided is overwritten.
    """
    apply_to = None
    if "--apply" in argv:
        i = argv.index("--apply")
        apply_to = argv[i + 1] if len(argv) > i + 1 else None
    db = connect()
    feats = _pack_content_features(db)
    print(f"{len(feats)} indexed model folders")

    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit(f'openpyxl is required:\n  "{sys.executable}" -m pip install openpyxl')
    if not os.path.exists(REVIEW_SHEET):
        sys.exit(f"No review sheet at {REVIEW_SHEET}")
    wb = load_workbook(REVIEW_SHEET)

    def truth_of(note):
        low = str(note or "").strip().lower()
        if re.search(r"full (character|figure)|each file is a full", low):
            return "characters"
        if low.startswith("parts") or re.search(r"\bparts\b", low):
            return "parts"
        return None

    conf = collections.Counter()
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        head = [c.value for c in ws[1]]
        if "folder" not in head or "notes" not in head:
            continue
        if apply_to and sheet == apply_to:
            continue                       # never score the sheet we are filling
        fi, ni = head.index("folder"), head.index("notes")
        for r in ws.iter_rows(min_row=2, values_only=True):
            if not r[fi]:
                continue
            t = truth_of(r[ni])
            if not t:
                continue
            f = feats.get(os.path.normpath(resolve_review_folder(str(r[fi]))))
            if not f:
                continue
            conf[(t, predict_content(f) or "(no opinion)")] += 1

    total = sum(conf.values())
    if not total:
        print("No overlap between your notes and the index — nothing to score.")
    else:
        decided = sum(v for (t, p), v in conf.items() if p != "(no opinion)")
        right = conf[("parts", "parts")] + conf[("characters", "characters")]
        majority = max(sum(v for (t, p), v in conf.items() if t == x)
                       for x in ("parts", "characters"))
        print(f"\nscored against {total} folders you labelled by hand")
        print(f"  answered            : {decided}  ({100*decided//total}%)")
        print(f"  correct when answered: {right}  ({100*right//max(decided,1)}%)")
        print(f"  always-'parts' baseline: {100*majority//total}%")
        print("\n  truth \\ predicted")
        cols = ["parts", "characters", "(no opinion)"]
        print("    " + " " * 12 + "".join(f"{c:>14s}" for c in cols))
        for t in ("parts", "characters"):
            print(f"    {t:12s}" + "".join(f"{conf[(t,c)]:14d}" for c in cols))

    if "--dump" in argv:
        out = os.path.join(os.path.dirname(REVIEW_SHEET), "content_features.csv")
        import csv
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["folder", "truth", "n", "fig", "frac_fig", "core_slots",
                        "slots", "lineage", "h_min", "h_med", "h_max", "h_p25", "h_p75"])
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                head = [c.value for c in ws[1]]
                if "folder" not in head or "notes" not in head:
                    continue
                fi, ni = head.index("folder"), head.index("notes")
                for r in ws.iter_rows(min_row=2, values_only=True):
                    if not r[fi]:
                        continue
                    key = os.path.normpath(resolve_review_folder(str(r[fi])))
                    f = feats.get(key)
                    if not f:
                        continue
                    hs = sorted(f["heights"]) or [0]
                    def q(p):
                        return hs[min(len(hs) - 1, int(p * len(hs)))]
                    w.writerow([
                        os.path.relpath(key, LIBRARY_ROOT) if key.startswith(LIBRARY_ROOT) else key,
                        truth_of(r[ni]) or "", f["n"], f["fig"],
                        round(f["fig"] / max(f["n"], 1), 3),
                        len(CORE_SLOTS & set(f["slots"])),
                        ";".join(f"{k}:{v}" for k, v in f["slots"].most_common()),
                        f["lin"].most_common(1)[0][0] if f["lin"] else "",
                        round(hs[0], 3), round(q(0.5), 3), round(hs[-1], 3),
                        round(q(0.25), 3), round(q(0.75), 3)])
        print(f"\nWrote {out}")
        print("Every labelled folder with its measured heights, as a fraction of "
              "its lineage's reference height.")

    if not apply_to:
        print("\nRead-only. Add --dump to write the feature table, or "
              "--apply \"Tau\" to fill that sheet's blank notes.")
        return
    if apply_to not in wb.sheetnames:
        sys.exit(f"No sheet called {apply_to!r}")
    ws = wb[apply_to]
    head = [c.value for c in ws[1]]
    fi, ni = head.index("folder") + 1, head.index("notes") + 1
    filled = skipped = 0
    for row in range(2, ws.max_row + 1):
        if str(ws.cell(row, ni).value or "").strip():
            skipped += 1
            continue
        f = feats.get(os.path.normpath(resolve_review_folder(
            str(ws.cell(row, fi).value or ""))))
        p = predict_content(f) if f else None
        if not p:
            continue
        ws.cell(row, ni).value = ("each file is a full character [predicted]"
                                 if p == "characters" else "parts [predicted]")
        filled += 1
    wb.save(REVIEW_SHEET)
    print(f"\n{apply_to}: filled {filled} blank notes, left {skipped} of yours alone.")
    print("Every one is tagged [predicted] — search for that to review or undo them.")


def apply_dropdowns(wb):
    """(Re)build the hidden choice lists and re-attach every dropdown.

    This has to run on EVERY save, not just when a sheet is created. Excel
    rewrites a cross-sheet list validation into its x14 extension format, and
    openpyxl drops that on read — the "Data Validation extension is not
    supported and will be removed" warning. So one load-and-save round trip
    after Excel has touched the file silently strips the dropdowns from every
    sheet at once. Re-applying them each time makes that self-healing.

    Excel also caps an INLINE list at 255 characters and corrupts the workbook
    past it, which is why the choices live on a hidden sheet and the validation
    points at a range.
    """
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.utils import get_column_letter

    lineages = sorted(LINEAGE_HEIGHT) + ["vehicle", "accessory", "various"]
    factions = [ANY_FACTION] + sorted(set(FACTION_GROUPS) | set(FACTION_LINEAGES))
    lists = wb["_choices"] if "_choices" in wb.sheetnames else wb.create_sheet("_choices")
    for row in list(lists.iter_rows()):
        for cell in row:
            cell.value = None
    for i, v in enumerate(lineages, 1):
        lists.cell(i, 1, v)
    for i, v in enumerate(factions, 1):
        lists.cell(i, 2, v)
    lists.sheet_state = "hidden"

    iy = SHEET_FIELDS.index("your_lineage") + 1
    ifac = SHEET_FIELDS.index("faction") + 1
    done = 0
    for name in wb.sheetnames:
        ws = wb[name]
        head = [c.value for c in ws[1]]
        if "folder" not in head or "your_lineage" not in head:
            continue
        n = ws.max_row
        if n < 2:
            continue
        ws.data_validations.dataValidation = []      # drop any stale copies
        dv = DataValidation(type="list", allow_blank=True, showDropDown=False,
                            formula1=f"=_choices!$A$1:$A${len(lineages)}")
        ws.add_data_validation(dv)
        dv.add(f"{get_column_letter(iy)}2:{get_column_letter(iy)}{n}")
        if "faction" in head:
            dvf = DataValidation(type="list", allow_blank=True, showDropDown=False,
                                 formula1=f"=_choices!$B$1:$B${len(factions)}")
            ws.add_data_validation(dvf)
            dvf.add(f"{get_column_letter(ifac)}2:{get_column_letter(ifac)}{n}")
        done += 1
    return done


def cmd_dropdowns(argv):
    """Restore the lineage and faction dropdowns on every review sheet."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit(f'openpyxl is required:\n  "{sys.executable}" -m pip install openpyxl')
    if not os.path.exists(REVIEW_SHEET):
        sys.exit(f"No review sheet at {REVIEW_SHEET}")
    wb = load_workbook(REVIEW_SHEET)
    n = apply_dropdowns(wb)
    wb.save(REVIEW_SHEET)
    print(f"Dropdowns restored on {n} sheets in {REVIEW_SHEET}")
    print("Close the workbook in Excel before running this, or the save is lost.")


def cmd_make_sheet(argv):
    """Build (or refresh) one review sheet from filenames alone — no indexing."""
    if not argv:
        sys.exit('Usage: make-sheet "<folder>" [sheet name]\n'
                 '  e.g. make-sheet "/path/to/stls/Grey Knights"')
    root = _abs(argv[0]).rstrip("/")
    name = (argv[1] if len(argv) > 1 else os.path.basename(root))[:31]
    if not os.path.isdir(root):
        sys.exit(f"Not a folder: {root}")
    try:
        from openpyxl import load_workbook, Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        sys.exit(f'openpyxl is required:\n  "{sys.executable}" -m pip install openpyxl')

    print(f"Scanning names under {root} ...")
    t0 = time.time()
    rows = survey_folder_names(root)
    print(f"  {len(rows)} model folders in {time.time()-t0:.1f}s")

    wb = load_workbook(REVIEW_SHEET) if os.path.exists(REVIEW_SHEET) else Workbook()
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1:
        del wb["Sheet"]
    # keep any verdicts already recorded for these folders
    # Verdicts are matched on the RESOLVED absolute folder, not the literal
    # cell text, so a sheet written with a different path form (or on another
    # computer) keeps its verdicts.
    previous = {}
    if name in wb.sheetnames:
        old = wb[name]
        head = [c.value for c in old[1]]
        if "folder" in head:
            fi = head.index("folder")
            idx = {c: head.index(c) for c in SHEET_EDITABLE if c in head}
            for r in old.iter_rows(min_row=2, values_only=True):
                if r[fi]:
                    previous[os.path.normpath(resolve_review_folder(str(r[fi])))] = {
                        c: r[i] for c, i in idx.items()}
        del wb[name]
    kept = 0
    for r in rows:
        key = os.path.normpath(resolve_review_folder(r["folder"]))
        old_vals = previous.get(key)
        if not old_vals:
            continue
        if any((old_vals.get(c) or "").strip() for c in SHEET_EDITABLE):
            kept += 1
        for c in SHEET_EDITABLE:
            v = (old_vals.get(c) or "").strip()
            if v:
                # A faction you corrected outranks the one derived from the path;
                # that is the whole point of the column.
                r[c] = v

    ws = wb.create_sheet(name)
    ws.append(SHEET_FIELDS)
    HDR = PatternFill("solid", fgColor="1F3B2C")
    EDIT = PatternFill("solid", fgColor="FFF6D5")
    DEF = PatternFill("solid", fgColor="FFF0F0")
    VEH = PatternFill("solid", fgColor="E4E4E4")
    ACCF = PatternFill("solid", fgColor="EDE7F6")
    for c in range(1, len(SHEET_FIELDS) + 1):
        cell = ws.cell(1, c)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = HDR
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([r[f] for f in SHEET_FIELDS])
    n = ws.max_row
    for row in ws.iter_rows(min_row=2, max_row=n, max_col=len(SHEET_FIELDS)):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
    iy = SHEET_FIELDS.index("your_lineage") + 1
    ino = SHEET_FIELDS.index("notes") + 1
    im = SHEET_FIELDS.index("matched_text") + 1
    ila = SHEET_FIELDS.index("lineage_assigned") + 1
    for rr in range(2, n + 1):
        v = ws.cell(rr, ila).value
        if v == "vehicle":
            for cc in range(1, len(SHEET_FIELDS) + 1):
                ws.cell(rr, cc).fill = VEH
        elif v == "accessory":
            for cc in range(1, len(SHEET_FIELDS) + 1):
                ws.cell(rr, cc).fill = ACCF
        else:
            ws.cell(rr, iy).fill = EDIT
            ws.cell(rr, ino).fill = EDIT
            if str(ws.cell(rr, im).value or "").startswith("(default"):
                ws.cell(rr, im).fill = DEF
    apply_dropdowns(wb)
    widths = {"folder": 52, "label": 40, "files": 7, "lineage_assigned": 17,
              "lineage_from_folder_name": 24, "matched_text": 30, "agreement": 11,
              "lineage_mix": 26, "slots_found": 34, "faction": 22,
              "your_lineage": 15, "notes": 46}
    for i, f in enumerate(SHEET_FIELDS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(f, 16)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(SHEET_FIELDS))}{n}"
    wb.save(REVIEW_SHEET)

    veh = sum(1 for r in rows if r["_kind"] == "vehicle")
    acc = sum(1 for r in rows if r["_kind"] == "accessory")
    print(f"Sheet '{name}' written to {REVIEW_SHEET}")
    print(f"    vehicles pre-marked   : {veh}")
    print(f"    accessories pre-marked: {acc}")
    print(f"    left for you to review: {len(rows)-veh-acc}")
    if kept:
        print(f"    existing verdicts kept: {kept}")


def resolve_review_folder(value):
    """Turn a sheet's folder value into an absolute path on THIS machine.

    Sheets store paths relative to the library root, so a workbook keeps
    working when the drive mounts somewhere else. An absolute path is used as
    it is when it exists; otherwise its trailing segments are re-tried under the
    library root, longest first, which rescues a sheet written on another
    computer.
    """
    value = str(value).strip()
    if os.path.isabs(value):
        if os.path.isdir(value) or not LIBRARY_ROOT:
            return value
        parts = [p for p in re.split(r"[\\/]+", value) if p]
        for i in range(1, len(parts)):
            cand = os.path.join(LIBRARY_ROOT, *parts[i:])
            if os.path.isdir(cand):
                return cand
        return value
    return os.path.join(LIBRARY_ROOT, value)


def cmd_import_review(argv):
    """Load hand-verified folder verdicts from the review spreadsheet.

    Every row is one model folder. A verdict in `your_lineage` wins; a blank
    means the classifier's own answer was correct. Free-text notes are mined
    for the parts-vs-characters distinction, which the Characters view needs
    and which no filename rule can infer.
    """
    src = argv[0] if argv else REVIEW_SHEET
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit(f'openpyxl is required to read the review sheet:\n'
                 f'  "{sys.executable}" -m pip install openpyxl')
    if not os.path.exists(src):
        sys.exit(f"Review sheet not found: {src}")
    wb = load_workbook(src, data_only=True)
    db = connect()
    cur = db.cursor()

    kept = collections.Counter()
    rows = 0
    for sheet in wb.sheetnames:
        if sheet == "How to use":
            continue
        ws = wb[sheet]
        head = [c.value for c in ws[1]]
        try:
            i_folder = head.index("folder")
            i_your = head.index("your_lineage")
            i_assigned = head.index("lineage_assigned")
            i_notes = head.index("notes")
        except ValueError:
            continue
        # Sheets written before the faction column exists simply lack it.
        i_faction = head.index("faction") if "faction" in head else None
        for r in ws.iter_rows(min_row=2, values_only=True):
            rel = str(r[i_folder] or "").strip()
            if not rel:
                continue
            rows += 1
            verdict = str(r[i_your] or "").strip().lower()
            assigned = str(r[i_assigned] or "").strip().lower()
            note = str(r[i_notes] or "").strip()
            # A blank verdict means "the classifier was right" — so record NO
            # override and let it decide afresh at classify time. Copying the
            # sheet's suggestion instead would freeze whatever the rules said on
            # the day the sheet was generated: a Chaos Spawn reviewed while it
            # still read `cultist` would keep importing as cultist forever, even
            # after the rule was fixed.
            value = verdict

            lineage = kind = faction_override = None
            if value in ("vehicle", "accessory"):
                kind = value
            elif value in VALID_LINEAGES:
                lineage = value
            elif value == "various":
                pass                              # mixed folder: judge per file
            elif value:
                kept["unrecognised: " + value] += 1

            low = note.lower()
            # The faction COLUMN is the primary signal now — it is pre-filled
            # from the folder's location and you correct it where that is wrong.
            # A faction named in the notes still works, for the sheets written
            # before the column existed.
            faction_override = None
            if i_faction is not None:
                col = str(r[i_faction] or "").strip()
                if col and col != (faction_of(resolve_review_folder(rel)) or ""):
                    faction_override = col
            if faction_override is None:
                faction_override = faction_in_notes(note)
            if faction_override is None and re.search(
                    r"\bchaos\b|heretic|traitor legion", low):
                faction_override = ("Chaos Daemons" if "daemon" in low or "demon" in low
                                    else "Chaos Space Marines")
            if re.search(r"full (character|figure)|each file is a full", low):
                content = "characters"
            elif low.startswith("parts") or re.search(r"\bparts\b", low):
                content = "parts"
            else:
                content = None

            folder = resolve_review_folder(rel)
            cur.execute("INSERT OR REPLACE INTO folder_rules"
                        "(folder,lineage,kind,content,faction,notes,source) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (folder, lineage, kind, content, faction_override,
                         note or None, "review-sheet"))
            kept["lineage " + lineage if lineage else
                 "kind " + kind if kind else "no override"] += 1
            if content:
                kept["content " + content] += 1
            if faction_override:
                kept["moved to " + faction_override] += 1
    db.commit()
    print(f"Imported {rows} folder verdicts from {os.path.basename(src)}")
    for k, v in sorted(kept.items()):
        print(f"    {k:28s} {v}")
    print("\nNow run `classify` to apply them.")


def cmd_classify(argv):
    """Re-run classification from the database. Never re-reads the drive."""
    db = connect()
    cur = db.cursor()
    rules = {r[0]: r for r in cur.execute(
        "SELECT folder,lineage,kind,content,faction FROM folder_rules")}
    rows = cur.execute("SELECT id,name,parent,grandparent,dim_x,dim_y,dim_z,tris,"
                       "path FROM parts WHERE kind='mesh' OR kind IS NULL").fetchall()
    changed = 0
    forced = collections.Counter()
    for pid, name, parent, gp, dx, dy, dz, tris, path in rows:
        slot, conf, lineage, whole, sprue, role, vehicle = classify_row(
            name or "", parent or "", gp or "", [dx or 0, dy or 0, dz or 0],
            tris or 0, faction_of(path or ""),
            os.path.basename(model_folder(path or "")))

        # A hand-verified folder verdict overrides everything the filename says.
        # Your rule: a folder given an armour lineage applies it to every part
        # inside, because the folder is the model and the files are its pieces.
        row_faction = faction_of(path or "")
        # Recompute the scale pack from the path. It used to be written once at
        # index time, which froze it: any later change to what counts as a
        # packaging folder left already-indexed rows grouped the old way, and
        # scale factors are estimated per pack. Deriving it here means those
        # fixes land on a `classify` run instead of needing a re-index.
        try:
            new_pack = os.path.relpath(model_folder(path or ""), LIBRARY_ROOT)
        except ValueError:
            new_pack = None
        rule = rules.get(model_folder(path or ""))
        if rule:
            _, r_lin, r_kind, r_content, r_faction = rule
            if r_lin:
                lineage = r_lin; forced["lineage"] += 1
            if r_faction:
                row_faction = r_faction; forced["faction moved"] += 1
            if r_kind == "vehicle":
                vehicle = True; forced["vehicle"] += 1
            if r_content == "characters":
                whole = True; forced["whole"] += 1
            elif r_content == "parts":
                whole = False; forced["parts"] += 1
        # Lineage has the final say on character-vs-parts. A dreadnought folder
        # noted as "parts" is still one dreadnought, so it must never be split
        # into components regardless of what the note said.
        if lineage in CHARACTER_ONLY_LINEAGES:
            whole = True
            forced["character-only lineage"] += 1
        if is_epic(path or ""):
            vehicle = True; forced["epic-excluded"] += 1
        cur.execute("UPDATE parts SET slot=?,slot_conf=?,side=?,role=?,faction=?,"
                    "subfaction=?,lineage=?,unit_class=?,is_whole=?,is_sprue=?,"
                    "is_vehicle=?,pack=COALESCE(?,pack) WHERE id=?",
                    (slot, conf, detect_side(name or ""), role,
                     row_faction, subfaction_of(path or ""), lineage,
                     CLASS_OF_LINEAGE.get(lineage, "infantry"),
                     int(whole), int(sprue), int(vehicle), new_pack, pid))
        changed += 1
    db.commit()
    print(f"Re-classified {changed} parts (user tags preserved)")
    if forced:
        print("  applied from your review: "
              + ", ".join(f"{k}={v}" for k, v in sorted(forced.items())))
    estimate_pack_scales(db)
    cmd_stats([])


def cmd_stats(argv):
    db = connect()
    c = db.cursor()
    total = c.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    if not total:
        print("Library is empty — run `index` first.")
        return
    meshes = c.execute("SELECT COUNT(*) FROM parts WHERE kind='mesh'").fetchone()[0] or 1
    usable = c.execute("SELECT COUNT(*) FROM parts WHERE excluded=0 AND is_whole=0 "
                       "AND is_vehicle=0 AND COALESCE(user_slot,slot) IS NOT NULL").fetchone()[0]
    print(f"\n{'='*58}\nLIBRARY: {total} files scanned, {meshes} are real meshes")
    print(f"  usable single components : {usable} ({100*usable/meshes:.0f}% of meshes)")
    for label, q in (("whole figures (hidden)", "is_whole=1"),
                     ("vehicle parts (hidden)", "is_vehicle=1"),
                     ("sprues/sets (flagged)", "is_sprue=1"),
                     ("images mislabelled .stl", "kind='image'"),
                     ("genuinely unreadable", "kind='unreadable'"),
                     ("unclassified -> review queue",
                      "COALESCE(user_slot,slot) IS NULL AND is_whole=0 AND is_vehicle=0 AND excluded=0")):
        n = c.execute(f"SELECT COUNT(*) FROM parts WHERE {q}").fetchone()[0]
        print(f"  {label:26s} : {n}")
    print("\nBY SLOT")
    for slot, n in c.execute(
            "SELECT COALESCE(user_slot,slot) s,COUNT(*) FROM parts WHERE excluded=0 "
            "AND is_whole=0 AND is_vehicle=0 AND s IS NOT NULL GROUP BY s ORDER BY COUNT(*) DESC"):
        print(f"  {slot:16s} {n}")
    print("\nBY LINEAGE")
    for lin, n in c.execute("SELECT lineage,COUNT(*) FROM parts WHERE excluded=0 "
                            "GROUP BY lineage ORDER BY COUNT(*) DESC"):
        print(f"  {lin or '?':16s} {n}")
    p = c.execute("SELECT COUNT(*),AVG(scale_factor),MIN(scale_factor),MAX(scale_factor) "
                  "FROM packs").fetchone()
    if p and p[0]:
        print(f"\nPACK SCALE FACTORS: {p[0]} packs, "
              f"avg x{p[1]:.3f}, range x{p[2]:.4f} - x{p[3]:.2f}")
    print("=" * 58)


# How far a part may stray from its expected size before we stop trusting the
# pack-wide factor. Real parts vary a lot, so this only catches gross errors.
SANE_LO, SANE_HI = 0.45, 2.2

SLOTS = [
    {"id": "head", "slot": "head", "label": "Head"},
    {"id": "torso", "slot": "torso", "label": "Torso", "anchor": True},
    {"id": "legs", "slot": "legs", "label": "Legs"},
    {"id": "arm_l", "slot": "arm", "label": "Left Arm", "side": "L"},
    {"id": "arm_r", "slot": "arm", "label": "Right Arm", "side": "R"},
    {"id": "pauldron_l", "slot": "pauldron", "label": "Left Pauldron", "side": "L"},
    {"id": "pauldron_r", "slot": "pauldron", "label": "Right Pauldron", "side": "R"},
    {"id": "backpack", "slot": "backpack", "label": "Backpack"},
    {"id": "weapon_ranged", "slot": "weapon_ranged", "label": "Ranged Weapons"},
    {"id": "weapon_melee", "slot": "weapon_melee", "label": "Melee Weapons"},
    {"id": "base", "slot": "base", "label": "Base"},
    {"id": "accessory", "slot": "accessory", "label": "Accessories"},
]


# =========================== GUI (serve) ==============================
def write_scaled_stl(src, dst, scale):
    import numpy as np
    info = read_stl(src)
    if info is None:
        return False
    v = np.ascontiguousarray(info["v"], dtype=np.float32) * float(scale)
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = (n / np.maximum(ln, 1e-9)).astype(np.float32)
    rec = np.zeros((len(v), 50), dtype=np.uint8)
    rec[:, 0:12] = n.view(np.uint8).reshape(-1, 12)
    rec[:, 12:48] = v.reshape(len(v), 9).view(np.uint8).reshape(-1, 36)
    with open(dst, "wb") as f:
        f.write(b"scaled by kitbash.py".ljust(80, b"\0"))
        f.write(struct.pack("<I", len(v)))
        f.write(rec.tobytes())
    return True


# --------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def part_scale(row, anchor_lineage):
    """Export correction, sanity check, then lineage matching against the anchor.

    The pack-wide factor is the more reliable signal, but it assumes every file
    in a pack shares one export scale. When that assumption breaks the result
    can be absurd (a 2.9mm head), so anything landing far outside the expected
    size for its slot falls back to a per-part estimate instead.
    """
    base = row["scale_factor"] or 1.0
    slot = (row["user_slot"] or row["slot"] or "")
    lin = row["lineage"] or "firstborn"
    mx = max(row["dim_x"] or 0, row["dim_y"] or 0, row["dim_z"] or 0)
    per_part = False
    if mx > 0 and slot:
        exp = expected_mm(slot, lin)
        got = base * mx
        if exp > 0 and (got > SANE_HI * exp or got < SANE_LO * exp):
            base = exp / mx
            per_part = True
    if anchor_lineage in LINEAGE_HEIGHT and lin in LINEAGE_HEIGHT:
        base *= LINEAGE_HEIGHT[anchor_lineage] / LINEAGE_HEIGHT[lin]
    return base


def scale_payload(row, anchor_lineage):
    """Scale plus the resulting real-world size, computed once, server-side."""
    slot = (row["user_slot"] or row["slot"] or "")
    lin = row["lineage"] or "firstborn"
    mx = max(row["dim_x"] or 0, row["dim_y"] or 0, row["dim_z"] or 0)
    exp = expected_mm(slot, lin) if slot else 0
    got = (row["scale_factor"] or 1.0) * mx
    per_part = bool(exp and mx and (got > SANE_HI * exp or got < SANE_LO * exp))
    sc = part_scale(row, anchor_lineage)
    dims = [round((row["dim_x"] or 0) * sc, 1), round((row["dim_y"] or 0) * sc, 1),
            round((row["dim_z"] or 0) * sc, 1)]
    return {"scale": round(sc, 5), "per_part": per_part, "dims": dims,
            "lineage": lin, "name": row["name"]}


def clamp_scale(value, fallback):
    """Never trust a scale off the wire: reject NaN, zero and absurd values."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if not (v == v) or v <= 0 or v > 1000:      # v != v catches NaN
        return fallback
    return v


def query_parts(conn, slot=None, side=None, q=None, unit_class=None, lineage=None,
                unclassified=False, limit=200, offset=0, max_bulk=None, role=None,
                faction=None, group=None, subfaction=None):
    where, args = ["excluded=0", "is_whole=0", "is_vehicle=0", "kind='mesh'"], []
    if group and group != "any":
        fl = factions_in_group(group)
        if fl:
            where.append("faction IN (%s)" % ",".join("?" * len(fl)))
            args += fl
        else:
            where.append("1=0")
    if HIDDEN_LINEAGES:
        where.append("(lineage IS NULL OR lineage NOT IN (%s))"
                     % ",".join("?" * len(HIDDEN_LINEAGES)))
        args += sorted(HIDDEN_LINEAGES)
    if faction and faction != "any":
        # ANY_FACTION belongs to every army, so a faction filter matches it too.
        where.append("(faction=? OR faction=?)")
        args += [faction, ANY_FACTION]
    if subfaction and subfaction != "any":
        if subfaction == "(none)":
            where.append("subfaction IS NULL")
        else:
            where.append("subfaction=?")
            args.append(subfaction)
    if max_bulk is not None:
        allowed = [k for k, v in LINEAGE_BULK.items() if v <= max_bulk + 1e-6]
        if allowed:
            where.append("(lineage IN (%s) OR lineage IS NULL)"
                         % ",".join("?" * len(allowed)))
            args += allowed
    if role:
        where.append("role=?")
        args.append(role)
    if unclassified:
        where.append("COALESCE(user_slot,slot) IS NULL")
    elif slot:
        where.append("COALESCE(user_slot,slot)=?")
        args.append(slot)
    if side:
        where.append("(side=? OR side IS NULL)")
        args.append(side)
    if unit_class and unit_class != "any":
        where.append("unit_class=?")
        args.append(unit_class)
    if lineage and lineage != "any":
        where.append("lineage=?")
        args.append(lineage)
    if q:
        for term in q.split():
            where.append("(name LIKE ? OR pack LIKE ?)")
            args += [f"%{term}%", f"%{term}%"]
    # Must cover every column row_json reads. This is a fixed list rather than
    # SELECT *, so adding a field to row_json without adding it here raises
    # "IndexError: No item with that key" at request time, not at import.
    sql = ("SELECT id,name,pack,slot,user_slot,side,role,lineage,unit_class,dim_x,"
           "dim_y,dim_z,tris,scale_factor,thumb,mesh,path,faction,subfaction,"
           "is_whole FROM parts WHERE "
           + " AND ".join(where) + " ORDER BY slot_conf DESC, name LIMIT ? OFFSET ?")
    rows = conn.execute(sql, args + [limit, offset]).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM parts WHERE " + " AND ".join(where),
                         args).fetchone()[0]
    return rows, total


def build_characters(conn, faction=None, lineage=None, q=None, group=None,
                     subfaction=None):
    """Group the library into selectable whole miniatures.

    Two kinds qualify:
      * a single complete STL — one file, one character;
      * a folder whose parts cover most of the core slots.

    A folder holding SEVERAL complete figures is a collection, not a character,
    so each of its complete files is listed separately instead. Folders of loose
    components (all pauldrons, all weapons, accessory packs) are excluded.

    Vehicles and dreadnoughts are deliberately included here — unlike the parts
    builder, a whole vehicle is a legitimate thing to pick.
    """
    where, args = ["excluded=0", "kind='mesh'"], []
    if group and group != "any":
        fl = factions_in_group(group)
        fl = list(fl) + [ANY_FACTION]
        where.append("faction IN (%s)" % ",".join("?" * len(fl)) if fl else "1=0")
        args += fl
    if faction and faction != "any":
        # ANY_FACTION belongs to every army, so a faction filter matches it too.
        where.append("(faction=? OR faction=?)")
        args += [faction, ANY_FACTION]
    if subfaction and subfaction != "any":
        if subfaction == "(none)":
            where.append("subfaction IS NULL")
        else:
            where.append("subfaction=?")
            args.append(subfaction)
    if lineage and lineage != "any":
        where.append("lineage=?")
        args.append(lineage)
    rows = conn.execute(
        "SELECT id,name,path,slot,slot_conf,user_slot,is_whole,is_sprue,is_vehicle,"
        "lineage,faction,thumb,scale_factor,dim_x,dim_y,dim_z FROM parts WHERE "
        + " AND ".join(where), args).fetchall()

    root = LIBRARY_ROOT
    groups = {}
    for r in rows:
        groups.setdefault(character_key(r["path"], root), []).append(r)

    chars = []
    for key, files in groups.items():
        # Completeness wording alone is not enough — it must also measure like a
        # figure and not be a confidently identified component.
        # Vehicles, dreadnoughts, titans and primarchs live ONLY here — never in
        # the parts builder — so a folder of their pieces IS the model, not a
        # bitz pack, and qualifies as a character however its notes read.
        lin_count = collections.Counter(f["lineage"] for f in files if f["lineage"])
        dominant = lin_count.most_common(1)[0][0] if lin_count else None
        big_model = (dominant in HIDDEN_LINEAGES) or any(f["is_vehicle"] for f in files)

        # A standalone miniature measures like a figure, was not identified as a
        # component, and — inside a multi-file folder — is clearly bigger than
        # its neighbours. Without that last test a single oversized weapon bit
        # hijacks the folder and hides the model it belongs to.
        sizes = sorted(max(f["dim_x"] or 0, f["dim_y"] or 0, f["dim_z"] or 0)
                       * (f["scale_factor"] or 1.0) for f in files)
        median = sizes[len(sizes) // 2] if sizes else 0

        def standalone(f):
            if not looks_like_figure(f):
                return False
            if f["is_whole"]:
                return True
            if (f["user_slot"] or f["slot"]):
                return False
            if len(files) > 2 and median > 0:
                mx = max(f["dim_x"] or 0, f["dim_y"] or 0, f["dim_z"] or 0) * (f["scale_factor"] or 1.0)
                return mx >= 1.4 * median
            return True

        wholes = [f for f in files if standalone(f)]
        slots = {(f["user_slot"] or f["slot"]) for f in files} - {None}
        core = len(CORE_SLOTS & slots)
        # A figure needs a body. This is what separates a real character folder
        # from a bitz pack of heads, arms and weapons.
        has_body = bool({"torso", "legs"} & slots)
        label_src = key.split(os.sep)[-1] if key else ""

        # A lone file is its own character, named after the file. Without this
        # a model sitting directly in a category folder like "01_HQ" would be
        # listed under the category name instead of its own.
        if len(files) == 1:
            f = files[0]
            if looks_like_figure(f):
                chars.append({
                    "id": f"file:{f['id']}", "label": pretty_label(f["name"]),
                    "folder": pretty_label(key), "files": 1,
                    "lineage": f["lineage"], "faction": f["faction"],
                    "kind": "single", "thumb_id": f["id"]})
            continue

        # `not big_model` matters: a dreadnought or vehicle folder is ONE model
        # in pieces, so it must never be split into "several finished figures"
        # just because a couple of large parts look figure-sized on their own.
        if len(wholes) >= 2 and not big_model:
            # a folder of finished figures: each one is its own character
            for f in wholes:
                chars.append({
                    "id": f"file:{f['id']}", "label": pretty_label(f["name"]),
                    "folder": pretty_label(key), "files": 1,
                    "lineage": f["lineage"], "faction": f["faction"],
                    "kind": "single", "thumb_id": f["id"]})
            continue
        if not wholes:
            if ACCESSORY_FOLDER.search(strip_loadout(norm_name(label_src))):
                continue
            # A big model's pieces always form one character; ordinary infantry
            # folders still need most of their core parts plus a body.
            if not big_model and (core < MIN_CORE_SLOTS or not has_body):
                continue
        chars.append({
            "id": f"pack:{key}", "label": pretty_label(key),
            "folder": pretty_label(os.path.dirname(key)) if os.path.dirname(key) else "",
            "files": len(files),
            "lineage": collections.Counter(
                f["lineage"] for f in files if f["lineage"]).most_common(1)[0][0]
            if any(f["lineage"] for f in files) else None,
            "faction": next((f["faction"] for f in files if f["faction"]), None),
            "kind": "assembled" if wholes else "multi-part",
            "thumb_id": next((f["id"] for f in files if f["thumb"]), files[0]["id"])})

    if q:
        ql = q.lower()
        chars = [c for c in chars if ql in c["label"].lower()
                 or ql in (c["folder"] or "").lower()]
    chars.sort(key=lambda c: c["label"].lower())
    return chars


def character_files(conn, cid, anchor=None):
    """Every mesh belonging to one character, one copy of each part.

    Returns (files, dropped). A pack that ships raw meshes, supported meshes and
    printer plates would otherwise export the same piece several times over.
    """
    if cid.startswith("file:"):
        rows = conn.execute("SELECT * FROM parts WHERE id=?",
                            (int(cid[5:]),)).fetchall()
        dropped = 0
    else:
        key = cid[5:]
        rows = [r for r in conn.execute(
            "SELECT * FROM parts WHERE excluded=0 AND kind='mesh'").fetchall()
            if character_key(r["path"], LIBRARY_ROOT) == key]
        rows, dropped = prefer_variants(rows)
    out = []
    for r in rows:
        anc = anchor or r["lineage"] or "firstborn"
        j = row_json(r, anc)
        j["is_whole"] = bool(r["is_whole"])
        out.append(j)
    out.sort(key=lambda j: (j["slot"] or "zz", j["name"]))
    return out, dropped


def row_json(r, anchor="firstborn"):
    raw = [r["dim_x"] or 0, r["dim_y"] or 0, r["dim_z"] or 0]
    sp = scale_payload(r, anchor)
    return {
        "id": r["id"], "name": r["name"], "pack": r["pack"],
        "slot": r["user_slot"] or r["slot"], "side": r["side"],
        "role": r["role"], "lineage": r["lineage"] or "firstborn",
        # the review queue needs to say what it is looking at
        "faction": r["faction"], "subfaction": r["subfaction"],
        "is_whole": bool(r["is_whole"]),
        "unit_class": r["unit_class"], "raw_dims": [round(d, 1) for d in raw],
        "tris": r["tris"], "scale": sp["scale"], "per_part": sp["per_part"],
        "dims": sp["dims"],
        "has_thumb": bool(r["thumb"]), "has_mesh": bool(r["mesh"]),
    }


# ----------------------------------------------------------------- server
DB_LOCK = threading.Lock()
# Random per-run secret. Any page in your browser can reach a localhost server,
# so state-changing requests must prove they came from OUR page: they carry a
# custom header, which browsers refuse to send cross-origin without a preflight
# this server never answers. Without it, a site you happen to be visiting could
# POST to /api/clear and empty the Bundles folder.
SESSION_TOKEN = secrets.token_urlsafe(18)


class Handler(BaseHTTPRequestHandler):
    conn = None

    def log_message(self, *a):
        pass

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
            if origin not in allowed:
                return False
        return self.headers.get("X-KB-Token") == SESSION_TOKEN

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        one = lambda k, d=None: (qs.get(k) or [d])[0]

        if u.path == "/":
            page = PAGE.replace("__TOKEN__", SESSION_TOKEN)
            return self._send(200, page, "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"})

        if u.path == "/api/meta":
            c = self.conn
            counts = {}
            with DB_LOCK:
                for s in SLOTS:
                    rows, total = query_parts(c, s["slot"], s.get("side"), limit=0)
                    counts[s["id"]] = total
            factions = [r[0] for r in c.execute(
                "SELECT DISTINCT faction FROM parts WHERE faction IS NOT NULL "
                "AND excluded=0 AND is_whole=0 AND is_vehicle=0 ORDER BY faction") if r[0]]
            # Everything indexed, for the CHARACTERS view. Knights and Titans
            # could never reach the list above: `classify` forces is_whole on
            # any characters-only lineage, and every knight and titan lineage
            # is characters-only, so the parts-builder predicate excludes the
            # whole faction. The same is true of anything vehicle-flagged.
            factions_all = [r[0] for r in c.execute(
                "SELECT DISTINCT faction FROM parts WHERE faction IS NOT NULL "
                "AND excluded=0 ORDER BY faction") if r[0]]
            # Same predicate as the parts list, for the same reason as above:
            # a lineage that only exists on whole models or vehicle components
            # is not something the builder can offer.
            lineages = [r[0] for r in c.execute(
                "SELECT DISTINCT lineage FROM parts WHERE lineage IS NOT NULL "
                "AND excluded=0 AND is_whole=0 AND is_vehicle=0 "
                "AND COALESCE(user_slot,slot) IS NOT NULL ORDER BY lineage")
                if r[0] and r[0] not in HIDDEN_LINEAGES]
            subfactions_present = [r[0] for r in c.execute(
                "SELECT DISTINCT subfaction FROM parts WHERE subfaction IS NOT NULL "
                "AND excluded=0 ORDER BY subfaction") if r[0]]
            _, unclass = query_parts(c, unclassified=True, limit=0)
            total = c.execute("SELECT COUNT(*) FROM parts WHERE kind='mesh' "
                              "AND excluded=0 AND is_whole=0").fetchone()[0]
            return self._send(200, {
                "slots": SLOTS, "counts": counts, "factions": factions,
                "lineages": lineages,
                "all_lineages": sorted(LINEAGE_HEIGHT),
                "unclassified": unclass,
                "hidden_lineages": sorted(HIDDEN_LINEAGES),
                "total": total, "bundle_dir": BUNDLE_DIR,
                "groups": sorted({group_of(f) for f in factions} - {None}),
                "factions_all": factions_all,
                "subfactions": subfactions_present,
                "faction_subfactions": SUBFACTIONS,
                "faction_group": {f: group_of(f) for f in factions},
                "lineage_height": LINEAGE_HEIGHT, "slot_fraction": SLOT_FRACTION,
                "sane": [SANE_LO, SANE_HI], "lineage_bulk": LINEAGE_BULK,
                "bulk_thresholds": [BULK_MILD, BULK_STRONG],
                "slot_bulk_policy": SLOT_BULK_POLICY})

        if u.path == "/api/characters":
            with DB_LOCK:
                chars = build_characters(self.conn, one("faction"),
                                         one("lineage"), one("q"), one("group"),
                                         one("subfaction"))
            # The cap used to be 800, which is why the dropdown appeared to stop
            # around "L" while the search box could still reach everything: the
            # list is alphabetical, so a cap truncates the far end of the
            # alphabet rather than thinning it out. A <select> handles a few
            # thousand options comfortably; narrow with the filters or the
            # search box if you ever reach this one.
            return self._send(200, {"total": len(chars),
                                    "characters": chars[:5000]})

        if u.path == "/api/character":
            cid = one("id", "")
            with DB_LOCK:
                files, dropped = character_files(self.conn, cid,
                                                 one("anchor") or None)
            return self._send(200, {"id": cid, "files": files,
                                    "variants_hidden": dropped})

        if u.path == "/api/counts":
            # Slot headers show how many parts the current filters actually
            # leave available, so the numbers move as you narrow things down.
            anchor = one("anchor", "firstborn")
            counts = {}
            with DB_LOCK:
                for sdef in SLOTS:
                    mb = None
                    if SLOT_BULK_POLICY.get(sdef["slot"]) == "down_only":
                        mb = LINEAGE_BULK.get(anchor)
                    _, total = query_parts(
                        self.conn, sdef["slot"], sdef.get("side"), one("q"),
                        None, one("lineage"), False, 0, 0, mb, one("role"),
                        one("faction"), one("group"), one("subfaction"))
                    counts[sdef["id"]] = total
            where, args = ["excluded=0", "kind='mesh'"], []
            if one("group") and one("group") != "any":
                fl = factions_in_group(one("group"))
                fl = list(fl) + [ANY_FACTION]
                where.append("faction IN (%s)" % ",".join("?" * len(fl)) if fl else "1=0")
                args += fl
            if one("faction") and one("faction") != "any":
                where.append("(faction=? OR faction=?)")
                args += [one("faction"), ANY_FACTION]
            sub = one("subfaction")
            if sub and sub != "any":
                if sub == "(none)":
                    where.append("subfaction IS NULL")
                else:
                    where.append("subfaction=?")
                    args.append(sub)
            with DB_LOCK:
                present = [r[0] for r in self.conn.execute(
                    "SELECT DISTINCT lineage FROM parts WHERE " + " AND ".join(where)
                    + " AND lineage IS NOT NULL ORDER BY lineage", args) if r[0]]
                # The parts list is narrower than "everything indexed": it drops
                # whole models, vehicle components and anything with no slot.
                # Offering a lineage the slots cannot show gives you a filter
                # that returns nothing — which is exactly what `firstborn` and
                # `daemon_lesser` did under T'au, on the strength of one stray
                # file each. Ask the same question the parts list asks.
                buildable = [r[0] for r in self.conn.execute(
                    "SELECT DISTINCT lineage FROM parts WHERE " + " AND ".join(where)
                    + " AND lineage IS NOT NULL AND is_whole=0 AND is_vehicle=0"
                    " AND COALESCE(user_slot,slot) IS NOT NULL ORDER BY lineage",
                    args) if r[0]]
            return self._send(200, {
                "counts": counts,
                # everything present here, for the Characters view
                "lineages_all": present,
                # only those the parts builder can actually offer
                "lineages": [l for l in buildable if l not in HIDDEN_LINEAGES]})

        if u.path == "/api/parts":
            mb = one("max_bulk")
            anchor = one("anchor", "firstborn")
            with DB_LOCK:
                rows, total = query_parts(
                    self.conn, one("slot"), one("side"), one("q"), None,
                    one("lineage"), one("unclassified") == "1",
                    int(one("limit", "120")), int(one("offset", "0")),
                    float(mb) if mb else None, one("role"), one("faction"),
                    one("group"), one("subfaction"))
            return self._send(200, {"total": total,
                                    "parts": [row_json(r, anchor) for r in rows]})

        m = re.match(r"^/api/(thumb|mesh)/(\d+)$", u.path)
        if m:
            kind, pid = m.group(1), int(m.group(2))
            with DB_LOCK:
                r = self.conn.execute(f"SELECT {kind} FROM parts WHERE id=?",
                                      (pid,)).fetchone()
            if not r or not r[0] or not os.path.exists(r[0]):
                return self._send(404, {"error": "not found"})
            with open(r[0], "rb") as f:
                data = f.read()
            if kind == "thumb":
                return self._send(200, data, "image/png",
                                  {"Cache-Control": "max-age=86400"})
            # preview meshes are stored gzipped; let the browser inflate them
            return self._send(200, data, "application/octet-stream",
                              {"Content-Encoding": "gzip", "Cache-Control": "max-age=86400"})

        if u.path == "/api/bundles":
            out = []
            if os.path.isdir(BUNDLE_DIR):
                for d in sorted(os.listdir(BUNDLE_DIR)):
                    p = os.path.join(BUNDLE_DIR, d)
                    if os.path.isdir(p):
                        n = sum(len(f) for _, _, f in os.walk(p))
                        out.append({"name": d, "files": n})
            return self._send(200, {"bundles": out, "dir": BUNDLE_DIR})

        return self._send(404, {"error": "no such endpoint"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.same_origin():
            return self._send(403, {"error": "cross-origin request refused"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})

        if u.path == "/api/tag":
            slot = payload.get("slot") or None
            valid = {s["slot"] for s in SLOTS}
            # "whole" is not a slot — it says the file is a finished model, which
            # takes it out of the parts builder and into the Characters view.
            whole = payload.get("whole")
            if slot is not None and slot != "whole" and slot not in valid:
                return self._send(400, {"error": f"unknown slot {slot!r}"})
            if slot == "whole":
                slot, whole = None, True
            with DB_LOCK:
                if whole is None:
                    self.conn.execute("UPDATE parts SET user_slot=? WHERE id=?",
                                      (slot, int(payload["id"])))
                else:
                    self.conn.execute(
                        "UPDATE parts SET user_slot=?,is_whole=? WHERE id=?",
                        (slot, 1 if whole else 0, int(payload["id"])))
                self.conn.commit()
            return self._send(200, {"ok": True})

        if u.path == "/api/scales":
            # The server is the single source of truth for scale, so the page
            # never re-implements the formula and cannot drift out of step.
            anchor = payload.get("anchor") or "firstborn"
            out = {}
            with DB_LOCK:
                for pid in (payload.get("ids") or [])[:500]:
                    r = self.conn.execute("SELECT * FROM parts WHERE id=?",
                                          (int(pid),)).fetchone()
                    if r:
                        out[str(pid)] = scale_payload(r, anchor)
            return self._send(200, {"scales": out})

        if u.path == "/api/bundle":
            return self._send(200, self.make_bundle(payload))

        if u.path == "/api/clear":
            return self._send(200, self.clear_output(payload))

        return self._send(404, {"error": "no such endpoint"})

    # ---------------------------------------------------------- actions
    def make_bundle(self, payload):
        name = (payload.get("name") or "").strip()
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        if not safe:
            return {"error": "Give the bundle a name."}
        items = payload.get("items") or []
        if not items:
            return {"error": "Nothing selected."}
        anchor = payload.get("anchor_lineage") or "firstborn"

        target = os.path.join(BUNDLE_DIR, safe)
        os.makedirs(target, exist_ok=True)
        written, failed = [], []
        manifest = [f"BUNDLE: {safe}",
                    f"Created: {time.strftime('%Y-%m-%d %H:%M')}",
                    f"Build scale anchored to: {anchor} "
                    f"({LINEAGE_HEIGHT.get(anchor, 32)}mm reference)", ""]
        for it in items:
            with DB_LOCK:
                r = self.conn.execute("SELECT * FROM parts WHERE id=?",
                                      (int(it["id"]),)).fetchone()
            if not r:
                continue
            scale = clamp_scale(it.get("scale"), part_scale(r, anchor))
            slot = r["user_slot"] or r["slot"] or "part"
            base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", r["name"])
            out = os.path.join(target, f"{slot}__{base}")
            if not out.lower().endswith(".stl"):
                out += ".stl"
            n = 1
            while os.path.exists(out):
                out = os.path.join(target, f"{slot}__{n}__{base}")
                n += 1
            try:
                ok = write_scaled_stl(r["path"], out, scale)
            except Exception as ex:
                ok = False
                failed.append(f"{r['name']}: {ex}")
            if ok:
                d = [(r["dim_x"] or 0) * scale, (r["dim_y"] or 0) * scale,
                     (r["dim_z"] or 0) * scale]
                written.append(os.path.basename(out))
                manifest += [f"{slot}: {r['name']}",
                             f"    source : {r['path']}",
                             f"    pack   : {r['pack']}",
                             f"    lineage: {r['lineage']}",
                             f"    scale  : x{scale:.4f}",
                             f"    size   : {d[0]:.1f} x {d[1]:.1f} x {d[2]:.1f} mm", ""]
            else:
                failed.append(r["name"])
        with open(os.path.join(target, "manifest.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(manifest))
        return {"ok": True, "dir": target, "written": len(written),
                "failed": failed, "files": written}

    def clear_output(self, payload):
        """Empty the Bundles folder. Refuses to touch anything outside it."""
        if payload.get("confirm") != "CLEAR":
            return {"error": "not confirmed"}
        root = os.path.realpath(BUNDLE_DIR)
        if not os.path.isdir(root):
            return {"ok": True, "removed": 0, "note": "nothing to clear"}
        removed, lines = 0, [f"\n=== cleared {time.strftime('%Y-%m-%d %H:%M')} ==="]
        for entry in os.listdir(root):
            p = os.path.join(root, entry)
            # never follow a link out of the bundle folder
            if os.path.islink(p) or not os.path.realpath(p).startswith(root + os.sep):
                lines.append(f"SKIPPED (outside bundle folder): {p}")
                continue
            try:
                if os.path.isdir(p):
                    for dp, _, fns in os.walk(p):
                        for fn in fns:
                            lines.append(os.path.join(dp, fn))
                            removed += 1
                    shutil.rmtree(p)
                else:
                    lines.append(p)
                    removed += 1
                    os.unlink(p)
            except OSError as ex:
                lines.append(f"FAILED {p}: {ex}")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CLEAR_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return {"ok": True, "removed": removed, "log": CLEAR_LOG}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Kitbash Builder</title><style>
*{box-sizing:border-box}
body{margin:0;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 background:#15171c;color:#dfe3ea}
header{padding:10px 16px;background:#1c1f26;border-bottom:1px solid #2b303a;
 display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:20}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
select,input,button{background:#252a33;color:#dfe3ea;border:1px solid #39404d;
 border-radius:6px;padding:6px 9px;font:inherit}
button{cursor:pointer}button:hover{background:#2f3540}
button.primary{background:#2f6f4f;border-color:#3c8a63}button.primary:hover{background:#38855e}
button.danger{background:#6f2f34;border-color:#8a3c43}button.danger:hover{background:#853a41}
.wrap{display:flex;gap:14px;padding:14px;align-items:flex-start}
.slots{flex:1;min-width:0}
.side{width:330px;flex:none;position:sticky;top:60px}
.card{background:#1c1f26;border:1px solid #2b303a;border-radius:9px;margin-bottom:11px}
.slot-head{display:flex;align-items:center;gap:9px;padding:9px 12px;cursor:pointer;
 user-select:none}
.slot-head:hover{background:#20242c}
.slot-title{font-weight:600}
.count{color:#7f8896;font-size:11px}
.chip{display:inline-flex;align-items:center;gap:6px;background:#2b3a33;
 border:1px solid #3c5a4a;border-radius:20px;padding:2px 6px 2px 3px;margin:2px;font-size:11px}
.chip img{width:22px;height:22px;border-radius:50%;background:#111}
.chip b{cursor:pointer;color:#9fb;padding:0 2px}
.body{display:none;padding:0 12px 12px}
.body.open{display:block}
.tools{display:flex;gap:7px;margin:4px 0 9px}
.tools input{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px;
 max-height:400px;overflow:auto}
.tile{background:#20242c;border:1px solid #2b303a;border-radius:7px;padding:5px;
 cursor:pointer;text-align:center}
.tile:hover{border-color:#4b7cf0}
.tile.sel{border-color:#48b07a;background:#1e2b25}
.tile img{width:100%;aspect-ratio:1;object-fit:contain;background:#131519;border-radius:4px}
.tile .n{font-size:10px;color:#aeb6c2;margin-top:3px;word-break:break-word;
 max-height:26px;overflow:hidden}
.tile .d{font-size:9px;color:#6f7886}
.sel-list{max-height:230px;overflow:auto;margin:6px 0}
.sel-row{display:flex;gap:7px;align-items:center;padding:4px;border-bottom:1px solid #262b34}
.sel-row img{width:30px;height:30px;background:#111;border-radius:4px}
.sel-row .m{flex:1;min-width:0}
.sel-row .m div{font-size:10px;color:#8b93a1;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
.sel-row input{width:62px;padding:2px 4px;font-size:11px}
.warn{background:#3a2f1c;border:1px solid #6b5426;color:#ffd98a;padding:6px 9px;
 border-radius:6px;margin:7px 0;font-size:11px}
.ok{background:#1e3226;border:1px solid #38694a;color:#a6e9c1;padding:6px 9px;
 border-radius:6px;margin:7px 0;font-size:11px}
#viewer{position:fixed;inset:0;background:rgba(8,9,12,.9);display:none;z-index:50;
 align-items:center;justify-content:center}
#viewer .box{background:#181b21;border:1px solid #333a45;border-radius:11px;padding:12px;
 width:min(640px,92vw)}
canvas{width:100%;height:390px;background:#0e1014;border-radius:8px;display:block;
 cursor:grab}
.meta{display:flex;gap:14px;font-size:11px;color:#8b93a1;margin-top:8px;flex-wrap:wrap}
h3{margin:0 0 8px;font-size:13px}
.tabs{display:inline-flex;gap:0;border:1px solid #39404d;border-radius:7px;overflow:hidden}
.tab{border:0;border-radius:0;background:#20242c;padding:6px 13px}
.tab.on{background:#2f6f4f}
</style></head><body>
<header>
  <h1>Kitbash Builder</h1>
  <span class="tabs"><button id="tabParts" class="tab on">Parts Builder</button
    ><button id="tabChars" class="tab">Characters</button></span>
  <label>Group <select id="fGroup"><option value="any">any</option></select></label>
  <label>Faction <select id="fFaction"><option value="any">any</option></select></label>
  <label>Sub <select id="fSub"><option value="any">any</option></select></label>
  <label>Lineage <select id="fLin"><option value="any">any</option></select></label>
  <label>Build scale <select id="fAnchor"></select></label>
  <span id="libinfo" class="count"></span>
  <span style="flex:1"></span>
  <button id="btnReview">Review queue (<span id="uncount">0</span>)</button>
</header>
<div class="wrap" id="partsView">
  <div class="slots" id="slots"></div>
  <div class="side">
    <div class="card" style="padding:12px">
      <h3>Bundle</h3>
      <input id="bname" placeholder="Bundle name, e.g. Squad Alpha" style="width:100%">
      <div id="anchorNote" class="count" style="margin:7px 0"></div>
      <div id="selList" class="sel-list"></div>
      <div id="msg"></div>
      <div style="display:flex;gap:7px;margin-top:8px">
        <button class="primary" id="btnBundle" style="flex:1">Create Bundle</button>
        <button id="btnClearSel">Clear picks</button>
      </div>
      <hr style="border:0;border-top:1px solid #2b303a;margin:12px 0">
      <div class="count" id="bundleList"></div>
      <button class="danger" id="btnClearOut" style="width:100%;margin-top:8px">
        Clear Output Folder</button>
    </div>
  </div>
</div>
<div class="wrap" id="charView" style="display:none">
  <div class="slots">
    <div class="card" style="padding:12px">
      <h3>Whole characters &amp; entities</h3>
      <div class="tools">
        <input id="charSearch" placeholder="search characters..." style="flex:1">
        <select id="charPick" style="flex:2"></select>
      </div>
      <div class="count" id="charMeta"></div>
    </div>
    <div class="card" style="padding:12px">
      <h3 id="charTitle">Pieces</h3>
      <div class="grid" id="charFiles" style="max-height:620px"></div>
    </div>
  </div>
  <div class="side">
    <div class="card" style="padding:12px">
      <h3>Export character</h3>
      <input id="charName" placeholder="Bundle name" style="width:100%">
      <div class="count" id="charNote" style="margin:8px 0"></div>
      <div id="charMsg"></div>
      <button class="primary" id="btnCharExport" style="width:100%;margin-top:8px">
        Export all pieces</button>
      <hr style="border:0;border-top:1px solid #2b303a;margin:12px 0">
      <div class="count" id="charBundles"></div>
      <button class="danger" id="btnCharClear" style="width:100%;margin-top:8px">
        Clear Output Folder</button>
    </div>
  </div>
</div>
<div id="viewer"><div class="box">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b id="vName"></b><button id="vClose">Close</button></div>
  <canvas id="cv"></canvas>
  <div class="meta" id="vMeta"></div>
  <div class="count" style="margin-top:6px">Drag to rotate · scroll to zoom</div>
</div></div>
<script data-token="__TOKEN__">
// overrides: scales you typed by hand, kept separately from computed values so
// re-rendering the list can never silently discard them.
const S={meta:null,sel:{},open:{},cache:{},overrides:{},scales:{}};
const $=s=>document.querySelector(s);
const TOKEN=document.currentScript.dataset.token;
const api=(u,o)=>{
  o=o||{};
  if(o.method==='POST')
    o.headers=Object.assign({'Content-Type':'application/json','X-KB-Token':TOKEN},o.headers||{});
  return fetch(u,o).then(r=>r.json());
};
const post=(u,body)=>api(u,{method:'POST',body:JSON.stringify(body)});

// The faction list narrows to the chosen group, so picking "Imperium" leaves
// only Imperial factions selectable rather than showing everything.
function fillFactions(){
  const g=$('#fGroup').value, sel=$('#fFaction'), cur=sel.value;
  // The Characters view shows every faction present; the parts builder shows
  // only those with parts it can offer. Knights and Titans exist purely as
  // characters, so they appear in one list and not the other.
  const list = charsVisible()
    ? (S.meta.factions_all || S.meta.factions)
    : S.meta.factions;
  sel.innerHTML='<option value="any">any</option>';
  for(const f of list){
    if(g!=='any' && S.meta.faction_group[f]!==g) continue;
    const o=document.createElement('option'); o.value=f;
    o.textContent = (S.meta.factions.indexOf(f)<0) ? f+' (characters only)' : f;
    sel.appendChild(o);
  }
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'any';
}

// Sub-faction (chapter or legion) sits BELOW faction. Selecting a faction
// repopulates it; "(none)" isolates packs that belong to no chapter/legion.
// Dreadnoughts, titans, vehicles and primarchs are characters-only, so the
// parts builder must not offer them as filters — but the Characters view must.
function fillLineages(mode){
  const sel=$('#fLin'), cur=sel.value;
  // Only lineages that exist under the current group/faction/sub — no point
  // offering phobos while you are looking at Necrons.
  const avail = S.available || {};
  const list = mode==='chars'
    ? (avail.lineages_all || S.meta.all_lineages || S.meta.lineages)
    : (avail.lineages || S.meta.lineages);
  sel.innerHTML='<option value="any">any</option>';
  for(const l of list){
    const o=document.createElement('option'); o.value=l;
    o.textContent = (S.meta.lineages.indexOf(l)<0) ? l+' (characters only)' : l;
    sel.appendChild(o);
  }
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'any';
}

function fillSubs(){
  const f=$('#fFaction').value, sel=$('#fSub'), cur=sel.value;
  const list=(S.meta.faction_subfactions||{})[f]||[];
  const present=new Set(S.meta.subfactions||[]);
  sel.innerHTML='<option value="any">any</option>';
  const usable=list.filter(x=>present.has(x));
  for(const x of usable){
    const o=document.createElement('option'); o.value=o.textContent=x; sel.appendChild(o);
  }
  if(usable.length){
    const o=document.createElement('option'); o.value='(none)';
    o.textContent='(no chapter/legion)'; sel.appendChild(o);
  }
  sel.disabled=!usable.length;
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'any';
}

function anchorLineage(){
  const man=$('#fAnchor').value;
  if(man!=='auto') return man;
  for(const k of ['torso','legs']){
    const a=S.sel[k]; if(a&&a.length) return a[0].lineage;
  }
  return 'firstborn';
}
// Scale is computed by the server and delivered with each part, so there is no
// second copy of the formula here to drift out of step with the exporter.
function scaleFor(p){
  const o=S.overrides[p.id];
  return (o!==undefined)?o:p.scale;
}

async function boot(){
  S.meta=await api('/api/meta');
  $('#libinfo').textContent=S.meta.total+' parts';
  $('#uncount').textContent=S.meta.unclassified;
  for(const g of S.meta.groups){const o=document.createElement('option');o.value=o.textContent=g;$('#fGroup').appendChild(o);}
  fillFactions(); fillSubs();
  fillLineages('parts');
  const an=$('#fAnchor');
  an.innerHTML='<option value="auto">match torso</option>';
  for(const l of Object.keys(S.meta.lineage_height)){
    const o=document.createElement('option');o.value=l;o.textContent=l+' ('+S.meta.lineage_height[l]+'mm)';an.appendChild(o);}
  an.onchange=()=>{renderSel();refreshCounts();};
  $('#fSub').onchange=()=>{
    Object.keys(S.open).forEach(id=>{if(S.open[id])loadSlot(id);});
    refreshCounts(); if(charsVisible())loadCharacters();
  };
  $('#fGroup').onchange=()=>{
    fillFactions(); fillSubs();
    Object.keys(S.open).forEach(id=>{if(S.open[id])loadSlot(id);});
    refreshCounts(); if(charsVisible())loadCharacters();
  };
  $('#fFaction').onchange=$('#fLin').onchange=()=>{
    fillSubs();
    Object.keys(S.open).forEach(id=>{if(S.open[id])loadSlot(id);});
    refreshCounts();
    if(charsVisible())loadCharacters();
  };
  renderSlots(); renderSel(); loadBundles(); refreshCounts();
}

// Slot headers reflect the CURRENT filters, so narrowing by faction or
// lineage immediately shows how many parts are really left in each slot.
async function refreshCounts(){
  const u=`/api/counts?faction=${$('#fFaction').value}&lineage=${$('#fLin').value}`
    +`&group=${encodeURIComponent($('#fGroup').value)}&anchor=${anchorLineage()}`
    +`&subfaction=${encodeURIComponent($('#fSub').value)}`;
  const r=await api(u);
  S.available={lineages:r.lineages, lineages_all:r.lineages_all};
  fillLineages($('#tabChars').classList.contains('on')?'chars':'parts');
  for(const s of S.meta.slots){
    const el=document.querySelector(`.card[data-id="${s.id}"] .count`);
    if(el){const n=r.counts[s.id]||0; el.textContent=`${n} available`;
      el.style.color = n?'' : '#7f5a5a';}
  }
}

function renderSlots(){
  const host=$('#slots'); host.innerHTML='';
  for(const s of S.meta.slots){
    S.sel[s.id]=S.sel[s.id]||[];
    const c=document.createElement('div'); c.className='card'; c.dataset.id=s.id;
    c.innerHTML=`<div class="slot-head"><span class="slot-title">${s.label}</span>
      <span class="count">${S.meta.counts[s.id]||0} available</span>
      <span style="flex:1"></span><span class="chips"></span></div>
      <div class="body"><div class="tools">
        <input placeholder="search ${s.label.toLowerCase()}...">
        <button class="more">Load more</button></div>
      <div class="grid"></div></div>`;
    c.querySelector('.slot-head').onclick=e=>{
      if(e.target.tagName==='B')return;
      const b=c.querySelector('.body'); const open=!b.classList.contains('open');
      b.classList.toggle('open',open); S.open[s.id]=open; if(open)loadSlot(s.id);};
    const inp=c.querySelector('input'); let t;
    inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>loadSlot(s.id),220);};
    c.querySelector('.more').onclick=()=>loadSlot(s.id,true);
    host.appendChild(c);
  }
  renderChips();
}

async function loadSlot(id,append){
  const s=S.meta.slots.find(x=>x.id===id);
  const card=document.querySelector(`.card[data-id="${id}"]`);
  const grid=card.querySelector('.grid'), q=card.querySelector('input').value.trim();
  const off=append?grid.children.length:0;
  // Directional bulk rule: heads and pauldrons may only come from armour of
  // equal or lighter build than the anchor, never heavier.
  const pol=S.meta.slot_bulk_policy[s.slot];
  const B=S.meta.lineage_bulk;
  let bulkParam='';
  if(pol==='down_only'){
    const b=B[anchorLineage()];
    if(b!==undefined)bulkParam=`&max_bulk=${b}`;
  }
  const u=`/api/parts?slot=${s.slot}&limit=120&offset=${off}&anchor=${anchorLineage()}`
    +(s.side?`&side=${s.side}`:'')+(q?`&q=${encodeURIComponent(q)}`:'')
    +`&faction=${$('#fFaction').value}&group=${encodeURIComponent($('#fGroup').value)}`
    +`&subfaction=${encodeURIComponent($('#fSub').value)}`
    +`&lineage=${$('#fLin').value}${bulkParam}`;
  const r=await api(u);
  if(!append)grid.innerHTML='';
  if(bulkParam&&!append){
    const note=document.createElement('div');
    note.className='count';
    note.style.cssText='grid-column:1/-1;margin-bottom:2px';
    note.textContent=`Showing ${s.label.toLowerCase()} from ${anchorLineage()}`
      +` or lighter armour — heavier ones would look oversized.`;
    grid.appendChild(note);
  }
  if(!r.parts.length&&!append)grid.innerHTML='<div class="count">No parts match.</div>';
  for(const p of r.parts){S.cache[p.id]=p;grid.appendChild(tile(p,id));}
}

function tile(p,slotId){
  const d=document.createElement('div'); d.className='tile';
  if(S.sel[slotId].some(x=>x.id===p.id))d.classList.add('sel');
  d.innerHTML=`<img loading="lazy" src="${p.has_thumb?'/api/thumb/'+p.id:''}">
    <div class="n">${p.name}</div>
    <div class="d">${p.dims[0].toFixed(0)}×${p.dims[1].toFixed(0)}×${p.dims[2].toFixed(0)}mm</div>`;
  d.onclick=e=>{if(e.shiftKey){view(p);return;} toggle(slotId,p); d.classList.toggle('sel');};
  d.oncontextmenu=e=>{e.preventDefault();view(p);};
  return d;
}

function toggle(slotId,p){
  const a=S.sel[slotId], i=a.findIndex(x=>x.id===p.id);
  if(i>=0)a.splice(i,1); else a.push(p);
  renderChips(); renderSel();
}

function renderChips(){
  for(const s of S.meta.slots){
    const el=document.querySelector(`.card[data-id="${s.id}"] .chips`); if(!el)continue;
    el.innerHTML=S.sel[s.id].map(p=>
      `<span class="chip"><img src="/api/thumb/${p.id}">${p.name.slice(0,16)}
       <b data-s="${s.id}" data-i="${p.id}">×</b></span>`).join('');
    el.querySelectorAll('b').forEach(b=>b.onclick=ev=>{
      ev.stopPropagation();
      const a=S.sel[b.dataset.s]; const i=a.findIndex(x=>x.id==b.dataset.i);
      if(i>=0)a.splice(i,1); renderChips(); renderSel();
      if(S.open[b.dataset.s])loadSlot(b.dataset.s);});
  }
}

async function renderSel(){
  const anchor=anchorLineage();
  const note=$('#anchorNote');
  const hasTorso=(S.sel['torso']||[]).length>0;
  note.textContent=$('#fAnchor').value==='auto'
    ? (hasTorso?`Scaled to torso lineage: ${anchor}`
       :`No torso picked — falling back to ${anchor}`)
    : `Forced to ${anchor}`;

  // Tiles carry sizes computed for the anchor they were fetched under, so when
  // the anchor moves (you picked a torso, or forced a build scale) any open
  // slot must be refetched or it would keep showing the old millimetres.
  if(S.lastAnchor!==anchor){
    S.lastAnchor=anchor;
    Object.keys(S.open).forEach(id=>{if(S.open[id])loadSlot(id);});
  }

  // Ask the server for scales at the current anchor. Selected parts may have
  // been fetched under a different anchor, so their cached values are stale.
  const ids=[]; for(const s of S.meta.slots)for(const p of S.sel[s.id])ids.push(p.id);
  if(ids.length){
    const r=await post('/api/scales',{anchor,ids});
    S.scales=r.scales||{};
  }else S.scales={};

  const host=$('#selList'); host.innerHTML=''; let n=0; const lins=new Set();
  let estimated=0, overridden=0;
  for(const s of S.meta.slots)for(const p of S.sel[s.id]){
    n++; lins.add(p.lineage);
    const info=S.scales[p.id]||{scale:p.scale,per_part:p.per_part,dims:p.dims};
    const ov=S.overrides[p.id];
    const sc=(ov!==undefined)?ov:info.scale;
    if(ov!==undefined)overridden++;
    if(info.per_part)estimated++;
    const mx=Math.max(...p.raw_dims)*sc;
    const row=document.createElement('div'); row.className='sel-row';
    row.innerHTML=`<img src="/api/thumb/${p.id}"><div class="m">
      <div title="${p.name}">${p.name}${info.per_part?' <span style="color:#ffd98a">*</span>':''}</div>
      <div>${s.label} · ${p.lineage} · ${mx.toFixed(1)}mm${ov!==undefined?' · edited':''}</div></div>
      <input type="number" step="0.01" min="0.0001" value="${sc.toFixed(3)}" data-i="${p.id}"
        title="${ov!==undefined?'Your value. Clear the box to go back to automatic.':'Automatic'}"
        style="${ov!==undefined?'border-color:#8a6d3b':''}">`;
    host.appendChild(row);
  }
  // Typed values are remembered in S.overrides, so adding another part later
  // re-renders this list without throwing your edits away.
  host.querySelectorAll('input').forEach(inp=>{
    inp.onchange=()=>{
      const id=inp.dataset.i, v=parseFloat(inp.value);
      if(inp.value.trim()===''||!isFinite(v)||v<=0)delete S.overrides[id];
      else S.overrides[id]=v;
      renderSel();
    };
  });
  if(!n)host.innerHTML='<div class="count">Nothing selected yet. Every slot is optional.</div>';
  const w=$('#msg'); w.innerHTML='';
  // Warn on BULK difference, not merely on lineage names differing: Gravis
  // next to Terminator is a sensible kitbash, Firstborn next to Terminator
  // is not, even though both are "two different lineages".
  if(lins.size>1){
    const B=S.meta.lineage_bulk,[MILD,STRONG]=S.meta.bulk_thresholds;
    const vals=[...lins].map(l=>B[l]!==undefined?B[l]:1.0);
    const spread=Math.max(...vals)-Math.min(...vals);
    if(spread>=STRONG)
      w.innerHTML+=`<div class="warn"><b>Heavy bulk mismatch</b> —
        ${[...lins].join(' + ')}. These armour patterns differ a lot in mass,
        so parts will look wrong together even scaled to ${anchor}.</div>`;
    else if(spread>=MILD)
      w.innerHTML+=`<div class="warn">Mixing ${[...lins].join(' + ')} —
        mild bulk difference. Usually fine, worth a look in the 3D view.</div>`;
  }
  if(estimated)w.innerHTML+=`<div class="warn">${estimated} part(s) marked
    <b>*</b>: their pack's scale looked wrong for this kind of part, so the size
    was estimated individually. Worth eyeballing in the 3D view.</div>`;
  if(overridden)w.innerHTML+=`<div class="ok">${overridden} part(s) using your
    own scale. Clear a box to return it to automatic.</div>`;
}

$('#btnClearSel').onclick=()=>{for(const k in S.sel)S.sel[k]=[];S.overrides={};
  renderChips();renderSel();
  Object.keys(S.open).forEach(id=>{if(S.open[id])loadSlot(id);});};

$('#btnBundle').onclick=async()=>{
  const name=$('#bname').value.trim();
  // Built from the selection itself, not from whatever happens to be rendered,
  // and only genuine overrides are sent — everything else the server computes.
  const items=[];
  for(const s of S.meta.slots)for(const p of S.sel[s.id]){
    const it={id:p.id};
    if(S.overrides[p.id]!==undefined)it.scale=S.overrides[p.id];
    items.push(it);
  }
  if(!items.length){$('#msg').innerHTML='<div class="warn">Nothing selected.</div>';return;}
  if(!name){$('#msg').innerHTML='<div class="warn">Give the bundle a name.</div>';return;}
  $('#btnBundle').disabled=true; $('#btnBundle').textContent='Writing...';
  const r=await post('/api/bundle',{name,items,anchor_lineage:anchorLineage()});
  $('#btnBundle').disabled=false; $('#btnBundle').textContent='Create Bundle';
  $('#msg').innerHTML=r.error?`<div class="warn">${r.error}</div>`
    :`<div class="ok">Wrote ${r.written} parts to ${r.dir}${r.failed&&r.failed.length?
      '<br>Failed: '+r.failed.join(', '):''}</div>`;
  loadBundles();
};

$('#btnClearOut').onclick=async()=>{
  if(!confirm('Delete everything in the Bundles folder?\n\nThis cannot be undone. '
    +'A log of removed files is saved.'))return;
  const r=await post('/api/clear',{confirm:'CLEAR'});
  $('#msg').innerHTML=`<div class="ok">Removed ${r.removed} files. Log: ${r.log||'-'}</div>`;
  loadBundles();
};

async function loadBundles(){
  const r=await api('/api/bundles');
  const cb=$('#charBundles'); if(cb)cb.innerHTML='Output: '+r.dir;
  $('#bundleList').innerHTML='Output: '+r.dir+'<br>'+
    (r.bundles.length?r.bundles.map(b=>`• ${b.name} (${b.files} files)`).join('<br>')
     :'(empty)');
}

$('#btnReview').onclick=async()=>{ S.revOffset=0; loadReview(); };

// The review queue is where unclassified parts land. It needs the same
// affordances as the builder: a picture, what faction it came from, and a way
// to open it in 3D — you cannot name a part you cannot see.
async function loadReview(){
  const lim=60, off=S.revOffset||0;
  const r=await api(`/api/parts?unclassified=1&limit=${lim}&offset=${off}`);
  const opts=S.meta.slots.map(s=>s.slot).filter((v,i,a)=>a.indexOf(v)===i);
  let h='<div class="card" style="padding:12px">'
    +'<h3>Review queue — assign a slot</h3>'
    +`<div class="count" style="margin-bottom:8px">${r.total} unclassified · showing `
    +`${off+1}-${Math.min(off+lim,r.total)}. Click a tile to inspect it in 3D. `
    +'Choose <b>full character</b> for a finished model — it leaves the parts '
    +'builder and appears under Characters.</div><div class="grid">';
  for(const p of r.parts){
    S.cache[p.id]=p;
    const fac=[p.faction,p.subfaction].filter(Boolean).join(' · ')||'unknown faction';
    h+=`<div class="tile"><img loading="lazy" data-v="${p.id}"
          src="${p.has_thumb?'/api/thumb/'+p.id:''}">
      <div class="n">${p.name}</div>
      <div class="d">${fac}<br>${p.lineage||'—'} ·
        ${p.dims[0].toFixed(0)}×${p.dims[1].toFixed(0)}×${p.dims[2].toFixed(0)}mm</div>
      <select data-i="${p.id}"><option value="">unassigned</option>
        <option value="whole">— full character —</option>`
      +opts.map(o=>`<option value="${o}">${o}</option>`).join('')+`</select></div>`;
  }
  h+='</div><div style="margin-top:10px;display:flex;gap:8px">'
    +(off>0?'<button id="revPrev">← previous</button>':'')
    +(off+lim<r.total?'<button id="revNext">next →</button>':'')
    +'<button id="revBack">Back to builder</button></div></div>';
  $('#slots').innerHTML=h;
  // clicking the picture opens the same 3D viewer the builder uses
  document.querySelectorAll('#slots img[data-v]').forEach(img=>{
    img.style.cursor='zoom-in';
    img.onclick=()=>{const p=S.cache[+img.dataset.v]; if(p)view(p);};
  });
  document.querySelectorAll('#slots select').forEach(sel=>sel.onchange=async()=>{
    await post('/api/tag',{id:+sel.dataset.i,slot:sel.value||null});
    sel.style.borderColor='#48b07a';
    if(sel.value==='whole'){
      const t=sel.closest('.tile'); if(t)t.style.opacity=.45;
    }
  });
  const prev=$('#revPrev'), next=$('#revNext');
  if(prev)prev.onclick=()=>{S.revOffset=Math.max(0,off-lim);loadReview();};
  if(next)next.onclick=()=>{S.revOffset=off+lim;loadReview();};
  $('#revBack').onclick=()=>{renderSlots();};
}

/* ---------------------------- characters view ----------------------------- */
// A character is either one complete STL or a folder whose parts add up to a
// figure. Filters still apply; vehicles and dreadnoughts ARE offered here,
// since picking a whole vehicle is legitimate even though its loose parts
// aren't useful in the parts builder.
// Whether the Characters view is on screen. This replaced a flag that was
// read in three places and never once assigned, so changing a filter silently
// skipped the reload and the dropdown kept whatever the last search left in
// it — typing was the only thing that refreshed it.
function charsVisible(){
  const v=$('#charView');
  return !!v && v.style.display!=='none';
}

function switchView(which){
  const parts = which==='parts';
  $('#partsView').style.display = parts?'flex':'none';
  $('#charView').style.display  = parts?'none':'flex';
  $('#tabParts').classList.toggle('on',parts);
  $('#tabChars').classList.toggle('on',!parts);
  fillFactions(); fillSubs();
  fillLineages(parts?'parts':'chars');
  if(!parts){ loadCharacters(); }
}
$('#tabParts').onclick=()=>switchView('parts');
$('#tabChars').onclick=()=>switchView('chars');

async function loadCharacters(){
  const q=$('#charSearch').value.trim();
  // The lineage dropdown hides dreadnoughts/primarchs because the PARTS builder
  // excludes them. The Characters view is where they live, so a hidden lineage
  // selected there must not filter everything away.
  const lin=$('#fLin').value;
  const u=`/api/characters?faction=${$('#fFaction').value}`
    +`&group=${encodeURIComponent($('#fGroup').value)}`
    +`&lineage=${lin}`+(q?`&q=${encodeURIComponent(q)}`:'')
    +`&subfaction=${encodeURIComponent($('#fSub').value)}`;
  const r=await api(u);
  const sel=$('#charPick');
  const was=sel.value;
  sel.innerHTML=r.characters.map(c=>
    `<option value="${c.id}">${c.label} — ${c.files} piece${c.files==1?'':'s'} (${c.kind})</option>`
  ).join('');
  const shown=r.characters.length===r.total ? `${r.total}`
            : `${r.characters.length} of ${r.total}`;
  $('#charMeta').textContent=`${shown} characters match`;
  S.chars=r.characters;
  if(r.characters.length){
    // stay on the same character when it survives the new filter
    const keep=r.characters.some(c=>c.id===was)?was:r.characters[0].id;
    sel.value=keep; loadCharacter(keep);
  } else {
    $('#charFiles').innerHTML='<div class="count">Nothing matches these filters.</div>';
    $('#charTitle').textContent='Pieces'; S.charFiles=[]; S.charMeta=null;
    $('#charNote').innerHTML='';
  }
}
let charTimer;
$('#charSearch').oninput=()=>{clearTimeout(charTimer);charTimer=setTimeout(loadCharacters,220);};
$('#charPick').onchange=()=>loadCharacter($('#charPick').value);

async function loadCharacter(id){
  const meta=(S.chars||[]).find(c=>c.id===id)||{};
  const r=await api(`/api/character?id=${encodeURIComponent(id)}`);
  S.charFiles=r.files; S.charMeta=meta;
  $('#charTitle').textContent=`${meta.label||'Pieces'} — ${r.files.length} file${r.files.length==1?'':'s'}`;
  const g=$('#charFiles'); g.innerHTML='';
  for(const p of r.files){
    S.cache[p.id]=p;
    const d=document.createElement('div'); d.className='tile';
    d.innerHTML=`<img loading="lazy" src="${p.has_thumb?'/api/thumb/'+p.id:''}">
      <div class="n">${p.name}</div>
      <div class="d">${p.slot||'—'}${p.is_whole?' · complete':''}<br>
        ${p.dims[0].toFixed(0)}×${p.dims[1].toFixed(0)}×${p.dims[2].toFixed(0)}mm</div>`;
    d.onclick=e=>{if(e.shiftKey){view(p);return;} view(p);};
    d.oncontextmenu=e=>{e.preventDefault();view(p);};
    g.appendChild(d);
  }
  const hid=r.variants_hidden?`<br><span class="warn">${r.variants_hidden} duplicate
    variant(s) hidden</span> — supported and pre-sliced copies of the same part.
    Raw meshes are kept, since scaling a supported mesh scales its supports too.`:'';
  $('#charNote').innerHTML=`Exports all ${r.files.length} piece(s) at
    <b>${meta.lineage||'native'}</b> scale.<br>Click any piece to inspect it in 3D.${hid}`;
  if(!$('#charName').value.trim()) $('#charName').value=meta.label||'';
}

$('#btnCharExport').onclick=async()=>{
  const name=$('#charName').value.trim();
  if(!S.charFiles||!S.charFiles.length){
    $('#charMsg').innerHTML='<div class="warn">Nothing selected.</div>';return;}
  if(!name){$('#charMsg').innerHTML='<div class="warn">Give the bundle a name.</div>';return;}
  const b=$('#btnCharExport'); b.disabled=true; b.textContent='Writing...';
  // Anchor to the character's own lineage so it keeps its native proportions.
  const r=await post('/api/bundle',{name,items:S.charFiles.map(f=>({id:f.id})),
    anchor_lineage:(S.charMeta&&S.charMeta.lineage)||'firstborn'});
  b.disabled=false; b.textContent='Export all pieces';
  $('#charMsg').innerHTML=r.error?`<div class="warn">${r.error}</div>`
    :`<div class="ok">Wrote ${r.written} file(s) to ${r.dir}</div>`;
  loadBundles();
};
$('#btnCharClear').onclick=()=>$('#btnClearOut').click();

/* ---------------- minimal WebGL viewer (no external libraries) ------------- */
const VIEW={cleanup:null};
async function view(p){
  $('#viewer').style.display='flex'; $('#vName').textContent=p.name;
  const sc=scaleFor(p), d=p.raw_dims.map(x=>x*sc);
  $('#vMeta').innerHTML=`<span>${p.pack}</span><span>${p.lineage}</span>
    ${p.role?`<span>${p.role}</span>`:''}
    <span>${d[0].toFixed(1)} × ${d[1].toFixed(1)} × ${d[2].toFixed(1)} mm</span>
    <span>${p.tris.toLocaleString()} triangles</span>
    <span>scale ×${sc.toFixed(3)}</span>`;
  if(!p.has_mesh){$('#vMeta').innerHTML+=' <span>(no preview mesh)</span>';return;}
  const buf=await fetch('/api/mesh/'+p.id).then(r=>r.arrayBuffer());
  drawMesh(parseKBM(buf));
}
function parseKBM(buf){
  const dv=new DataView(buf);
  if(String.fromCharCode(dv.getUint8(0),dv.getUint8(1),dv.getUint8(2),dv.getUint8(3))!=='KBM1')
    return null;
  const n=dv.getUint32(4,true);
  const lo=[dv.getFloat32(8,true),dv.getFloat32(12,true),dv.getFloat32(16,true)];
  const ext=[dv.getFloat32(20,true),dv.getFloat32(24,true),dv.getFloat32(28,true)];
  const q=new Int16Array(buf,32,n*9);
  const v=new Float32Array(n*9);
  for(let i=0;i<n*9;i+=3){
    v[i]  =lo[0]+q[i]  /32767*ext[0];
    v[i+1]=lo[1]+q[i+1]/32767*ext[1];
    v[i+2]=lo[2]+q[i+2]/32767*ext[2];
  }
  return v;
}
function drawMesh(verts){
  const cv=$('#cv'); if(!verts)return;
  cv.width=cv.clientWidth*devicePixelRatio; cv.height=cv.clientHeight*devicePixelRatio;
  const gl=cv.getContext('webgl'); if(!gl)return;
  // centre + normalise
  let mn=[1e9,1e9,1e9],mx=[-1e9,-1e9,-1e9];
  for(let i=0;i<verts.length;i+=3)for(let k=0;k<3;k++){
    mn[k]=Math.min(mn[k],verts[i+k]);mx[k]=Math.max(mx[k],verts[i+k]);}
  const c=[0,1,2].map(k=>(mn[k]+mx[k])/2), r=Math.max(mx[0]-mn[0],mx[1]-mn[1],mx[2]-mn[2])||1;
  const pos=new Float32Array(verts.length), nrm=new Float32Array(verts.length);
  for(let i=0;i<verts.length;i+=3)for(let k=0;k<3;k++)pos[i+k]=(verts[i+k]-c[k])/r*2;
  for(let t=0;t<pos.length;t+=9){
    const ax=pos[t+3]-pos[t],ay=pos[t+4]-pos[t+1],az=pos[t+5]-pos[t+2];
    const bx=pos[t+6]-pos[t],by=pos[t+7]-pos[t+1],bz=pos[t+8]-pos[t+2];
    let nx=ay*bz-az*by,ny=az*bx-ax*bz,nz=ax*by-ay*bx;
    const L=Math.hypot(nx,ny,nz)||1; nx/=L;ny/=L;nz/=L;
    for(let k=0;k<3;k++){nrm[t+k*3]=nx;nrm[t+k*3+1]=ny;nrm[t+k*3+2]=nz;}
  }
  const vs=`attribute vec3 p;attribute vec3 n;uniform mat4 M;varying vec3 N;
    void main(){N=mat3(M)*n;gl_Position=vec4((M*vec4(p,1.0)).xyz*vec3(1.0,1.0,-0.35)
      +vec3(0.0,0.0,0.5),1.0);}`;
  const fs=`precision mediump float;varying vec3 N;
    void main(){vec3 l=normalize(vec3(0.4,0.7,0.6));
      float d=max(dot(normalize(N),l),0.0);
      vec3 col=vec3(0.16)+vec3(0.78,0.80,0.86)*d;gl_FragColor=vec4(col,1.0);}`;
  const sh=(t,s)=>{const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);return o;};
  const pr=gl.createProgram();
  gl.attachShader(pr,sh(gl.VERTEX_SHADER,vs));gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,fs));
  gl.linkProgram(pr);gl.useProgram(pr);
  const bind=(data,name)=>{const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);
    gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);
    const a=gl.getAttribLocation(pr,name);gl.enableVertexAttribArray(a);
    gl.vertexAttribPointer(a,3,gl.FLOAT,false,0,0);};
  bind(pos,'p');bind(nrm,'n');
  gl.enable(gl.DEPTH_TEST);gl.viewport(0,0,cv.width,cv.height);
  let rx=-1.1,ry=0.6,zoom=1,drag=false,lx=0,ly=0;
  const M=gl.getUniformLocation(pr,'M');
  function draw(){
    const cx=Math.cos(rx),sx=Math.sin(rx),cy=Math.cos(ry),sy=Math.sin(ry),z=zoom;
    // Z-up models: rotate about X then about the vertical axis
    const m=[ cy*z,sy*sx*z,-sy*cx*z,0,  0,cx*z,sx*z,0,  sy*z,-cy*sx*z,cy*cx*z,0, 0,0,0,1];
    gl.uniformMatrix4fv(M,false,new Float32Array(m));
    gl.clearColor(0.055,0.063,0.078,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES,0,pos.length/3);
  }
  // Listeners are torn down when the viewer closes. Assigning window.onmousemove
  // each time would leak a closure per part opened and clobber anything else
  // that wanted the same global handler.
  if(VIEW.cleanup)VIEW.cleanup();
  const onDown=e=>{drag=true;lx=e.clientX;ly=e.clientY;cv.style.cursor='grabbing';};
  const onUp=()=>{drag=false;cv.style.cursor='grab';};
  const onMove=e=>{if(!drag)return;ry+=(e.clientX-lx)*0.01;rx+=(e.clientY-ly)*0.01;
    lx=e.clientX;ly=e.clientY;draw();};
  const onWheel=e=>{e.preventDefault();zoom*=e.deltaY>0?0.92:1.08;draw();};
  cv.addEventListener('mousedown',onDown);
  window.addEventListener('mouseup',onUp);
  window.addEventListener('mousemove',onMove);
  cv.addEventListener('wheel',onWheel,{passive:false});
  VIEW.cleanup=()=>{
    cv.removeEventListener('mousedown',onDown);
    window.removeEventListener('mouseup',onUp);
    window.removeEventListener('mousemove',onMove);
    cv.removeEventListener('wheel',onWheel);
    VIEW.cleanup=null;
  };
  draw();
}
function closeViewer(){if(VIEW.cleanup)VIEW.cleanup();$('#viewer').style.display='none';}
$('#vClose').onclick=closeViewer;
$('#viewer').onclick=e=>{if(e.target.id==='viewer')closeViewer();};
boot();
</script></body></html>"""


def cmd_serve(argv):
    if not os.path.exists(DB_PATH):
        sys.exit(f"No library database at {DB_PATH}\n"
                 f"Run:  python3 \"{os.path.abspath(__file__)}\" index "
                 "\"/path/to/your/stls\"")
    os.makedirs(BUNDLE_DIR, exist_ok=True)
    Handler.conn = db()
    n = Handler.conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Kitbash Builder — {n} parts indexed")
    print(f"  {url}")
    print(f"  bundles -> {BUNDLE_DIR}")
    print("  Ctrl-C to stop")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# Commands that need to know where the library is before they can do anything.
NEEDS_LIBRARY = {"classify", "serve", "import-review", "make-sheet",
                 "content-check"}


def main():
    cmds = {"index": cmd_index, "classify": cmd_classify, "stats": cmd_stats,
            "serve": cmd_serve, "doctor": cmd_doctor,
            "import-review": cmd_import_review, "make-sheet": cmd_make_sheet,
            "rules": cmd_rules, "content-check": cmd_content_check,
            "dropdowns": cmd_dropdowns}
    args = configure(sys.argv[1:])
    if not args or args[0] not in cmds:
        print(__doc__)
        print("Commands: " + ", ".join(cmds))
        return
    if args[0] in NEEDS_LIBRARY:
        require_library()
    cmds[args[0]](args[1:])


if __name__ == "__main__":
    main()
