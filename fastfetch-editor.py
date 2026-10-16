#!/usr/bin/env python3
"""
fastfetch-editor: a small GUI that adds, edits and deletes ASCII logos in the
fastfetch source tree, and previews, edits and installs fastfetch presets.

"Add logo" tab: fill in the boxes and the tool will
  1. copy the logo text into   src/logo/ascii/<letter>/<name>.txt
  2. add a matching entry to   src/logo/ascii/<letter>.inc

The letter is always chosen manually. Existing entries are never touched, except
the one with the same name if you confirm replacing it.

"Edit logo" tab: pick a letter, pick one of the logos of that letter, then
  - change its names, colors, flags and logo text and save, or
  - delete it (the entry in <letter>.inc and its .txt file).
Entries that contain something the editor cannot reproduce exactly (for example
comments inside the entry) are never rewritten: their details are read-only,
only the logo text can be edited, and they can still be deleted.

"Presets" tab: pick one of the presets in <project>/presets, edit its JSONC text and see
what fastfetch prints with it in the preview on the right (this needs a built fastfetch:
<project>/build/fastfetch, or one found in PATH). Three buttons at the bottom install the
preset text as config.jsonc in ~/.config/fastfetch (current user), in
/etc/skel/.config/fastfetch (users created later) or in /root/.config/fastfetch (root).
The last two ask for the administrator password through pkexec.

Usage:
    python3 fastfetch-editor-v3.py [path/to/fastfetch-2.69.0]

Requires PyQt6 (pip install PyQt6, or your distribution's python-pyqt6 package).
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

LOGO_REL = Path("src") / "logo" / "ascii"
MAX_NAMES = 9    # FASTFETCH_LOGO_MAX_NAMES
MAX_COLORS = 9   # FASTFETCH_LOGO_MAX_COLORS ($1 ... $9)
LETTERS = [chr(c) for c in range(ord("a"), ord("z") + 1)]
STEM_RE = re.compile(r"^[a-z0-9_]+$")

# One "#ifdef FASTFETCH_DATATEXT_LOGO_X ... #endif" entry of a .inc file
BLOCK_RE = re.compile(
    r"^    #ifdef FASTFETCH_DATATEXT_LOGO_([A-Z0-9_]+)[ \t]*\n.*?^    #endif[ \t]*\n",
    re.MULTILINE | re.DOTALL,
)
LAST_RE = re.compile(r"^    // LAST[ \t]*$", re.MULTILINE)
NAMES_RE = re.compile(r"\.names\s*=\s*\{([^}]*)\}")
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

# (label, C constant, SGR code used for the preview)
NAMED_COLORS = [
    ("Default", "FF_COLOR_FG_DEFAULT", "39"),
    ("Black", "FF_COLOR_FG_BLACK", "30"),
    ("Red", "FF_COLOR_FG_RED", "31"),
    ("Green", "FF_COLOR_FG_GREEN", "32"),
    ("Yellow", "FF_COLOR_FG_YELLOW", "33"),
    ("Blue", "FF_COLOR_FG_BLUE", "34"),
    ("Magenta", "FF_COLOR_FG_MAGENTA", "35"),
    ("Cyan", "FF_COLOR_FG_CYAN", "36"),
    ("White", "FF_COLOR_FG_WHITE", "37"),
    ("Light black", "FF_COLOR_FG_LIGHT_BLACK", "90"),
    ("Light red", "FF_COLOR_FG_LIGHT_RED", "91"),
    ("Light green", "FF_COLOR_FG_LIGHT_GREEN", "92"),
    ("Light yellow", "FF_COLOR_FG_LIGHT_YELLOW", "93"),
    ("Light blue", "FF_COLOR_FG_LIGHT_BLUE", "94"),
    ("Light magenta", "FF_COLOR_FG_LIGHT_MAGENTA", "95"),
    ("Light cyan", "FF_COLOR_FG_LIGHT_CYAN", "96"),
    ("Light white", "FF_COLOR_FG_LIGHT_WHITE", "97"),
]
_CONST_TO_SGR = {const: sgr for _, const, sgr in NAMED_COLORS}

# Tango-like palette, indexed by the first 16 xterm colors
_SGR_HEX = {
    "39": "#d0d0d0", "30": "#555753", "31": "#cc0000", "32": "#4e9a06", "33": "#c4a000",
    "34": "#3465a4", "35": "#75507b", "36": "#06989a", "37": "#d3d7cf",
    "90": "#888a85", "91": "#ef2929", "92": "#8ae234", "93": "#fce94f",
    "94": "#729fcf", "95": "#ad7fa8", "96": "#34e2e2", "97": "#eeeeec",
}
_XTERM16 = ["30", "31", "32", "33", "34", "35", "36", "37",
            "90", "91", "92", "93", "94", "95", "96", "97"]

DEFAULT_HEX = _SGR_HEX["39"]


# ----------------------------------------------------------------------------
# Core logic (no GUI)
# ----------------------------------------------------------------------------

@dataclass
class ColorSpec:
    """A logo color: a named FF_COLOR_FG_* constant, a 256-color index or an RGB triple."""

    kind: str   # "named", "256" or "rgb"
    value: str  # constant name, "36" or "127;63;191"

    def normalized(self) -> "ColorSpec":
        value = self.value.strip()
        if self.kind == "rgb":
            value = re.sub(r"\s*[;,]\s*", ";", value)
        return ColorSpec(self.kind, value)

    def error(self) -> Optional[str]:
        spec = self.normalized()
        if spec.kind == "named":
            return None if spec.value in _CONST_TO_SGR else f"Unknown color constant: {spec.value}"
        if spec.kind == "256":
            if spec.value.isdigit() and 0 <= int(spec.value) <= 255:
                return None
            return f"256-color value must be 0-255 (got '{self.value}')"
        if spec.kind == "rgb":
            parts = spec.value.split(";")
            if len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return None
            return f"RGB value must look like r;g;b with 0-255 each (got '{self.value}')"
        return f"Unknown color kind: {self.kind}"

    def to_c(self) -> str:
        spec = self.normalized()
        if spec.kind == "named":
            return spec.value
        if spec.kind == "256":
            return f'FF_COLOR_FG_256 "{spec.value}"'
        return f'FF_COLOR_FG_RGB "{spec.value}"'

    def to_hex(self) -> str:
        if self.error():
            return DEFAULT_HEX
        spec = self.normalized()
        if spec.kind == "named":
            return _SGR_HEX[_CONST_TO_SGR[spec.value]]
        if spec.kind == "rgb":
            r, g, b = (int(p) for p in spec.value.split(";"))
            return f"#{r:02x}{g:02x}{b:02x}"
        return xterm256_hex(int(spec.value))


def xterm256_hex(index: int) -> str:
    if index < 16:
        return _SGR_HEX[_XTERM16[index]]
    if index < 232:
        index -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return "#{:02x}{:02x}{:02x}".format(
            levels[index // 36], levels[(index // 6) % 6], levels[index % 6])
    gray = 8 + (index - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


@dataclass
class LogoSpec:
    letter: str = ""
    stem: str = ""                 # file name without extension, e.g. "satellaos"
    display: str = ""              # text of the "// comment" above the entry
    names: list = field(default_factory=list)
    colors: list = field(default_factory=list)       # list[ColorSpec]
    color_keys: Optional[ColorSpec] = None
    color_title: Optional[ColorSpec] = None
    small: bool = False
    alter: bool = False
    art: str = ""
    # Order of the fields inside an existing entry (kept when it is edited, so the
    # diff only shows what really changed). Empty for new entries.
    order: list = field(default_factory=list)

    @property
    def macro(self) -> str:
        return self.stem.upper()


def c_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(spec: LogoSpec) -> str:
    """Return the text of one .inc entry (including the #ifdef/#endif lines)."""
    comment = (spec.display or (spec.names[0] if spec.names else spec.stem))
    # A backslash (or the trigraph ??/) at the end of a // comment would continue the
    # comment on the next line and swallow the "{", so they are dropped from the comment.
    comment = " ".join(comment.replace("??/", "").replace("\\", "").split())
    parts: dict[str, list[str]] = {
        "names": ["        .names = { " + ", ".join(c_string(n) for n in spec.names) + " },"],
        "lines": [f"        .lines = FASTFETCH_DATATEXT_LOGO_{spec.macro},"],
        "type": [],
        "colors": ["        .colors = {"]
                  + [f"            {color.to_c()}," for color in spec.colors]
                  + ["        },"],
        "colorKeys": [],
        "colorTitle": [],
    }
    type_bits = []
    if spec.small:
        type_bits.append("FF_LOGO_LINE_TYPE_SMALL_BIT")
    if spec.alter:
        type_bits.append("FF_LOGO_LINE_TYPE_ALTER_BIT")
    if type_bits:
        parts["type"] = ["        .type = " + " | ".join(type_bits) + ","]
    if spec.color_keys is not None:
        parts["colorKeys"] = [f"        .colorKeys = {spec.color_keys.to_c()},"]
    if spec.color_title is not None:
        parts["colorTitle"] = [f"        .colorTitle = {spec.color_title.to_c()},"]

    # Fields of an existing entry keep their order; anything else follows the default order
    sequence = [f for f in spec.order if f in parts] + [f for f in parts if f not in spec.order]
    lines = [
        f"    #ifdef FASTFETCH_DATATEXT_LOGO_{spec.macro}",
        f"    // {comment}",
        "    {",
    ]
    for name in sequence:
        lines.extend(parts[name])
    lines.append("    },")
    lines.append("    #endif")
    return "\n".join(lines) + "\n"


def insert_or_replace_entry(inc_text: str, spec: LogoSpec) -> tuple[str, bool]:
    """
    Add the entry for `spec` to the text of a .inc file.
    An entry with the same macro is replaced; otherwise the new one is placed in
    alphabetical order (by macro name), or just before the "// LAST" terminator.
    Returns (new_text, replaced).
    """
    entry = render_entry(spec)
    blocks = list(BLOCK_RE.finditer(inc_text))

    for block in blocks:
        if block.group(1) == spec.macro:
            return inc_text[:block.start()] + entry + inc_text[block.end():], True

    for block in blocks:
        if block.group(1) > spec.macro:
            return inc_text[:block.start()] + entry + inc_text[block.start():], False

    last = LAST_RE.search(inc_text)
    if last is None:
        raise ValueError("Could not find the '// LAST' terminator in the .inc file.")
    return inc_text[:last.start()] + entry + inc_text[last.start():], False


def logo_dir(root: Path) -> Path:
    return root / LOGO_REL


def is_project_root(root: Path) -> bool:
    return (logo_dir(root) / "a.inc").is_file() and (logo_dir(root) / "a").is_dir()


def read_inc(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def scan_entries(root: Path) -> list[tuple[str, str, list[str]]]:
    """Return (letter, macro, names) for every existing entry of every .inc file."""
    result = []
    for letter in LETTERS:
        inc = logo_dir(root) / f"{letter}.inc"
        if not inc.is_file():
            continue
        for block in BLOCK_RE.finditer(read_inc(inc)):
            names = []
            names_match = NAMES_RE.search(block.group(0))
            if names_match:
                names = [m.group(1) for m in STRING_RE.finditer(names_match.group(1))]
            result.append((letter, block.group(1), names))
    return result


def existing_txt_files(root: Path, stem: str) -> list[Path]:
    """All <folder>/<stem>.txt files, compared case-insensitively."""
    found = []
    base = logo_dir(root)
    for folder in sorted(base.iterdir()):
        if folder.is_dir() and re.fullmatch(r"[a-z_]", folder.name):
            for txt in folder.glob("*.txt"):
                if txt.stem.lower() == stem.lower():
                    found.append(txt)
    return found


def art_warnings(spec: LogoSpec) -> list[str]:
    """Cheap checks that are safe to run while typing."""
    warnings = []
    used = sorted({int(m.group(1)) for m in re.finditer(r"\$([1-9])", spec.art.replace("$$", ""))})
    missing = [n for n in used if n > len(spec.colors)]
    if missing:
        warnings.append(
            "The logo uses " + ", ".join(f"${n}" for n in missing)
            + f" but only {len(spec.colors)} color(s) are defined.")
    return warnings


def validate_fields(spec: LogoSpec) -> tuple[list[str], list[str]]:
    """Checks that do not depend on the project tree: names, colors and the logo text."""
    errors: list[str] = []
    warnings: list[str] = art_warnings(spec)

    if not spec.names:
        errors.append("Enter at least one logo name.")
    elif len(spec.names) > MAX_NAMES:
        errors.append(f"At most {MAX_NAMES} names are allowed.")
    if spec.small:
        bad = [n for n in spec.names if not n.lower().endswith(("_small", "-small"))]
        if bad:
            errors.append("Names of a small logo must end with '_small' or '-small': " + ", ".join(bad))

    if not spec.colors:
        errors.append("Add at least one color.")
    elif len(spec.colors) > MAX_COLORS:
        errors.append(f"At most {MAX_COLORS} colors are allowed.")
    for index, color in enumerate(spec.colors, start=1):
        problem = color.error()
        if problem:
            errors.append(f"Color ${index}: {problem}")
    for label, color in (("Keys color", spec.color_keys), ("Title color", spec.color_title)):
        if color is not None and color.error():
            errors.append(f"{label}: {color.error()}")

    if not spec.art.strip():
        errors.append("The logo text is empty.")
    return errors, warnings


def name_clash_warnings(root: Path, spec: LogoSpec) -> list[str]:
    """Names of `spec` that are already used by a different logo."""
    warnings = []
    for letter, macro, names in scan_entries(root):
        if macro != spec.macro:
            clash = {n.lower() for n in names} & {n.lower() for n in spec.names}
            if clash:
                warnings.append(
                    "Name(s) " + ", ".join(sorted(clash))
                    + f" are already used by {macro} in {letter}.inc.")
    return warnings


def validate(spec: LogoSpec, root: Optional[Path]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block saving, warnings need a confirmation."""
    errors: list[str] = []

    if root is None or not is_project_root(root):
        errors.append("The project folder is not a fastfetch source tree (src/logo/ascii not found).")
    if spec.letter not in LETTERS:
        errors.append("Select the folder letter.")
    if not spec.stem:
        errors.append("Enter a logo file name.")
    elif not STEM_RE.fullmatch(spec.stem):
        errors.append("The logo file name may only contain a-z, 0-9 and '_'.")

    field_errors, warnings = validate_fields(spec)
    errors += field_errors

    if errors or root is None:
        return errors, warnings

    # Checks against the existing tree
    for txt in existing_txt_files(root, spec.stem):
        if txt.parent.name != spec.letter:
            errors.append(f"A logo named '{spec.stem}' already exists in folder '{txt.parent.name}'.")
    for letter, macro, _names in scan_entries(root):
        if macro == spec.macro and letter != spec.letter:
            errors.append(f"An entry for {spec.macro} already exists in {letter}.inc.")
    warnings += name_clash_warnings(root, spec)
    return errors, warnings


def validate_edit(spec: LogoSpec, root: Optional[Path]) -> tuple[list[str], list[str]]:
    """Like validate(), for an existing logo whose file name and letter do not change."""
    errors, warnings = validate_fields(spec)
    if root is None or not is_project_root(root):
        errors.append("The project folder is not a fastfetch source tree (src/logo/ascii not found).")
    if errors or root is None:
        return errors, warnings
    return errors, warnings + name_clash_warnings(root, spec)


def replacement_notes(spec: LogoSpec, root: Path) -> list[str]:
    """Things that will be overwritten (to be confirmed by the user)."""
    notes = []
    txt = logo_dir(root) / spec.letter / f"{spec.stem}.txt"
    if txt.exists():
        notes.append(f"{txt.relative_to(root)} already exists and will be overwritten.")
    inc = logo_dir(root) / f"{spec.letter}.inc"
    if any(b.group(1) == spec.macro for b in BLOCK_RE.finditer(read_inc(inc))):
        notes.append(f"The existing {spec.macro} entry in {inc.name} will be replaced.")
    return notes


def apply_logo(root: Path, spec: LogoSpec, art_bytes: bytes) -> list[Path]:
    """Write the .txt file and update the .inc file. Returns the changed paths."""
    inc_path = logo_dir(root) / f"{spec.letter}.inc"
    txt_dir = logo_dir(root) / spec.letter
    txt_path = txt_dir / f"{spec.stem}.txt"

    # Compute the new .inc first so that a failure leaves the tree untouched
    new_inc, _ = insert_or_replace_entry(read_inc(inc_path), spec)

    txt_dir.mkdir(parents=True, exist_ok=True)
    txt_path.write_bytes(art_bytes)
    inc_path.write_bytes(new_inc.encode("utf-8"))
    return [txt_path, inc_path]


def art_to_bytes(art: str, loaded_raw: Optional[bytes]) -> bytes:
    """Exact bytes of a loaded, unmodified file; otherwise the editor text."""
    if loaded_raw is not None:
        try:
            original = loaded_raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            original = None
        if original == art:
            return loaded_raw
    text = art if art.endswith("\n") else art + "\n"
    return text.encode("utf-8")


# ----------------------------------------------------------------------------
# Reading existing entries (used by the "Edit logo" tab)
# ----------------------------------------------------------------------------

ENTRY_FIELDS = {"names", "lines", "type", "colors", "colorKeys", "colorTitle"}
TYPE_BITS = {"FF_LOGO_LINE_TYPE_SMALL_BIT": "small", "FF_LOGO_LINE_TYPE_ALTER_BIT": "alter"}
_COLOR_256_RE = re.compile(r'FF_COLOR_FG_256\s+"([^"]*)"')
_COLOR_RGB_RE = re.compile(r'FF_COLOR_FG_RGB\s+"([^"]*)"')
_STRING_OR_SPACE_RE = re.compile(r'("(?:[^"\\]|\\.)*")|\s+')
_COLOR_TOKEN_RE = re.compile(r'(?:[^,"]|"(?:[^"\\]|\\.)*")+')


@dataclass
class ParsedEntry:
    """One entry of a .inc file, read back into a LogoSpec."""

    letter: str                  # the .inc file the entry lives in
    macro: str                   # FASTFETCH_DATATEXT_LOGO_<macro>
    spec: LogoSpec
    block: str                   # the exact original text (#ifdef ... #endif)
    problems: list = field(default_factory=list)   # why the entry cannot be edited safely

    @property
    def exact(self) -> bool:
        """True when rendering the spec gives back the original text byte for byte."""
        return not self.problems and render_entry(self.spec) == self.block


def _canonical(text: str) -> str:
    """Whitespace-, comma- and display-comment-insensitive form used to compare entries."""
    lines = text.splitlines()
    if len(lines) > 1 and lines[1].strip().startswith("//"):
        del lines[1]
    flat = _STRING_OR_SPACE_RE.sub(lambda m: m.group(1) or "", "\n".join(lines))
    return re.sub(r",(?=\})", "", flat)


def parse_color_token(token: str) -> Optional[ColorSpec]:
    token = token.strip()
    match = _COLOR_256_RE.fullmatch(token)
    if match:
        spec = ColorSpec("256", match.group(1))
    else:
        match = _COLOR_RGB_RE.fullmatch(token)
        if match:
            spec = ColorSpec("rgb", match.group(1))
        elif token in _CONST_TO_SGR:
            spec = ColorSpec("named", token)
        else:
            return None
    return None if spec.error() else spec.normalized()


def parse_entry(block: str, letter: str) -> ParsedEntry:
    """
    Read one #ifdef ... #endif entry. Whatever cannot be represented by a LogoSpec is
    reported in `problems`; such entries must not be rewritten by the editor.
    """
    head = re.match(r"\s*#ifdef FASTFETCH_DATATEXT_LOGO_([A-Z0-9_]+)", block)
    macro = head.group(1) if head else "?"
    spec = LogoSpec(letter=letter, stem=macro.lower())
    entry = ParsedEntry(letter=letter, macro=macro, spec=spec, block=block)
    problems = entry.problems
    if head is None:
        problems.append("The entry does not start with #ifdef FASTFETCH_DATATEXT_LOGO_<NAME>.")
        return entry

    lines = block.splitlines()
    if len(lines) > 1 and lines[1].strip().startswith("//"):
        spec.display = lines[1].strip()[2:].strip()
        body_lines = lines[:1] + lines[2:]
    else:
        body_lines = lines
    body = "\n".join(body_lines)
    plain = STRING_RE.sub('""', body)           # strings removed: safe to look for syntax
    if "//" in plain or "/*" in plain:
        problems.append("The entry contains comments.")
    if "#" in re.sub(r"^\s*#(ifdef|endif)\b.*$", "", plain, flags=re.MULTILINE):
        problems.append("The entry contains preprocessor lines other than #ifdef/#endif.")

    found_fields = list(dict.fromkeys(re.findall(r"\.(\w+)\s*=", plain)))
    spec.order = [f for f in found_fields if f in ENTRY_FIELDS]
    unknown = sorted(set(found_fields) - ENTRY_FIELDS)
    if unknown:
        problems.append("Unsupported field(s): " + ", ".join("." + f for f in unknown))

    names_match = NAMES_RE.search(body)
    if names_match:
        for raw in (m.group(1) for m in STRING_RE.finditer(names_match.group(1))):
            if re.search(r'\\(?!["\\])', raw):
                problems.append(f"A name contains an unsupported escape sequence: {raw}")
            spec.names.append(re.sub(r'\\(["\\])', r"\1", raw))
    else:
        problems.append("The entry has no .names list.")

    lines_match = re.search(r"\.lines\s*=\s*FASTFETCH_DATATEXT_LOGO_([A-Z0-9_]+)\s*,", body)
    if lines_match is None or lines_match.group(1) != macro:
        problems.append(f".lines does not use FASTFETCH_DATATEXT_LOGO_{macro}.")

    type_match = re.search(r"\.type\s*=\s*([^,\n]+),", plain)
    if type_match:
        for bit in (b.strip() for b in type_match.group(1).split("|")):
            if bit in TYPE_BITS:
                setattr(spec, TYPE_BITS[bit], True)
            else:
                problems.append(f"Unsupported .type value: {bit}")

    colors_match = re.search(r"\.colors\s*=\s*\{(.*?)\}\s*,", body, re.DOTALL)
    if colors_match:
        for token in _COLOR_TOKEN_RE.findall(colors_match.group(1)):
            if not token.strip():
                continue
            color = parse_color_token(token)
            if color is None:
                problems.append(f"Unsupported color: {token.strip()}")
                color = ColorSpec("named", "FF_COLOR_FG_DEFAULT")   # keeps $N numbering aligned
            spec.colors.append(color)
    else:
        problems.append("The entry has no .colors list.")

    for key, attr in (("colorKeys", "color_keys"), ("colorTitle", "color_title")):
        match = re.search(rf"\.{key}\s*=\s*(.+?)\s*,\s*$", body, re.MULTILINE)
        if match:
            color = parse_color_token(match.group(1))
            if color is None:
                problems.append(f"Unsupported .{key}: {match.group(1)}")
            setattr(spec, attr, color)

    if not problems and _canonical(render_entry(spec)) != _canonical(block):
        problems.append("The entry has a layout or content that the editor cannot reproduce.")
    return entry


def scan_letter(root: Path, letter: str) -> list[ParsedEntry]:
    """Every entry of <letter>.inc, in file order."""
    inc = logo_dir(root) / f"{letter}.inc"
    if letter not in LETTERS or not inc.is_file():
        return []
    return [parse_entry(block.group(0), letter) for block in BLOCK_RE.finditer(read_inc(inc))]


def txt_files_for_macro(root: Path, macro: str) -> list[Path]:
    """
    The logo text file(s) behind FASTFETCH_DATATEXT_LOGO_<macro>. CMake names the macro
    after the file name up to the first dot, upper-cased, from any folder [a-z_].
    """
    found = []
    base = logo_dir(root)
    for folder in sorted(base.iterdir()):
        if folder.is_dir() and re.fullmatch(r"[a-z_]", folder.name):
            for txt in sorted(folder.glob("*.txt")):
                if txt.name.split(".")[0].upper() == macro:
                    found.append(txt)
    return found


def spec_key(spec: LogoSpec) -> tuple:
    """Everything that ends up in the .inc entry, in a comparable form."""
    def color(c: Optional[ColorSpec]):
        return None if c is None else (c.normalized().kind, c.normalized().value)
    return (spec.display, tuple(spec.names), tuple(color(c) for c in spec.colors),
            color(spec.color_keys), color(spec.color_title), spec.small, spec.alter)


def remove_entry(inc_text: str, macro: str) -> tuple[str, bool]:
    """Remove the first entry with this macro. Returns (new_text, removed)."""
    for block in BLOCK_RE.finditer(inc_text):
        if block.group(1) == macro:
            return inc_text[:block.start()] + inc_text[block.end():], True
    return inc_text, False


def update_logo(root: Path, spec: LogoSpec, art_bytes: bytes, txt_path: Optional[Path],
                write_entry: bool, write_art: bool) -> list[Path]:
    """
    Save an edited logo. The entry is replaced in place (same position in the .inc file);
    the .txt file is written to `txt_path` (or <letter>/<stem>.txt when it does not exist yet).
    Returns the changed paths.
    """
    inc_path = logo_dir(root) / f"{spec.letter}.inc"
    new_inc = None
    if write_entry:
        new_inc, replaced = insert_or_replace_entry(read_inc(inc_path), spec)
        if not replaced:
            raise ValueError(
                f"The {spec.macro} entry was not found in {inc_path.name}. "
                "It was probably changed outside the editor; reload the list.")
    changed = []
    if write_art:
        target = txt_path or (logo_dir(root) / spec.letter / f"{spec.stem}.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(art_bytes)
        changed.append(target)
    if new_inc is not None:
        inc_path.write_bytes(new_inc.encode("utf-8"))
        changed.append(inc_path)
    return changed


def delete_plan(root: Path, letter: str, macro: str) -> tuple[list[Path], list[str]]:
    """
    What deleting the entry would do: (txt files that get deleted, other .inc letters that
    still use the same logo text). The text is only deleted when nothing else uses it.
    """
    others = [l for l, m, _ in scan_entries(root) if m == macro]
    if letter in others:
        others.remove(letter)
    txts = [] if others else txt_files_for_macro(root, macro)
    return txts, others


def delete_logo(root: Path, letter: str, macro: str) -> tuple[Path, list[Path]]:
    """Delete an entry and (unless it is still used elsewhere) its .txt file."""
    inc_path = logo_dir(root) / f"{letter}.inc"
    new_inc, removed = remove_entry(read_inc(inc_path), macro)
    if not removed:
        raise ValueError(
            f"The {macro} entry was not found in {inc_path.name}. "
            "It was probably changed outside the editor; reload the list.")
    txts, _others = delete_plan(root, letter, macro)
    inc_path.write_bytes(new_inc.encode("utf-8"))
    for txt in txts:
        txt.unlink()
    return inc_path, txts


def preview_html(art: str, colors: list) -> str:
    """Render the logo text like fastfetch does: $1..$9 switch color, $$ is a literal $."""
    default = colors[0].to_hex() if colors else DEFAULT_HEX
    current = default
    segments: list[tuple[str, str]] = []

    def put(text: str) -> None:
        if segments and segments[-1][0] == current:
            segments[-1] = (current, segments[-1][1] + text)
        else:
            segments.append((current, text))

    art = art.replace("\r\n", "\n")
    i = 0
    while i < len(art):
        ch = art[i]
        if ch == "$":
            nxt = art[i + 1] if i + 1 < len(art) else ""
            if nxt == "$" or nxt == "":
                put("$")
                i += 2
                continue
            if nxt in "123456789":
                index = int(nxt) - 1
                current = colors[index].to_hex() if index < len(colors) else DEFAULT_HEX
                i += 2
                continue
            put("$")
            i += 1
            continue
        if ch == "\x1b":                       # raw ANSI sequences are skipped in the preview
            match = re.match(r"\x1b\[[0-9;]*m", art[i:])
            if match:
                i += len(match.group(0))
                continue
        if ch == "\t":
            put("    ")
        elif ch != "\r":
            put(ch)
        i += 1

    body = "".join(
        f'<span style="color:{color}">{html.escape(text)}</span>' for color, text in segments)
    return f'<pre style="margin:0">{body}</pre>'


def find_project_root(argument: Optional[str]) -> Optional[Path]:
    candidates = []
    if argument:
        candidates.append(Path(argument).expanduser())
    here = Path(__file__).resolve().parent
    candidates += [here, Path.cwd()]
    for base in (here, Path.cwd()):
        candidates += sorted(base.glob("fastfetch-*"))
    for candidate in candidates:
        if candidate.is_dir() and is_project_root(candidate):
            return candidate.resolve()
    return None


# ----------------------------------------------------------------------------
# Presets: JSONC check, terminal preview and installing a preset
# ----------------------------------------------------------------------------

PRESETS_REL = Path("presets")
CONFIG_NAME = "config.jsonc"
PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PREVIEW_COLUMNS = 120          # terminal size reported to fastfetch for the preview
PREVIEW_LINES = 40
PREVIEW_TIMEOUT_MS = 30000
PREVIEW_BACKGROUND = "#1e1e1e"
PREVIEW_FONT_SHRINK = 4        # the preview font is this many points smaller (4 steps of Ctrl + mouse wheel)

# Run with "sh -c": $1 is the target directory, $2 the file that is installed there.
APPLY_SCRIPT = f'mkdir -p -- "$1" && install -m 644 -- "$2" "$1/{CONFIG_NAME}"'


@dataclass
class Preset:
    name: str    # shown in the list, e.g. "satellaos" or "examples/10"
    path: Path


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def scan_presets(root: Path) -> list[Preset]:
    """All *.jsonc files below <root>/presets: top-level files first, then sub-folders."""
    base = root / PRESETS_REL
    if not base.is_dir():
        return []
    found = []
    for path in base.rglob("*.jsonc"):
        if path.is_file():
            found.append(Preset(path.relative_to(base).with_suffix("").as_posix(), path))
    found.sort(key=lambda preset: ("/" in preset.name, _natural_key(preset.name)))
    return found


_JSONC_COMMENT_RE = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r'"(?:[^"\\]|\\.)*"|,(?=\s*[}\]])', re.DOTALL)


def strip_jsonc(text: str) -> str:
    """Turn JSONC (comments, trailing commas) into plain JSON. Line numbers are kept."""
    def drop_comment(match: re.Match) -> str:
        piece = match.group(0)
        if piece.startswith('"'):
            return piece
        return "\n" * piece.count("\n") if piece.startswith("/*") else ""

    def drop_comma(match: re.Match) -> str:
        piece = match.group(0)
        return piece if piece.startswith('"') else ""

    return _TRAILING_COMMA_RE.sub(drop_comma, _JSONC_COMMENT_RE.sub(drop_comment, text))


def jsonc_error(text: str) -> Optional[str]:
    """None when `text` is valid JSONC, otherwise a short description of the first problem."""
    try:
        json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return None


# A cell style is (foreground, background, bold, italic, underline, reverse); colors are
# "#rrggbb" strings or None for the terminal default.
_BLANK_STYLE = (None, None, False, False, False, False)
_ANSI_TOKEN_RE = re.compile(
    r"\x1b\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])"      # CSI sequence (colors, cursor, erase)
    r"|\x1b\](?P<osc>[^\x07\x1b]*)(?:\x07|\x1b\\)?"        # OSC sequence (window title, ...)
    r"|\x1b(?P<esc>[()*+][0-9A-Za-z]|.)"                   # charset selection, save/restore cursor
    r"|(?P<text>[^\x1b]+)",
    re.DOTALL,
)


class AnsiScreen:
    """
    A tiny terminal emulator. It replays the escape sequences fastfetch prints (colors,
    cursor movement, erasing) into a grid of characters, so that the logo and the module
    lines end up side by side exactly like in a real terminal, and shows the result as HTML.
    """

    def __init__(self) -> None:
        self.rows: list[list[tuple[str, tuple]]] = [[]]    # every cell is (text, style)
        self.row = 0
        self.col = 0
        self.style = _BLANK_STYLE
        self.saved = (0, 0)

    # ---- input ---------------------------------------------------------
    def feed(self, data: str) -> None:
        for token in _ANSI_TOKEN_RE.finditer(data):
            text = token.group("text")
            if text is not None:
                self._write(text)
            elif token.group("final") is not None:
                self._csi(token.group("params"), token.group("final"))
            elif token.group("esc") == "7":
                self.saved = (self.row, self.col)
            elif token.group("esc") == "8":
                self.row, self.col = self.saved

    def _write(self, text: str) -> None:
        for ch in text:
            if ch == "\n":
                self.row += 1
                self.col = 0
            elif ch == "\r":
                self.col = 0
            elif ch == "\t":
                for _ in range(8 - self.col % 8):
                    self._put(" ")
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch >= " " and ch != "\x7f":
                self._put(ch)

    def _line(self) -> list:
        while len(self.rows) <= self.row:
            self.rows.append([])
        return self.rows[self.row]

    def _put(self, ch: str) -> None:
        line = self._line()
        if unicodedata.combining(ch):                      # accents stick to the previous cell
            if 0 < self.col <= len(line):
                base, style = line[self.col - 1]
                line[self.col - 1] = (base + ch, style)
            return
        width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        while len(line) < self.col:
            line.append((" ", _BLANK_STYLE))
        line[self.col:self.col + width] = [(ch, self.style)] + [("", self.style)] * (width - 1)
        self.col += width

    def _csi(self, params: str, final: str) -> None:
        if params[:1] in ("?", "<", "=", ">"):
            return                                         # private modes: cursor, line wrap, ...
        args = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else []
        first = args[0] if args and args[0] > 0 else 1
        if final == "m":
            self._sgr(args)
        elif final == "A":
            self.row = max(0, self.row - first)
        elif final == "B":
            self.row += first
        elif final == "C":
            self.col += first
        elif final == "D":
            self.col = max(0, self.col - first)
        elif final == "E":
            self.row += first
            self.col = 0
        elif final == "F":
            self.row = max(0, self.row - first)
            self.col = 0
        elif final == "G":
            self.col = first - 1
        elif final in ("H", "f"):
            self.row = first - 1
            self.col = (args[1] if len(args) > 1 and args[1] > 0 else 1) - 1
        elif final == "d":
            self.row = first - 1
        elif final == "K":
            self._erase_line(args[0] if args else 0)
        elif final == "J":
            self._erase_display(args[0] if args else 0)
        elif final == "s":
            self.saved = (self.row, self.col)
        elif final == "u":
            self.row, self.col = self.saved

    def _erase_line(self, mode: int) -> None:
        line = self._line()
        if mode == 0:
            del line[self.col:]
        elif mode == 1:
            line[:self.col + 1] = [(" ", _BLANK_STYLE)] * (self.col + 1)
        else:
            line.clear()

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            del self.rows[self.row + 1:]
        elif mode == 1:
            self._erase_line(1)
            for index in range(min(self.row, len(self.rows))):
                self.rows[index] = []
        else:
            self.rows = [[]]

    def _sgr(self, args: list) -> None:
        fg, bg, bold, italic, underline, reverse = self.style
        args = args or [0]
        i = 0
        while i < len(args):
            code = args[i]
            if code == 0:
                fg = bg = None
                bold = italic = underline = reverse = False
            elif code == 1:
                bold = True
            elif code == 3:
                italic = True
            elif code == 4:
                underline = True
            elif code == 7:
                reverse = True
            elif code == 22:
                bold = False
            elif code == 23:
                italic = False
            elif code == 24:
                underline = False
            elif code == 27:
                reverse = False
            elif 30 <= code <= 37 or 90 <= code <= 97:
                fg = _SGR_HEX[str(code)]
            elif code == 39:
                fg = None
            elif 40 <= code <= 47 or 100 <= code <= 107:
                bg = _SGR_HEX[str(code - 10)]
            elif code == 49:
                bg = None
            elif code in (38, 48):
                color = None
                if i + 2 < len(args) and args[i + 1] == 5:                 # 38;5;<0-255>
                    color = xterm256_hex(min(args[i + 2], 255))
                    i += 2
                elif i + 4 < len(args) and args[i + 1] == 2:               # 38;2;<r>;<g>;<b>
                    r, g, b = (min(value, 255) for value in args[i + 2:i + 5])
                    color = f"#{r:02x}{g:02x}{b:02x}"
                    i += 4
                if code == 38:
                    fg = color
                else:
                    bg = color
            i += 1
        self.style = (fg, bg, bold, italic, underline, reverse)

    # ---- output --------------------------------------------------------
    @staticmethod
    def _is_blank(cell: tuple) -> bool:
        char, (_fg, bg, _bold, _italic, underline, reverse) = cell
        return char in ("", " ") and bg is None and not underline and not reverse

    @staticmethod
    def _span(chars: list, style: Optional[tuple]) -> str:
        if not chars:
            return ""
        text = html.escape("".join(chars))
        fg, bg, bold, italic, underline, reverse = style
        if reverse:
            fg, bg = bg or PREVIEW_BACKGROUND, fg or DEFAULT_HEX
        css = []
        if fg:
            css.append(f"color:{fg}")
        if bg:
            css.append(f"background-color:{bg}")
        if bold:
            css.append("font-weight:bold")
        if italic:
            css.append("font-style:italic")
        if underline:
            css.append("text-decoration:underline")
        return f'<span style="{";".join(css)}">{text}</span>' if css else text

    def _line_html(self, line: list) -> str:
        end = len(line)
        while end and self._is_blank(line[end - 1]):
            end -= 1
        parts, run, style = [], [], None
        for char, cell_style in line[:end]:
            if cell_style != style:
                parts.append(self._span(run, style))
                run, style = [], cell_style
            run.append(char)
        parts.append(self._span(run, style))
        return "".join(parts)

    def to_html(self) -> str:
        lines = [self._line_html(line) for line in self.rows]
        while lines and not lines[-1]:
            lines.pop()
        return '<pre style="margin:0">' + "\n".join(lines) + "</pre>"

    def to_text(self) -> str:
        lines = ["".join(char for char, _ in line).rstrip() for line in self.rows]
        return "\n".join(lines).strip("\n")


def ansi_to_html(data: str) -> str:
    """Render text with ANSI escape sequences (fastfetch output) as colored HTML."""
    screen = AnsiScreen()
    screen.feed(data)
    return screen.to_html()


def ansi_to_text(data: str) -> str:
    """The same text without any escape sequences."""
    screen = AnsiScreen()
    screen.feed(data)
    return screen.to_text()


def find_fastfetch(root: Optional[Path]) -> str:
    """The binary used for the preview: <project>/build/fastfetch, else the one in PATH."""
    if root is not None:
        built = root / "build" / "fastfetch"
        if built.is_file() and os.access(built, os.X_OK):
            return str(built)
    return shutil.which("fastfetch") or ""


@dataclass
class ApplyTarget:
    key: str
    label: str            # text of the button
    who: str              # used in the confirmation: "Install the preset for <who>?"
    directory: Path       # config.jsonc is installed here
    needs_root: bool
    tooltip: str
    after: str            # shown after a successful install


def apply_targets() -> list[ApplyTarget]:
    return [
        ApplyTarget(
            "user", "Apply for current user", "the current user",
            Path.home() / ".config" / "fastfetch", False,
            f"Install the preset as ~/.config/fastfetch/{CONFIG_NAME} for the current user.",
            "Run fastfetch in a new terminal to see it."),
        ApplyTarget(
            "skel", "Apply for other users", "users created from now on (/etc/skel)",
            Path("/etc/skel/.config/fastfetch"), True,
            f"Install the preset as /etc/skel/.config/fastfetch/{CONFIG_NAME}, so every user "
            "created later starts with it. Existing users are not changed.",
            "Users created from now on get this configuration. Existing users are not changed."),
        ApplyTarget(
            "root", "Apply for root", "root",
            Path("/root/.config/fastfetch"), True,
            f"Install the preset as /root/.config/fastfetch/{CONFIG_NAME}.",
            "Run fastfetch as root to see it."),
    ]


def apply_command(directory: Path, source: str, escalate: bool) -> list[str]:
    """The command that creates `directory` (mkdir -p) and installs `source` as config.jsonc."""
    command = ["sh", "-c", APPLY_SCRIPT, "sh", str(directory), source]
    return ["pkexec"] + command if escalate else command


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def build_gui(argument: Optional[str]):
    """Create the QApplication and the main window. Raises ImportError without PyQt6."""
    from PyQt6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, pyqtSignal
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtWidgets import (
        QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
        QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
        QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
        QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
    )

    ROLE = Qt.ItemDataRole.UserRole
    YES = QMessageBox.StandardButton.Yes
    NO = QMessageBox.StandardButton.No

    def ask(parent, title: str, text: str) -> bool:
        """Yes/No question; the default answer is No."""
        return QMessageBox.question(parent, title, text, YES | NO, NO) == YES

    class ColorEdit(QWidget):
        """Named color / 256-color / RGB chooser with a swatch."""

        changed = pyqtSignal()

        def __init__(self, allow_unset: bool = False, parent=None):
            super().__init__(parent)
            self.combo = QComboBox()
            if allow_unset:
                self.combo.addItem("(not set)", ("unset", ""))
            for label, const, _ in NAMED_COLORS:
                self.combo.addItem(label, ("named", const))
            self.combo.addItem("256-color index", ("256", ""))
            self.combo.addItem("RGB", ("rgb", ""))

            self.value = QLineEdit()
            self.value.setMaximumWidth(110)
            self.pick = QPushButton("Pick...")
            self.swatch = QLabel()
            self.swatch.setFixedSize(18, 18)

            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            for widget in (self.combo, self.value, self.pick, self.swatch):
                layout.addWidget(widget)
            layout.addStretch(1)

            self.combo.currentIndexChanged.connect(self._kind_changed)
            self.value.textChanged.connect(self._update)
            self.pick.clicked.connect(self._pick_rgb)
            self._kind_changed()

        def _data(self) -> tuple:
            return self.combo.currentData() or ("unset", "")

        def _kind(self) -> str:
            return self._data()[0]

        def _kind_changed(self) -> None:
            kind = self._kind()
            self.value.setVisible(kind in ("256", "rgb"))
            self.pick.setVisible(kind == "rgb")
            self.value.setPlaceholderText("0-255" if kind == "256" else "r;g;b")
            self._update()

        def _pick_rgb(self) -> None:
            color = QColorDialog.getColor(parent=self)
            if color.isValid():
                self.value.setText(f"{color.red()};{color.green()};{color.blue()}")

        def _update(self) -> None:
            spec = self.color()
            hex_color = spec.to_hex() if spec is not None else "transparent"
            self.swatch.setStyleSheet(f"background:{hex_color}; border:1px solid #888;")
            self.changed.emit()

        def color(self) -> Optional[ColorSpec]:
            kind, data = self._data()
            if kind == "unset":
                return None
            if kind == "named":
                return ColorSpec("named", data)
            return ColorSpec(kind, self.value.text())

        def set_color(self, spec: Optional[ColorSpec]) -> None:
            if spec is None:
                self.combo.setCurrentIndex(0)
                return
            for index in range(self.combo.count()):
                kind, data = self.combo.itemData(index)
                if kind == spec.kind and (kind != "named" or data == spec.value):
                    self.combo.setCurrentIndex(index)
                    if kind != "named":
                        self.value.setText(spec.value)
                    return

    class ColorList(QWidget):
        """The $1 ... $9 color rows."""

        changed = pyqtSignal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.rows: list[ColorEdit] = []
            self.row_labels: list[QLabel] = []
            self.rows_layout = QVBoxLayout()
            self.rows_layout.setContentsMargins(0, 0, 0, 0)

            self.add_button = QPushButton("Add color")
            self.remove_button = QPushButton("Remove last")
            buttons = QHBoxLayout()
            buttons.addWidget(self.add_button)
            buttons.addWidget(self.remove_button)
            buttons.addStretch(1)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addLayout(self.rows_layout)
            layout.addLayout(buttons)

            self.add_button.clicked.connect(lambda: self.add_row())
            self.remove_button.clicked.connect(self.remove_row)

        def add_row(self, spec: Optional[ColorSpec] = None) -> None:
            if len(self.rows) >= MAX_COLORS:
                return
            edit = ColorEdit()
            if spec is not None:
                edit.set_color(spec)
            edit.changed.connect(self.changed)
            label = QLabel(f"${len(self.rows) + 1}")
            label.setMinimumWidth(22)
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(edit, 1)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.rows_layout.addWidget(holder)
            self.rows.append(edit)
            self.row_labels.append(holder)
            self._refresh_buttons()
            self.changed.emit()

        def remove_row(self) -> None:
            if len(self.rows) <= 1:
                return
            self._pop_row()
            self._refresh_buttons()
            self.changed.emit()

        def _pop_row(self) -> None:
            self.rows.pop()
            holder = self.row_labels.pop()
            self.rows_layout.removeWidget(holder)
            holder.hide()
            holder.deleteLater()

        def set_colors(self, specs: list) -> None:
            """Replace all rows (at least one row is always kept)."""
            while self.rows:
                self._pop_row()
            for spec in specs or [ColorSpec("named", "FF_COLOR_FG_DEFAULT")]:
                self.add_row(spec)

        def _refresh_buttons(self) -> None:
            self.add_button.setEnabled(len(self.rows) < MAX_COLORS)
            self.remove_button.setEnabled(len(self.rows) > 1)

        def colors(self) -> list:
            return [row.color() for row in self.rows]

    class LogoForm(QSplitter):
        """
        The logo entry form (names, colors, flags), the logo text and the previews.
        Shared by the "Add logo" and "Edit logo" tabs. The owner sets `letter` and `stem`
        and adds its own rows (`form`) and buttons (`buttons`).
        """

        changed = pyqtSignal()    # a field was edited by the user (not by fill())

        def __init__(self, parent=None):
            super().__init__(parent)
            self.letter = ""
            self.stem = ""
            self.loaded_raw: Optional[bytes] = None
            self.extra_note = ""                        # shown above the logo-text warnings
            self.inc_override: Optional[str] = None     # shown instead of the generated entry
            self._filling = False
            mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

            self.display_edit = QLineEdit()
            self.display_edit.setPlaceholderText("Comment above the entry, e.g. SatellaOS (optional)")
            self.names_edit = QLineEdit()
            self.names_edit.setPlaceholderText(f"Comma separated, up to {MAX_NAMES}: SatellaOS, satellaos")
            self.color_list = ColorList()
            self.color_list.set_colors([ColorSpec("named", "FF_COLOR_FG_BLUE"),
                                        ColorSpec("named", "FF_COLOR_FG_WHITE")])
            self.keys_edit = ColorEdit(allow_unset=True)
            self.title_edit = ColorEdit(allow_unset=True)
            self.small_check = QCheckBox("Small logo (names must end with _small or -small)")
            self.alter_check = QCheckBox("Alternative logo (FF_LOGO_LINE_TYPE_ALTER_BIT)")

            self.file_edit = QLineEdit()
            self.file_edit.setReadOnly(True)
            self.file_edit.setPlaceholderText("Load an existing .txt file or paste the logo below")
            self.load_button = QPushButton("Load .txt...")
            file_row = QHBoxLayout()
            file_row.addWidget(self.file_edit, 1)
            file_row.addWidget(self.load_button)
            self.art_edit = QPlainTextEdit()
            self.art_edit.setFont(mono)
            self.art_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.art_edit.setPlaceholderText("ASCII logo. $1 ... $9 switch to a color, $$ prints a '$'.")

            self.form = QFormLayout()
            self.form.addRow("Display name", self.display_edit)
            self.form.addRow("Names", self.names_edit)
            self.form.addRow("Colors", self.color_list)
            self.form.addRow("Keys color", self.keys_edit)
            self.form.addRow("Title color", self.title_edit)
            self.form.addRow("", self.small_check)
            self.form.addRow("", self.alter_check)
            self.options_box = QGroupBox("Logo entry")
            self.options_box.setLayout(self.form)

            art_box = QGroupBox("Logo text (.txt)")
            art_layout = QVBoxLayout(art_box)
            art_layout.addLayout(file_row)
            art_layout.addWidget(self.art_edit, 1)

            left = QWidget()
            left_layout = QVBoxLayout(left)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.addWidget(self.options_box)
            left_layout.addWidget(art_box, 1)

            self.preview = QTextBrowser()
            self.preview.setFont(mono)
            self.preview.setStyleSheet("background:#1e1e1e; color:#d0d0d0;")
            self.inc_preview = QPlainTextEdit()
            self.inc_preview.setReadOnly(True)
            self.inc_preview.setFont(mono)
            tabs = QTabWidget()
            tabs.addTab(self.preview, "Logo preview")
            tabs.addTab(self.inc_preview, ".inc entry")

            self.notes = QLabel()
            self.notes.setWordWrap(True)
            self.notes.setStyleSheet("color:#c4a000;")
            self.buttons = QHBoxLayout()
            right = QWidget()
            right_layout = QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.addWidget(tabs, 1)
            right_layout.addWidget(self.notes)
            right_layout.addLayout(self.buttons)

            self.addWidget(left)
            self.addWidget(right)
            self.setStretchFactor(0, 5)
            self.setStretchFactor(1, 4)

            self.load_button.clicked.connect(self.load_txt)
            for edit in (self.display_edit, self.names_edit):
                edit.textChanged.connect(self._edited)
            self.art_edit.textChanged.connect(self._edited)
            self.color_list.changed.connect(self._edited)
            self.keys_edit.changed.connect(self._edited)
            self.title_edit.changed.connect(self._edited)
            self.small_check.toggled.connect(self._edited)
            self.alter_check.toggled.connect(self._edited)

        # ---- state ---------------------------------------------------------
        def collect(self) -> LogoSpec:
            names = [n.strip() for n in self.names_edit.text().split(",") if n.strip()]
            colors = [c for c in self.color_list.colors() if c is not None]
            return LogoSpec(
                letter=self.letter,
                stem=self.stem,
                display=self.display_edit.text().strip(),
                names=names,
                colors=colors,
                color_keys=self.keys_edit.color(),
                color_title=self.title_edit.color(),
                small=self.small_check.isChecked(),
                alter=self.alter_check.isChecked(),
                art=self.art_edit.toPlainText(),
            )

        def fill(self, spec: LogoSpec, raw: Optional[bytes], source: str) -> None:
            """Show `spec`; `raw` are the exact bytes of its logo file (if it has one)."""
            self._filling = True
            try:
                self.display_edit.setText(spec.display)
                self.names_edit.setText(", ".join(spec.names))
                self.color_list.set_colors(spec.colors)
                self.keys_edit.set_color(spec.color_keys)
                self.title_edit.set_color(spec.color_title)
                self.small_check.setChecked(spec.small)
                self.alter_check.setChecked(spec.alter)
                self.art_edit.setPlainText(spec.art)
                self.loaded_raw = raw
                self.file_edit.setText(source)
            finally:
                self._filling = False
            self.refresh()

        def _edited(self) -> None:
            if self._filling:
                return
            self.refresh()
            self.changed.emit()

        def refresh(self) -> None:
            spec = self.collect()
            self.preview.setHtml(preview_html(spec.art, spec.colors))
            if self.inc_override is not None:
                self.inc_preview.setPlainText(self.inc_override)
            else:
                self.inc_preview.setPlainText(render_entry(spec) if spec.stem else "")
            notes = [self.extra_note] if self.extra_note else []
            if self.inc_override is None:
                notes += art_warnings(spec)
            self.notes.setText("\n".join(notes))

        def load_txt(self) -> None:
            filename, _ = QFileDialog.getOpenFileName(
                self, "Select the ASCII logo", str(Path.cwd()), "Text files (*.txt);;All files (*)")
            if not filename:
                return
            raw = Path(filename).read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                QMessageBox.critical(self, "Cannot read file", "The file is not valid UTF-8 text.")
                return
            self.loaded_raw = raw
            self.file_edit.setText(filename)
            self.art_edit.setPlainText(text.replace("\r\n", "\n"))
            self.loaded_file_selected(filename)

        def loaded_file_selected(self, filename: str) -> None:
            """Hook for the owner (the Add tab proposes a file name from it)."""

    class AddPage(QWidget):
        """The "Add logo" tab."""

        saved = pyqtSignal()

        def __init__(self, get_root):
            super().__init__()
            self.get_root = get_root
            self.form = LogoForm()

            self.letter_combo = QComboBox()
            self.letter_combo.addItem("- select -", "")
            for letter in LETTERS:
                self.letter_combo.addItem(letter, letter)
            self.stem_edit = QLineEdit()
            self.stem_edit.setPlaceholderText("e.g. satellaos  (a-z, 0-9, _)")
            self.target_label = QLabel()
            self.target_label.setWordWrap(True)
            self.target_label.setStyleSheet("color:#888;")
            self.form.form.insertRow(0, "Folder letter", self.letter_combo)
            self.form.form.insertRow(1, "Logo file name", self.stem_edit)
            self.form.form.insertRow(2, "", self.target_label)

            self.add_button = QPushButton("Add logo to the project")
            self.add_button.setMinimumHeight(34)
            self.form.buttons.addWidget(self.add_button)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.form)

            self.form.loaded_file_selected = self.propose_stem
            self.stem_edit.textChanged.connect(self.refresh)
            self.letter_combo.currentIndexChanged.connect(self.refresh)
            self.add_button.clicked.connect(self.save)
            self.refresh()

        def propose_stem(self, filename: str) -> None:
            if not self.stem_edit.text().strip():
                self.stem_edit.setText(re.sub(r"[^a-z0-9_]", "_", Path(filename).stem.lower()))

        def collect_spec(self) -> LogoSpec:
            self.form.letter = self.letter_combo.currentData() or ""
            self.form.stem = self.stem_edit.text().strip().lower()
            return self.form.collect()

        def refresh(self) -> None:
            spec = self.collect_spec()
            if spec.letter and spec.stem:
                self.target_label.setText(
                    f"{LOGO_REL.as_posix()}/{spec.letter}/{spec.stem}.txt  and  "
                    f"{LOGO_REL.as_posix()}/{spec.letter}.inc   (macro FASTFETCH_DATATEXT_LOGO_{spec.macro})")
            else:
                self.target_label.setText("")
            self.form.refresh()

        def root_changed(self) -> None:
            self.refresh()

        def save(self) -> None:
            root = self.get_root()
            spec = self.collect_spec()
            errors, warnings = validate(spec, root)
            if errors:
                QMessageBox.critical(self, "Cannot add the logo", "\n".join(f"- {e}" for e in errors))
                return
            confirmations = replacement_notes(spec, root) + warnings
            if confirmations:
                if not ask(self, "Please confirm",
                           "\n".join(f"- {c}" for c in confirmations) + "\n\nContinue?"):
                    return
            try:
                changed = apply_logo(root, spec, art_to_bytes(spec.art, self.form.loaded_raw))
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Writing failed", str(exc))
                return
            QMessageBox.information(
                self, "Logo added",
                "Changed files:\n" + "\n".join(f"- {p.relative_to(root)}" for p in changed)
                + "\n\nRebuild fastfetch to use it, e.g. fastfetch --logo " + spec.names[0])
            self.saved.emit()

    class EditPage(QWidget):
        """The "Edit logo" tab: pick a letter, pick a logo, change or delete it."""

        def __init__(self, get_root):
            super().__init__()
            self.get_root = get_root
            self.entries: list[ParsedEntry] = []
            self.current: Optional[ParsedEntry] = None
            self.txt_path: Optional[Path] = None
            self.txt_raw: Optional[bytes] = None
            self.art_editable = True
            self.dirty = False
            self._letter = ""
            self._guard = False

            self.letter_combo = QComboBox()
            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText("Filter the list...")
            self.filter_edit.setClearButtonEnabled(True)
            top = QHBoxLayout()
            top.addWidget(QLabel("Folder letter"))
            top.addWidget(self.letter_combo)
            top.addWidget(self.filter_edit, 1)

            self.list = QListWidget()
            left = QWidget()
            left_layout = QVBoxLayout(left)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.addLayout(top)
            left_layout.addWidget(self.list, 1)

            self.form = LogoForm()
            self.form.load_button.setText("Replace from .txt...")
            self.save_button = QPushButton("Save changes")
            self.save_button.setMinimumHeight(34)
            self.revert_button = QPushButton("Revert")
            self.delete_button = QPushButton("Delete logo")
            self.form.buttons.addWidget(self.save_button, 1)
            self.form.buttons.addWidget(self.revert_button)
            self.form.buttons.addWidget(self.delete_button)

            splitter = QSplitter()
            splitter.addWidget(left)
            splitter.addWidget(self.form)
            splitter.setStretchFactor(0, 2)
            splitter.setStretchFactor(1, 9)
            splitter.setSizes([330, 1050])
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(splitter)

            self.letter_combo.currentIndexChanged.connect(self.on_letter_changed)
            self.filter_edit.textChanged.connect(self.apply_filter)
            self.list.currentItemChanged.connect(self.on_item_changed)
            self.form.changed.connect(self.mark_dirty)
            self.save_button.clicked.connect(self.save)
            self.revert_button.clicked.connect(self.revert)
            self.delete_button.clicked.connect(self.delete)
            self.root_changed()

        # ---- tree / list handling ------------------------------------------
        def root_changed(self) -> None:
            root = self.get_root()
            self._guard = True
            try:
                self.letter_combo.clear()
                self.letter_combo.addItem("- select -", "")
                if root is not None:
                    self.fill_letters(root)
                self._letter = ""
                self.list.clear()
                self.entries = []
            finally:
                self._guard = False
            self.load_entry(None)

        def fill_letters(self, root: Path) -> None:
            for letter in LETTERS:
                inc = logo_dir(root) / f"{letter}.inc"
                if inc.is_file():
                    count = len(BLOCK_RE.findall(read_inc(inc)))
                    self.letter_combo.addItem(f"{letter}  ({count})", letter)

        def refresh_counts(self) -> None:
            """Update the (n) counters after a logo was added or deleted."""
            root = self.get_root()
            if root is None:
                return
            self._guard = True
            try:
                self.letter_combo.clear()
                self.letter_combo.addItem("- select -", "")
                self.fill_letters(root)
                index = self.letter_combo.findData(self._letter)
                self.letter_combo.setCurrentIndex(max(index, 0))
            finally:
                self._guard = False

        def reload_after_add(self) -> None:
            """Called when the Add tab changed the tree."""
            self.refresh_counts()
            if self._letter and not self.dirty:
                self.populate(self._letter, self.current.macro if self.current else None)

        def confirm_discard(self) -> bool:
            return (not self.dirty) or ask(
                self, "Unsaved changes",
                "The selected logo has unsaved changes. Discard them?")

        def on_letter_changed(self, _index: int) -> None:
            if self._guard:
                return
            letter = self.letter_combo.currentData() or ""
            if letter == self._letter:
                return
            if not self.confirm_discard():
                self._guard = True
                self.letter_combo.setCurrentIndex(max(self.letter_combo.findData(self._letter), 0))
                self._guard = False
                return
            self._letter = letter
            self.populate(letter)

        def populate(self, letter: str, select_macro: Optional[str] = None) -> None:
            root = self.get_root()
            self.entries = scan_letter(root, letter) if root is not None and letter else []
            self._guard = True
            try:
                self.list.clear()
                selected = None
                for index, entry in enumerate(self.entries):
                    title = entry.spec.display or (entry.spec.names[0] if entry.spec.names else entry.macro)
                    item = QListWidgetItem(("(!) " if entry.problems else "") + f"{title}   [{entry.macro.lower()}]")
                    item.setData(ROLE, index)       # Qt.ItemDataRole.UserRole
                    item.setToolTip("\n".join(entry.problems) if entry.problems
                                    else ", ".join(entry.spec.names))
                    self.list.addItem(item)
                    if entry.macro == select_macro and selected is None:
                        selected = item
                if selected is not None:
                    self.list.setCurrentItem(selected)
            finally:
                self._guard = False
            self.apply_filter()
            self.load_entry(self.entries[selected.data(ROLE)] if selected is not None else None)

        def apply_filter(self) -> None:
            needle = self.filter_edit.text().strip().lower()
            for row in range(self.list.count()):
                item = self.list.item(row)
                entry = self.entries[item.data(ROLE)]
                haystack = " ".join([entry.macro, entry.spec.display, *entry.spec.names]).lower()
                item.setHidden(bool(needle) and needle not in haystack)

        def on_item_changed(self, current, previous) -> None:
            if self._guard or current is None:
                return
            if not self.confirm_discard():
                self._guard = True
                self.list.setCurrentItem(previous)
                self._guard = False
                return
            self.load_entry(self.entries[current.data(ROLE)])

        # ---- loading one entry ----------------------------------------------
        def load_entry(self, entry: Optional[ParsedEntry]) -> None:
            self.current = entry
            self.txt_path = None
            self.txt_raw = None
            self.art_editable = True
            self.form.inc_override = None
            self.form.extra_note = ""
            enabled = entry is not None
            for widget in (self.form, self.save_button, self.revert_button, self.delete_button):
                widget.setEnabled(enabled)
            if entry is None:
                self.form.letter = self.form.stem = ""
                self.form.fill(LogoSpec(colors=[ColorSpec("named", "FF_COLOR_FG_BLUE"),
                                                ColorSpec("named", "FF_COLOR_FG_WHITE")]), None, "")
                self.form.extra_note = ("Select a folder letter and then a logo to edit." if self.get_root()
                                        else "Select a valid fastfetch source folder first.")
                self.form.refresh()
                self.dirty = False
                return

            root = self.get_root()
            notes = []
            txts = txt_files_for_macro(root, entry.macro)
            spec = replace(entry.spec, letter=entry.letter, stem=entry.macro.lower(), art="")
            source = ""
            if txts:
                self.txt_path = txts[0]
                self.txt_raw = self.txt_path.read_bytes()
                spec.stem = self.txt_path.name.split(".")[0]
                source = str(self.txt_path.relative_to(root))
                try:
                    spec.art = self.txt_raw.decode("utf-8").replace("\r\n", "\n")
                except UnicodeDecodeError:
                    self.art_editable = False
                    notes.append("The logo file is not valid UTF-8, so its text is not shown and cannot be edited here.")
                if self.txt_path.parent.name != entry.letter:
                    notes.append(f"The logo text is stored in folder '{self.txt_path.parent.name}', "
                                 f"while this entry is listed in {entry.letter}.inc.")
            else:
                notes.append("No logo .txt file exists for this entry, so it is inactive. "
                             f"Saving a logo text creates {LOGO_REL.as_posix()}/{entry.letter}/{spec.stem}.txt.")
            others = [l for l, m, _ in scan_entries(root) if m == entry.macro]
            if entry.letter in others:
                others.remove(entry.letter)
            if others:
                notes.append("The same logo text is also used by entries in "
                             + ", ".join(f"{o}.inc" for o in others) + ".")
            if entry.problems:
                notes.append("This entry contains something the editor cannot preserve ("
                             + "; ".join(entry.problems[:2])
                             + "). Its details are read-only; you can still edit the logo text or delete it.")
                self.form.inc_override = entry.block
            elif not entry.exact:
                notes.append("This entry is formatted differently from the editor's layout; "
                             "it is only rewritten if you change its details.")

            self.form.letter, self.form.stem = entry.letter, spec.stem
            self.form.extra_note = "\n".join(notes)
            self.form.fill(spec, self.txt_raw, source)
            self.form.options_box.setEnabled(not entry.problems)
            self.form.art_edit.setReadOnly(not self.art_editable)
            self.form.load_button.setEnabled(self.art_editable)
            self.dirty = False

        def mark_dirty(self) -> None:
            if self.current is not None:
                self.dirty = True

        def revert(self) -> None:
            if self.current is not None:
                self.populate(self._letter, self.current.macro)

        # ---- save / delete --------------------------------------------------
        def save(self) -> None:
            entry, root = self.current, self.get_root()
            if entry is None or root is None:
                return
            spec = self.form.collect()
            spec.order = list(entry.spec.order)
            if entry.problems:
                spec.order = []
                errors = [] if spec.art.strip() else ["The logo text is empty."]
                warnings = []
            else:
                errors, warnings = validate_edit(spec, root)
            if errors:
                QMessageBox.critical(self, "Cannot save the logo", "\n".join(f"- {e}" for e in errors))
                return

            write_entry = not entry.problems and spec_key(spec) != spec_key(entry.spec)
            art = art_to_bytes(spec.art, self.form.loaded_raw)
            write_art = self.art_editable and art != self.txt_raw
            if not write_entry and not write_art:
                QMessageBox.information(self, "Nothing to save", "The logo was not changed.")
                self.dirty = False
                return

            confirmations = list(warnings)
            others = [l for l, m, _ in scan_entries(root) if m == entry.macro]
            if entry.letter in others:
                others.remove(entry.letter)
            if write_art and others:
                confirmations.append("The logo text is shared with entries in "
                                     + ", ".join(f"{o}.inc" for o in others) + "; they will change too.")
            if confirmations and not ask(
                    self, "Please confirm",
                    "\n".join(f"- {c}" for c in confirmations) + "\n\nContinue?"):
                return
            try:
                changed = update_logo(root, spec, art, self.txt_path, write_entry, write_art)
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Writing failed", str(exc))
                return
            QMessageBox.information(
                self, "Logo saved",
                "Changed files:\n" + "\n".join(f"- {p.relative_to(root)}" for p in changed)
                + "\n\nRebuild fastfetch to use the changes.")
            self.populate(self._letter, entry.macro)

        def delete(self) -> None:
            entry, root = self.current, self.get_root()
            if entry is None or root is None:
                return
            txts, others = delete_plan(root, entry.letter, entry.macro)
            lines = [f"Remove the {entry.macro} entry from {entry.letter}.inc."]
            lines += [f"Delete {t.relative_to(root)}." for t in txts]
            if others:
                lines.append("Keep the logo text: it is still used by "
                             + ", ".join(f"{o}.inc" for o in others) + ".")
            if not ask(self, "Delete logo", "\n".join(lines) + "\n\nThis cannot be undone. Delete it?"):
                return
            try:
                delete_logo(root, entry.letter, entry.macro)
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Deleting failed", str(exc))
                self.populate(self._letter)
                return
            self.refresh_counts()
            self.populate(self._letter)

    class PresetsPage(QWidget):
        """The "Presets" tab: pick and edit a preset, preview it, install it as config.jsonc."""

        def __init__(self, get_root):
            super().__init__()
            self.get_root = get_root
            self.presets: list[Preset] = []
            self.current: Optional[Preset] = None
            self.saved_text = ""                  # text of the current preset on disk
            self.crlf = False                     # the file on disk uses Windows line endings
            self.dirty = False
            self._guard = False
            self._stale = True                    # the preview does not show the current text yet
            self._rerun = False                   # run fastfetch again when the current run ends
            self._timed_out = False
            self._binary_auto = True              # the binary path was filled in automatically
            self._process: Optional[QProcess] = None
            self._apply_process: Optional[QProcess] = None
            self._tmp_config: Optional[str] = None
            mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

            # ---- left: the preset list and the preset text -----------------------
            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText("Filter the presets...")
            self.filter_edit.setClearButtonEnabled(True)
            self.list = QListWidget()
            list_box = QGroupBox("Presets")
            list_layout = QVBoxLayout(list_box)
            list_layout.addWidget(self.filter_edit)
            list_layout.addWidget(self.list, 1)

            self.editor = QPlainTextEdit()
            self.editor.setFont(mono)
            self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.json_status = QLabel()
            self.json_status.setWordWrap(True)
            self.save_button = QPushButton("Save changes")
            self.save_as_button = QPushButton("Save as...")
            self.revert_button = QPushButton("Revert")
            edit_buttons = QHBoxLayout()
            for button in (self.save_button, self.save_as_button, self.revert_button):
                edit_buttons.addWidget(button)
            edit_box = QGroupBox("Preset text (JSONC)")
            edit_layout = QVBoxLayout(edit_box)
            edit_layout.addWidget(self.editor, 1)
            edit_layout.addWidget(self.json_status)
            edit_layout.addLayout(edit_buttons)

            left = QSplitter(Qt.Orientation.Vertical)
            left.addWidget(list_box)
            left.addWidget(edit_box)
            left.setStretchFactor(0, 2)
            left.setStretchFactor(1, 5)

            # ---- right: the fastfetch preview ------------------------------------
            self.binary_edit = QLineEdit()
            self.binary_edit.setPlaceholderText("Path to the fastfetch binary")
            self.binary_button = QPushButton("Browse...")
            binary_row = QHBoxLayout()
            binary_row.addWidget(QLabel("Fastfetch binary"))
            binary_row.addWidget(self.binary_edit, 1)
            binary_row.addWidget(self.binary_button)

            self.refresh_button = QPushButton("Refresh preview")
            self.live_check = QCheckBox("Update while typing")
            self.status_label = QLabel()
            self.status_label.setWordWrap(True)
            self.status_label.setStyleSheet("color:#888;")
            control_row = QHBoxLayout()
            control_row.addWidget(self.refresh_button)
            control_row.addWidget(self.live_check)
            control_row.addWidget(self.status_label, 1)

            preview_font = QFont(mono)
            if preview_font.pointSizeF() > 0:
                preview_font.setPointSizeF(max(1.0, preview_font.pointSizeF() - PREVIEW_FONT_SHRINK))
            self.preview = QTextBrowser()
            self.preview.setFont(preview_font)
            self.preview.setStyleSheet(f"background:{PREVIEW_BACKGROUND}; color:{DEFAULT_HEX};")
            self.preview.setPlaceholderText("The output of fastfetch is shown here.")
            preview_box = QGroupBox("Preview (fastfetch output)")
            preview_layout = QVBoxLayout(preview_box)
            preview_layout.addLayout(binary_row)
            preview_layout.addLayout(control_row)
            preview_layout.addWidget(self.preview, 1)

            splitter = QSplitter()
            splitter.addWidget(left)
            splitter.addWidget(preview_box)
            splitter.setStretchFactor(0, 4)
            splitter.setStretchFactor(1, 6)
            splitter.setSizes([560, 820])

            # ---- bottom: the three apply buttons ---------------------------------
            self.apply_buttons: list[QPushButton] = []
            apply_box = QGroupBox(f"Install the preset text above as {CONFIG_NAME}")
            apply_layout = QHBoxLayout(apply_box)
            for target in apply_targets():
                button = QPushButton(target.label)
                button.setMinimumHeight(34)
                button.setToolTip(target.tooltip)
                button.clicked.connect(lambda _checked=False, key=target.key: self.apply(key))
                apply_layout.addWidget(button, 1)
                self.apply_buttons.append(button)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(splitter, 1)
            layout.addWidget(apply_box)

            self.live_timer = QTimer(self)
            self.live_timer.setSingleShot(True)
            self.live_timer.setInterval(700)
            self.live_timer.timeout.connect(self.refresh_preview)
            self.run_timer = QTimer(self)
            self.run_timer.setSingleShot(True)
            self.run_timer.setInterval(PREVIEW_TIMEOUT_MS)
            self.run_timer.timeout.connect(self.on_run_timeout)

            self.filter_edit.textChanged.connect(self.apply_filter)
            self.list.currentItemChanged.connect(self.on_item_changed)
            self.editor.textChanged.connect(self.on_text_changed)
            self.save_button.clicked.connect(self.save)
            self.save_as_button.clicked.connect(self.save_as)
            self.revert_button.clicked.connect(self.revert)
            self.binary_edit.textEdited.connect(self.on_binary_edited)
            self.binary_button.clicked.connect(self.choose_binary)
            self.refresh_button.clicked.connect(self.refresh_preview)
            self.set_editing_enabled(False)

        # ---- list handling -------------------------------------------------------
        def root_changed(self) -> None:
            root = self.get_root()
            if self._binary_auto:
                self.binary_edit.setText(find_fastfetch(root))
            self.dirty = False
            self.populate("satellaos")

        def populate(self, select_name: Optional[str] = None) -> None:
            """Reload the list from disk and select `select_name` (or the first preset)."""
            root = self.get_root()
            self.presets = scan_presets(root) if root is not None else []
            self._guard = True
            try:
                self.list.clear()
                for index, preset in enumerate(self.presets):
                    item = QListWidgetItem(preset.name)
                    item.setData(ROLE, index)
                    item.setToolTip(str(preset.path.relative_to(root)))
                    self.list.addItem(item)
            finally:
                self._guard = False
            self.apply_filter()
            self.load_preset(None)
            if self.presets:
                wanted = next((i for i, p in enumerate(self.presets) if p.name == select_name), 0)
                self.list.setCurrentRow(wanted)

        def apply_filter(self) -> None:
            needle = self.filter_edit.text().strip().lower()
            for row in range(self.list.count()):
                item = self.list.item(row)
                item.setHidden(bool(needle) and needle not in self.presets[item.data(ROLE)].name.lower())

        def confirm_discard(self) -> bool:
            return (not self.dirty) or ask(
                self, "Unsaved changes", "The preset text has unsaved changes. Discard them?")

        def on_item_changed(self, current, previous) -> None:
            if self._guard or current is None:
                return
            if not self.confirm_discard():
                self._guard = True
                self.list.setCurrentItem(previous)
                self._guard = False
                return
            self.load_preset(self.presets[current.data(ROLE)])

        # ---- loading, editing, saving --------------------------------------------
        def set_editing_enabled(self, enabled: bool) -> None:
            self.editor.setReadOnly(not enabled)
            for button in (self.save_button, self.save_as_button, self.revert_button):
                button.setEnabled(enabled)
            self.set_apply_enabled(enabled)

        def set_apply_enabled(self, enabled: bool) -> None:
            for button in self.apply_buttons:
                button.setEnabled(enabled)

        def load_preset(self, preset: Optional[Preset]) -> None:
            self.current = preset
            text = ""
            if preset is not None:
                try:
                    text = preset.path.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    QMessageBox.critical(self, "Cannot read the preset", f"{preset.path.name}: {exc}")
                    self.current = None
            self.crlf = "\r\n" in text
            text = text.replace("\r\n", "\n")
            self.saved_text = text
            self._guard = True
            try:
                self.editor.setPlainText(text)
            finally:
                self._guard = False
            self.dirty = False
            if self.current is None:
                if self.get_root() is None:
                    note = "Select a valid fastfetch source folder first."
                elif not self.presets:
                    note = f"No presets were found in {PRESETS_REL.as_posix()}/."
                else:
                    note = "Select a preset on the list."
                self.editor.setPlaceholderText(note)
                self.preview.clear()
                self.status_label.clear()
            self.set_editing_enabled(self.current is not None)
            self.update_json_status()
            if self.current is not None:
                self.request_preview()

        def on_text_changed(self) -> None:
            if self._guard:
                return
            self.dirty = self.editor.toPlainText() != self.saved_text
            self.update_json_status()
            if self.live_check.isChecked():
                self.live_timer.start()

        def update_json_status(self) -> None:
            if self.current is None:
                self.json_status.clear()
                return
            error = jsonc_error(self.editor.toPlainText())
            text, color = ("Valid JSONC", "#4e9a06") if error is None else (f"Invalid JSONC - {error}", "#cc0000")
            if self.dirty:
                text += "   (modified, not saved)"
            self.json_status.setText(text)
            self.json_status.setStyleSheet(f"color:{color};")

        def confirm_invalid(self, text: str, verb: str) -> bool:
            error = jsonc_error(text)
            return error is None or ask(
                self, "Invalid JSONC",
                f"The preset text is not valid JSONC ({error}).\n\n{verb} it anyway?")

        def write_preset(self, path: Path, text: str, crlf: bool) -> bool:
            data = text.replace("\n", "\r\n") if crlf else text
            try:
                path.write_bytes(data.encode("utf-8"))
            except OSError as exc:
                QMessageBox.critical(self, "Writing failed", str(exc))
                return False
            return True

        def save(self) -> None:
            root = self.get_root()
            if self.current is None or root is None:
                return
            text = self.editor.toPlainText()
            if text == self.saved_text:
                QMessageBox.information(self, "Nothing to save", "The preset was not changed.")
                return
            if not self.confirm_invalid(text, "Save") or not self.write_preset(self.current.path, text, self.crlf):
                return
            self.saved_text = text
            self.dirty = False
            self.update_json_status()
            QMessageBox.information(
                self, "Preset saved", f"Saved {self.current.path.relative_to(root)}")

        def save_as(self) -> None:
            root = self.get_root()
            if self.current is None or root is None:
                return
            name, ok = QInputDialog.getText(
                self, "Save preset as", "Name of the new preset (letters, digits, '_' and '-'):")
            if not ok:
                return
            name = name.strip()
            if name.lower().endswith(".jsonc"):
                name = name[:-len(".jsonc")]
            if not PRESET_NAME_RE.fullmatch(name):
                QMessageBox.critical(
                    self, "Invalid name",
                    "Use only letters, digits, '_' and '-', and start with a letter or digit.")
                return
            path = root / PRESETS_REL / f"{name}.jsonc"
            if path.exists() and not ask(
                    self, "Overwrite?", f"{path.relative_to(root)} already exists. Overwrite it?"):
                return
            text = self.editor.toPlainText()
            if not self.confirm_invalid(text, "Save") or not self.write_preset(path, text, False):
                return
            # The new preset is now on disk; the one that was open stays as it was saved
            self.dirty = False
            self.populate(name)

        def revert(self) -> None:
            if self.current is not None:
                self.load_preset(self.current)

        # ---- preview -------------------------------------------------------------
        def on_binary_edited(self, _text: str) -> None:
            self._binary_auto = False

        def choose_binary(self) -> None:
            start = self.binary_edit.text().strip() or str(Path.cwd())
            filename, _ = QFileDialog.getOpenFileName(self, "Select the fastfetch binary", start)
            if filename:
                self.binary_edit.setText(filename)
                self._binary_auto = False
                self.request_preview()

        def request_preview(self) -> None:
            """Run fastfetch now when the tab is visible, otherwise when it is shown next."""
            if self.isVisible():
                self.refresh_preview()
            else:
                self._stale = True

        def showEvent(self, event) -> None:
            super().showEvent(event)
            if self._stale and self.current is not None:
                self.refresh_preview()

        def refresh_preview(self) -> None:
            if self._process is not None:
                self._rerun = True                  # a run is in progress: run again afterwards
                return
            binary = self.binary_edit.text().strip()
            resolved = shutil.which(binary) if binary else None
            if not resolved:
                self.preview.setPlainText(
                    "fastfetch was not found.\n\n"
                    "Build it with ./build.sh --build-binary, install fastfetch, "
                    "or choose the binary with Browse...")
                self.status_label.setText("No fastfetch binary")
                return
            self._stale = False
            self._timed_out = False
            try:
                fd, self._tmp_config = tempfile.mkstemp(prefix="fastfetch-preview-", suffix=".jsonc")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(self.editor.toPlainText())
            except OSError as exc:
                self.status_label.setText(f"Cannot write the temporary config: {exc}")
                return

            env = QProcessEnvironment.systemEnvironment()
            env.insert("COLUMNS", str(PREVIEW_COLUMNS))
            env.insert("LINES", str(PREVIEW_LINES))
            env.insert("COLORTERM", "truecolor")
            if env.value("TERM", "") in ("", "dumb"):
                env.insert("TERM", "xterm-256color")
            process = QProcess(self)
            process.setProcessEnvironment(env)
            process.finished.connect(self.on_run_finished)
            process.errorOccurred.connect(self.on_run_error)
            self._process = process
            self.status_label.setText("Running fastfetch...")
            self.run_timer.start()
            # "--pipe false" makes fastfetch print the logo and the colors although stdout is no terminal
            process.start(resolved, ["-c", self._tmp_config, "--pipe", "false"])

        def finish_run(self) -> None:
            self.run_timer.stop()
            process, self._process = self._process, None
            if process is not None:
                process.deleteLater()
            if self._tmp_config:
                try:
                    os.unlink(self._tmp_config)
                except OSError:
                    pass
                self._tmp_config = None

        def after_run(self) -> None:
            if self._rerun:
                self._rerun = False
                self.refresh_preview()

        def on_run_finished(self, exit_code: int, exit_status) -> None:
            process = self._process
            if process is None:
                return
            out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
            err = bytes(process.readAllStandardError()).decode("utf-8", "replace")
            timed_out = self._timed_out
            self.finish_run()

            shown = out if out.strip() else err
            if shown.strip():
                self.preview.setHtml(ansi_to_html(shown))
            else:
                self.preview.setPlainText("(fastfetch printed nothing)")
            problem = ""
            if timed_out:
                problem = f"fastfetch needed more than {PREVIEW_TIMEOUT_MS // 1000} s and was stopped."
            elif exit_status != QProcess.ExitStatus.NormalExit:
                problem = "fastfetch crashed."
            elif exit_code != 0:
                problem = f"fastfetch exited with code {exit_code}."
            messages = ansi_to_text(err)
            first_line = messages.splitlines()[0][:200] if messages else ""
            self.status_label.setText(" ".join(part for part in (problem, first_line) if part) or "OK")
            self.status_label.setToolTip(messages)
            self.after_run()

        def on_run_error(self, error) -> None:
            if error != QProcess.ProcessError.FailedToStart or self._process is None:
                return                              # crashes are reported by on_run_finished
            reason = self._process.errorString()
            self.finish_run()
            self.preview.clear()
            self.status_label.setText(f"Cannot start fastfetch: {reason}")
            self.after_run()

        def on_run_timeout(self) -> None:
            if self._process is not None:
                self._timed_out = True
                self._process.kill()

        # ---- applying the preset -------------------------------------------------
        def apply(self, key: str) -> None:
            if self._apply_process is not None:
                return
            target = next(t for t in apply_targets() if t.key == key)
            text = self.editor.toPlainText()
            if not text.strip():
                QMessageBox.critical(self, "Cannot apply", "The preset text is empty.")
                return
            if not self.confirm_invalid(text, "Install"):
                return
            is_root = hasattr(os, "geteuid") and os.geteuid() == 0
            escalate = target.needs_root and not is_root
            if escalate and not shutil.which("pkexec"):
                QMessageBox.critical(
                    self, "Cannot apply",
                    f"Writing to {target.directory} needs administrator rights, but pkexec "
                    "(polkit) was not found. Install polkit or start the editor as root.")
                return

            name = self.current.name if self.current else "(unsaved text)"
            config = target.directory / CONFIG_NAME
            lines = [f"Install the preset '{name}'"
                     + (" with your unsaved edits" if self.dirty else "")
                     + f" for {target.who}?",
                     "",
                     "This will run:",
                     f"    mkdir -p {target.directory}",
                     f"    install -m 644 <preset text> {config}",
                     "",
                     f"An existing {CONFIG_NAME} in that folder will be overwritten."]
            if escalate:
                lines += ["", "You will be asked for your administrator password (pkexec)."]
            if not ask(self, target.label, "\n".join(lines) + "\n\nContinue?"):
                return

            try:
                fd, tmp = tempfile.mkstemp(prefix="fastfetch-apply-", suffix=".jsonc")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text if text.endswith("\n") else text + "\n")
            except OSError as exc:
                QMessageBox.critical(self, "Cannot apply", f"Cannot write the temporary file: {exc}")
                return
            command = apply_command(target.directory, tmp, escalate)
            process = QProcess(self)
            process.finished.connect(
                lambda code, _status, p=process, t=tmp, tg=target, esc=escalate:
                self.on_apply_finished(p, t, tg, esc, code))
            process.errorOccurred.connect(
                lambda error, p=process, t=tmp:
                self.on_apply_error(p, t, error))
            self._apply_process = process
            self.set_apply_enabled(False)
            process.start(command[0], command[1:])

        def end_apply(self, process, tmp: str) -> None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._apply_process = None
            process.deleteLater()
            self.set_apply_enabled(self.current is not None)

        def on_apply_finished(self, process, tmp: str, target: ApplyTarget, escalate: bool, code: int) -> None:
            err = bytes(process.readAllStandardError()).decode("utf-8", "replace").strip()
            self.end_apply(process, tmp)
            if code == 0:
                QMessageBox.information(
                    self, "Preset installed", f"Installed {target.directory / CONFIG_NAME}\n\n{target.after}")
                return
            if escalate and code in (126, 127) and not err:
                detail = "The authentication was cancelled or failed."
            else:
                detail = err or f"The command exited with code {code}."
            QMessageBox.critical(self, "Applying failed", f"{target.label}\n\n{detail}")

        def on_apply_error(self, process, tmp: str, error) -> None:
            if error != QProcess.ProcessError.FailedToStart or process is not self._apply_process:
                return                              # other errors end in on_apply_finished
            reason = process.errorString()
            self.end_apply(process, tmp)
            QMessageBox.critical(self, "Applying failed", f"The command could not be started: {reason}")

        def shutdown(self) -> None:
            """Stop a running preview and remove its temporary file (the window is closing)."""
            self.live_timer.stop()
            self._rerun = False
            if self._process is not None:
                self._process.kill()
                self._process.waitForFinished(1000)
            self.finish_run()

    class EditorWindow(QMainWindow):
        def __init__(self, root: Optional[Path]):
            super().__init__()
            self.setWindowTitle("fastfetch-editor")
            self.resize(1380, 820)
            self.root: Optional[Path] = root

            self.root_edit = QLineEdit(str(root) if root else "")
            self.root_edit.setPlaceholderText("Path to the fastfetch source folder")
            browse_root = QPushButton("Browse...")
            self.root_status = QLabel()
            root_row = QHBoxLayout()
            root_row.addWidget(QLabel("Project folder"))
            root_row.addWidget(self.root_edit, 1)
            root_row.addWidget(browse_root)
            root_row.addWidget(self.root_status)

            self.add_page = AddPage(lambda: self.root)
            self.edit_page = EditPage(lambda: self.root)
            self.presets_page = PresetsPage(lambda: self.root)
            self.tabs = QTabWidget()
            self.tabs.addTab(self.add_page, "Add logo")
            self.tabs.addTab(self.edit_page, "Edit logo")
            self.tabs.addTab(self.presets_page, "Presets")

            central = QWidget()
            layout = QVBoxLayout(central)
            layout.addLayout(root_row)
            layout.addWidget(self.tabs, 1)
            self.setCentralWidget(central)

            browse_root.clicked.connect(self.choose_root)
            self.root_edit.textChanged.connect(self.root_changed)
            self.add_page.saved.connect(self.edit_page.reload_after_add)
            self.root_changed()

        def root_changed(self) -> None:
            path = Path(self.root_edit.text().strip()).expanduser()
            if self.root_edit.text().strip() and is_project_root(path):
                self.root = path
                self.root_status.setText("OK: fastfetch source tree found")
                self.root_status.setStyleSheet("color:#4e9a06;")
            else:
                self.root = None
                self.root_status.setText("src/logo/ascii was not found in this folder")
                self.root_status.setStyleSheet("color:#cc0000;")
            self.add_page.root_changed()
            self.edit_page.root_changed()
            self.presets_page.root_changed()

        def choose_root(self) -> None:
            start = self.root_edit.text().strip() or str(Path.cwd())
            folder = QFileDialog.getExistingDirectory(self, "Select the fastfetch source folder", start)
            if folder:
                self.root_edit.setText(folder)

        def closeEvent(self, event) -> None:
            if (self.edit_page.dirty or self.presets_page.dirty) and not ask(
                    self, "Unsaved changes",
                    "The Edit logo or the Presets tab has unsaved changes. Close anyway?"):
                event.ignore()
                return
            self.presets_page.shutdown()
            event.accept()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    return app, EditorWindow(find_project_root(argument))


def run_gui(argument: Optional[str]) -> int:
    try:
        app, window = build_gui(argument)
    except ImportError:
        print("PyQt6 is required: pip install PyQt6 (or install your distribution's python-pyqt6 package).",
              file=sys.stderr)
        return 1
    window.show()
    return app.exec()


def main() -> int:
    argument = sys.argv[1] if len(sys.argv) > 1 else None
    return run_gui(argument)


if __name__ == "__main__":
    sys.exit(main())
