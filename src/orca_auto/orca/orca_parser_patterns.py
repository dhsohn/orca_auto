"""Shared regex patterns and keyword tables for ORCA output parsing."""

from __future__ import annotations

import re

# Input line: "! B3LYP def2-TZVP Opt Freq ..." or "|  1> ! B3LYP ..."
_INPUT_LINE_RE = re.compile(r"^(?:\s*\|\s*\d+>\s*)?!\s*(.+)$", re.MULTILINE)

# Energy
_ENERGY_RE = re.compile(r"FINAL SINGLE POINT ENERGY\s+([-\d.]+)")

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
_COORD_LINE_RE = re.compile(r"^\s*([A-Z][a-z]?)\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+", re.MULTILINE)

# Vibrational frequencies
_FREQ_SECTION_RE = re.compile(
    r"VIBRATIONAL FREQUENCIES\s*\n"
    r"-+\s*\n"
    r"([\s\S]*?)(?:\n\s*\n|\n-{20,})",
)
_FREQ_VALUE_RE = re.compile(r"^\s*\d+:\s+([-\d.]+)\s+cm\*\*-1", re.MULTILINE)

# Thermodynamics
_ENTHALPY_RE = re.compile(r"Total (?:E|e)nthalpy\s*\.{3,}\s*([-\d.]+)\s*Eh")
_GIBBS_RE = re.compile(r"Final Gibbs free energy\s*\.{3,}\s*([-\d.]+)\s*Eh")

# Runtime
_RUNTIME_RE = re.compile(
    r"TOTAL RUN TIME:\s*(\d+)\s*days?\s+(\d+)\s*hours?\s+"
    r"(\d+)\s*minutes?\s+(\d+)\s*seconds?",
)

# charge / multiplicity: "* xyz 0 1" or "|  2> * xyz 0 1"
_CHARGE_MULT_RE = re.compile(r"(?:\|\s*\d+>\s*)?\*\s*xyz\s+([-\d]+)\s+(\d+)")

# Optimization cycle header
_OPT_CYCLE_RE = re.compile(r"Geometry Optimization Cycle\s+(\d+)", re.IGNORECASE)

# Convergence table items (Energy change, MAX gradient, RMS gradient, MAX step, RMS step)
_CONVERGENCE_ITEM_RE = re.compile(
    r"^\s*(Energy change|MAX gradient|RMS gradient|MAX step|RMS step)"
    r"\s+([-\d.eE+]+)\s+[-\d.eE+]+\s+(YES|NO)\s*$",
    re.MULTILINE,
)

# Normal termination marker
_NORMAL_TERMINATION_RE = re.compile(r"ORCA TERMINATED NORMALLY")
_ERROR_TERMINATION_RE = re.compile(
    r"ORCA\s+finished\s+by\s+error\s+termination|"
    r"aborting the run|"
    r"ended prematurely and may have crashed|"
    r"FATAL ERROR",
    re.IGNORECASE,
)

# Known calculation type keywords (searched in input line)
_CALC_TYPE_KEYWORDS: dict[str, str] = {
    "OPTTS": "ts",
    "SCANTS": "ts",
    "TS": "ts",
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
    "SCAN": "scan",
    "IRC": "irc",
}

# Known method keywords
_METHOD_KEYWORDS: list[str] = [
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
]

# Known basis set keywords
_BASIS_KEYWORDS: list[str] = [
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
]
