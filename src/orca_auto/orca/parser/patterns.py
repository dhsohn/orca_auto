"""Shared regex patterns and keyword tables for ORCA output parsing."""

from __future__ import annotations

import math
import re

# Input line: "! B3LYP def2-TZVP Opt Freq ..." or "|  1> ! B3LYP ...". A route
# line starts with exactly one "!": ORCA's error banners ("!!!!!!!!" rules and
# "!!! FATAL ERROR ENCOUNTERED !!!") start with two or more and must not be
# read as a route.
_INPUT_LINE_RE = re.compile(r"^(?:\s*\|\s*\d+>\s*)?!(?!!)\s*(.+)$", re.MULTILINE)

# Energy. ORCA prints the total energy on its own line; near-converged SCF
# runs append a parenthesized annotation such as "(SCF not fully converged!)"
# on that same line, so the pattern captures it as group 2 for consumers that
# must distinguish annotated values. The value group accepts the full
# floating-point syntax including Fortran D exponents, and the line anchors
# reject the phrase embedded mid-line as well as malformed number fragments
# that float() would crash on.
FINAL_SINGLE_POINT_ENERGY_PATTERN = (
    r"(?m)^[ \t]*FINAL SINGLE POINT ENERGY[ \t]+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?)"
    r"[ \t]*(\([^)\r\n]*\))?[ \t]*\r?$"
)
FINAL_SINGLE_POINT_ENERGY_RE = re.compile(FINAL_SINGLE_POINT_ENERGY_PATTERN)
FINAL_SINGLE_POINT_ENERGY_BYTES_RE = re.compile(FINAL_SINGLE_POINT_ENERGY_PATTERN.encode("ascii"))


def final_single_point_energy_value(raw: str | bytes) -> float:
    """Parse one captured energy value, accepting Fortran D exponents.

    Raises ValueError for a value that is not finite, so every consumer
    shares the same rejection of overflowed exponent forms.
    """
    text = raw.decode("ascii") if isinstance(raw, (bytes, bytearray)) else raw
    value = float(text.replace("D", "E").replace("d", "e"))
    if not math.isfinite(value):
        raise ValueError(f"non-finite ORCA energy value: {text!r}")
    return value


# Coordinate section (element + xyz)
_COORD_SECTION_RE = re.compile(
    r"CARTESIAN COORDINATES \(ANGSTROEM\)\s*\n"
    r"-+\s*\n"
    r"((?:\s*[A-Z][a-z]?\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s*\n)+)",
)

# Thermodynamics
_ENTHALPY_RE = re.compile(r"Total (?:E|e)nthalpy\s*\.{3,}\s*([-\d.]+)\s*Eh")
_GIBBS_RE = re.compile(r"Final Gibbs free energy\s*\.{3,}\s*([-\d.]+)\s*Eh")
_ZPE_RE = re.compile(r"Zero point energy\s*\.{3,}\s*([-\d.]+)\s*Eh")
_GIBBS_CORRECTION_RE = re.compile(r"G-E\(el\)\s*\.{3,}\s*([-\d.]+)\s*Eh")
_THERMO_TEMPERATURE_RE = re.compile(r"THERMOCHEMISTRY AT\s+([\d.]+)\s*K")

# Program header: "Program Version 5.0.4 -  RELEASE  -"
_PROGRAM_VERSION_RE = re.compile(r"Program Version\s+([\w.]+)")

# Coordinate line including the xyz values (Å)
_COORD_XYZ_LINE_RE = re.compile(
    r"^\s*([A-Z][a-z]?)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)",
    re.MULTILINE,
)

# Implicit solvation: CPCM(...) route token, SMD flags in the echoed %cpcm block
_CPCM_TOKEN_RE = re.compile(r"^CPCM(?:\(([^)]*)\))?$", re.IGNORECASE)
_SMD_TRUE_RE = re.compile(r"\bsmd\s+true\b", re.IGNORECASE)
_SMD_SOLVENT_RE = re.compile(r'\bsmdsolvent\s+"([^"]+)"', re.IGNORECASE)

# Runtime
_RUNTIME_RE = re.compile(
    r"TOTAL RUN TIME:\s*(\d+)\s*days?\s+(\d+)\s*hours?\s+"
    r"(\d+)\s*minutes?\s+(\d+)\s*seconds?",
)

# charge / multiplicity: inline, echoed, or file-form geometry directives
# such as "* xyzfile 0 1 input.xyz" (the trailing path is ignored).
_CHARGE_MULT_RE = re.compile(
    r"^[ \t]*(?:\|[ \t]*\d+>[ \t]*)?\*[ \t]+xyz(?:file)?[ \t]+(-?\d+)[ \t]+(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Optimization cycle header
_OPT_CYCLE_RE = re.compile(r"Geometry Optimization Cycle\s+(\d+)", re.IGNORECASE)

# Known method keywords
_METHOD_KEYWORDS: tuple[str, ...] = (
    "CCSD(T)",
    "CCSD",
    "MP2",
    "RI-MP2",
    "DLPNO-CCSD(T)",
    "B3LYP",
    "PBE0",
    "PBE",
    "BP86",
    "TPSS",
    "M06-2X",
    "M06",
    "ωB97X-D3",
    "wB97X-D3",
    "ωB97X-D",
    "wB97X-D",
    "ωB97X",
    "wB97X",
    "ωB97M-V",
    "wB97M-V",
    "ωB97M-D4",
    "wB97M-D4",
    "B2PLYP",
    "REVPBE",
    "BLYP",
    "CAM-B3LYP",
    "LC-BLYP",
    "BHandHLYP",
    "HF",
    "RHF",
    "UHF",
    "ROHF",
    "CASSCF",
    "NEVPT2",
    "MRCI",
    "B97-3c",
    "r2SCAN-3c",
    "PBEh-3c",
)

# Known basis set keywords
_BASIS_KEYWORDS: tuple[str, ...] = (
    "def2-QZVPP",
    "def2-QZVP",
    "def2-TZVPP",
    "def2-TZVP",
    "def2-SVP",
    "def2-SV(P)",
    "ma-def2-TZVPP",
    "ma-def2-TZVP",
    "ma-def2-SVP",
    "cc-pVQZ",
    "cc-pVTZ",
    "cc-pVDZ",
    "aug-cc-pVQZ",
    "aug-cc-pVTZ",
    "aug-cc-pVDZ",
    "6-311++G(d,p)",
    "6-311+G(d,p)",
    "6-311G(d,p)",
    "6-311++G(d)",
    "6-311+G(d)",
    "6-311G(d)",
    "6-31++G(d,p)",
    "6-31+G(d,p)",
    "6-31G(d,p)",
    "6-31++G(d)",
    "6-31+G(d)",
    "6-31G(d)",
    "6-31G*",
    "6-31G**",
    "STO-3G",
)
