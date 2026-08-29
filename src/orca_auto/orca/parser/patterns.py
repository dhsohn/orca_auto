"""Shared regex patterns and keyword tables for ORCA output parsing."""

from __future__ import annotations

import math
import re

# Input line: "! B3LYP def2-TZVP Opt Freq ..." or "|  1> ! B3LYP ..."
_INPUT_LINE_RE = re.compile(r"^(?:\s*\|\s*\d+>\s*)?!\s*(.+)$", re.MULTILINE)

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


# Optimization convergence
_OPT_CONVERGED_RE = re.compile(r"THE OPTIMIZATION HAS CONVERGED")
_OPT_NOT_CONVERGED_RE = re.compile(
    r"ORCA GEOMETRY OPTIMIZATION.*(?:DID NOT CONVERGE|NOT CONVERGED)|"
    r"The optimization did not converge",
    re.IGNORECASE,
)

# Coordinate section (element + xyz)
_COORD_SECTION_RE = re.compile(
    r"CARTESIAN COORDINATES \(ANGSTROEM\)\s*\n"
    r"-+\s*\n"
    r"((?:\s*[A-Z][a-z]?\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s*\n)+)",
)

# Vibrational frequencies.
#
# The section body must NOT terminate on a bare blank line: real ORCA output
# puts a "Scaling factor for frequencies ..." line and blank lines between the
# header rule and the numbered frequency list, so a blank-line terminator would
# capture only the scaling-factor line and drop every frequency. Terminate on the
# section's trailing dashed rule / "NORMAL MODES" header instead (or end-of-text
# for a truncated file). Frequency values never start a line with 5+ dashes, so
# the `-{5,}` terminator cannot fire inside the list.
_FREQ_SECTION_RE = re.compile(
    r"VIBRATIONAL FREQUENCIES\s*\n"
    r"-+\s*\n"
    r"([\s\S]*?)"
    r"(?:\n[ \t]*-{5,}|\n[ \t]*NORMAL MODES|\Z)",
)
_FREQ_VALUE_RE = re.compile(r"^\s*\d+:\s+([-\d.]+)\s+cm\*\*-1", re.MULTILINE)

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

# charge / multiplicity: "* xyz 0 1", "|  2> * xyz 0 1", or the file form the
# workflow emits, "* xyzfile 0 1 input.xyz" (the trailing path is ignored).
_CHARGE_MULT_RE = re.compile(r"(?:\|\s*\d+>\s*)?\*\s*xyz(?:file)?\s+([-\d]+)\s+(\d+)")

# Optimization cycle header
_OPT_CYCLE_RE = re.compile(r"Geometry Optimization Cycle\s+(\d+)", re.IGNORECASE)

# Convergence table items (Energy change, MAX gradient, RMS gradient, MAX step, RMS step)
_CONVERGENCE_ITEM_RE = re.compile(
    r"^\s*(Energy change|MAX gradient|RMS gradient|MAX step|RMS step)"
    r"\s+([-\d.eE+]+)\s+[-\d.eE+]+\s+(YES|NO)\s*$",
    re.MULTILINE,
)

# Known calculation type keywords (searched in input line). No bare "TS" or
# "SCAN" entries: ORCA has no `! TS` keyword, and a route-level `SCAN` token is
# the SCAN density functional — relaxed scans are requested via `%geom Scan`,
# never the route line.
_CALC_TYPE_KEYWORDS: dict[str, str] = {
    "OPTTS": "ts",
    "SCANTS": "ts",
    "OPT": "opt",
    "FREQ": "freq",
    "MD": "md",
    "COPT": "opt",
    "NEB": "neb",
    "NEB-TS": "neb",
    "NEB-CI": "neb",
    "ZOOM-NEB": "neb",
    "ZOOM-NEB-TS": "neb",
    "ZOOM-NEB-CI": "neb",
    "IRC": "irc",
}

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
