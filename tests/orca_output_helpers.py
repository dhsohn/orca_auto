"""Synthetic ORCA inputs and outputs shared by the report tests."""

from __future__ import annotations

from pathlib import Path

OPT_CYCLES_BLOCK = """
                *** Geometry Optimization Cycle   1 ***

FINAL SINGLE POINT ENERGY      -100.00000000

                *** Geometry Optimization Cycle   2 ***

FINAL SINGLE POINT ENERGY      -100.00500000

                *** Geometry Optimization Cycle   3 ***

FINAL SINGLE POINT ENERGY      -100.00520000

                    ***********************HURRAY********************
                    ***        THE OPTIMIZATION HAS CONVERGED     ***
                    *************************************************
"""


COORDS_BLOCK = """
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  H      0.000000    0.000000    0.000000
  O      1.200000    0.000000    0.000000
  O      3.000000    0.000000    0.000000

"""


FREQ_TS_BLOCK = """
-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -410.20 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1
     8:     300.00 cm**-1

------------
NORMAL MODES
------------

                  0          1          2          3          4          5
      0       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      1       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      2       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      3       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      4       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      5       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      6       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      7       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      8       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
                  6          7          8
      0       0.800000   0.100000   0.000000
      1       0.000000   0.000000   0.100000
      2       0.000000   0.000000   0.000000
      3      -0.400000   0.200000   0.000000
      4       0.000000   0.000000   0.300000
      5       0.000000   0.000000   0.000000
      6       0.000000   0.500000   0.000000
      7       0.000000   0.000000   0.700000
      8       0.000000   0.000000   0.000000

IR SPECTRUM
"""


def write_opt_inp(path: Path, route: str) -> None:
    path.write_text(f"{route}\n\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")


def write_opt_out(path: Path, *, freq_block: str = "") -> None:
    path.write_text(
        "! Opt B3LYP def2-SVP\n"
        + COORDS_BLOCK
        + OPT_CYCLES_BLOCK
        + freq_block
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


NEB_TS_CYCLES_BLOCK = """
                *** Geometry Optimization Cycle   1 ***

FINAL SINGLE POINT ENERGY      -343.99900000

                *** Geometry Optimization Cycle   2 ***

FINAL SINGLE POINT ENERGY      -343.99864000

"""


NEB_BLOCK = """
----------------------
NEB settings
----------------------
Method type                             ....  climbing image
Tangent type                            ....  improved
Number of intermediate images           ....  8
Generation of initial path              ....  image dependent pair potential
Initial path via TS guess               ....  off

Optimization method:
Method                                  ....  L-BFGS
Max. iterations                         ....  500

Generation of  the initial path:
Writing initial trajectory to file      ....  nebts_initial_path_trj.xyz

Starting iterations:
Optim.  Iteration  HEI  E(HEI)-E(0)  max(|Fp|)   RMS(Fp)    dS
Switch-on CI threshold               0.020000
   LBFGS     0      5    0.372340    0.129615   0.029150  12.1839
   LBFGS     1      5    0.352980    0.104320   0.024586  12.1349
Image  6 will be converted to a climbing image in the next iteration (max(|Fp|) < 0.0200)
Optim.  Iteration  CI   E(CI)-E(0)   max(|Fp|)   RMS(Fp)    dS     max(|FCI|)   RMS(FCI)
Convergence thresholds               0.020000   0.010000            0.002000    0.001000
   LBFGS    49      6    0.130964    0.016940   0.003526  15.3056    0.017426    0.005167
   LBFGS    50      6    0.129316    0.045416   0.007419  15.4226    0.040132    0.010358

                    *********************H U R R A Y*********************
                    ***        THE NEB OPTIMIZATION HAS CONVERGED     ***
                    *****************************************************

                    ***********************HURRAY************************
                    ***        THE TS OPTIMIZATION HAS CONVERGED      ***
                    *****************************************************
---------------------------------------------------------------
                      PATH SUMMARY FOR NEB-TS
---------------------------------------------------------------
All forces in Eh/Bohr. Global forces for TS.
Image     E(Eh)   dE(kcal/mol)  max(|Fp|)  RMS(Fp)
  0    -344.08225     0.00       0.00014   0.00003
  1    -344.07675     3.45       0.00232   0.00071
  2    -344.07387     5.26       0.01427   0.00363
  3    -344.07067     7.27       0.00282   0.00105
  4    -344.04841    21.23       0.00130   0.00058
  5    -344.01506    42.16       0.00124   0.00056
  6    -343.99728    53.32       0.00150   0.00060 <= CI
 TS    -343.99864    52.47       0.00013   0.00005 <= TS
  7    -344.03091    32.22       0.00151   0.00061
  8    -344.06788     9.02       0.00179   0.00073
  9    -344.07676     3.45       0.00026   0.00009

"""


NEB_IRC_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

Settings:
Direction                           .... both
Storing full IRC trajectory in      .... neb_IRC_Full.xyz

----------------------
IRC PATH SUMMARY
----------------------
All gradients are in Eh/Bohr.

Step     E(Eh)        dE(kcal/mol)  max(|G|)  RMS(G)
  1    -344.015000    -11.12       0.00160   0.00080
  2    -343.998640      0.00       0.00200   0.00090 <= TS
  3    -344.020000    -14.26       0.00150   0.00070

"""


def write_neb_inp(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "! NEB-TS B3LYP def2-SVP Freq",
                "",
                "%neb",
                '  neb_end_xyzfile "product.xyz"',
                "end",
                "",
                "* xyzfile 0 1 reactant.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_neb_out(path: Path) -> None:
    path.write_text(
        "! NEB-TS B3LYP def2-SVP Freq\n"
        + COORDS_BLOCK
        + NEB_BLOCK
        + NEB_TS_CYCLES_BLOCK
        + FREQ_TS_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def write_neb_irc_out(path: Path) -> None:
    path.write_text(
        "! NEB-TS B3LYP def2-SVP Freq IRC\n"
        + COORDS_BLOCK
        + NEB_BLOCK
        + NEB_TS_CYCLES_BLOCK
        + FREQ_TS_BLOCK
        + NEB_IRC_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


SCAN_FREQ_BLOCK = """
-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

Scaling factor for frequencies =  1.000000000  (already applied!)

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -155.30 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1
     8:     300.00 cm**-1
"""


SCAN_MODES_BLOCK = """
------------
NORMAL MODES
------------

These modes are the Cartesian displacements weighted by the diagonal matrix
M(i,i)=1/sqrt(m[i]) where m[i] is the mass of the displaced atom
Thus, these vectors are normalized but *not* orthogonal

                  0          1          2          3          4          5
      0       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      1       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      2       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      3       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      4       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      5       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      6       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      7       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      8       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
                  6          7          8
      0       0.900000   0.100000   0.000000
      1       0.000000   0.000000   0.100000
      2       0.000000   0.000000   0.000000
      3      -0.300000   0.200000   0.000000
      4       0.000000   0.000000   0.300000
      5       0.000000   0.000000   0.000000
      6       0.000000   0.500000   0.000000
      7       0.000000   0.000000   0.700000
      8       0.000000   0.000000   0.000000

IR SPECTRUM
"""


SCAN_SURFACE_BLOCK = """
RELAXED SURFACE SCAN RESULTS

The Calculated Surface using the 'Actual Energy'
   1.86000000 -100.00000000
   1.91000000 -99.99000000
   1.96000000 -100.02000000

The Calculated Surface using the SCF energy
   1.86000000 -101.00000000
"""


def write_scan_out(path: Path) -> None:
    path.write_text(
        COORDS_BLOCK
        + SCAN_SURFACE_BLOCK
        + SCAN_FREQ_BLOCK
        + SCAN_MODES_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def write_scan_inp(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "! Opt B3LYP def2-SVP Freq",
                "",
                "%geom",
                "  Scan",
                "    B 0 1 = 1.86, 1.96, 3",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


IRC_COORDS_BLOCK = """
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  C      0.000000    0.000000    0.000000
  H      0.000000    0.000000    1.089000

"""


IRC_FREQ_BLOCK = """
VIBRATIONAL FREQUENCIES
-----------------------

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -500.00 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1

NORMAL MODES
------------

                  6          7
      0       0.700000   0.100000
      1       0.000000   0.000000
      2       0.000000   0.000000
      3      -0.500000   0.000000
      4       0.000000   0.200000
      5       0.000000   0.100000

IR SPECTRUM
"""


IRC_OPT_BLOCK = """
----------------------------
GEOMETRY OPTIMIZATION CYCLE   1
----------------------------
FINAL SINGLE POINT ENERGY     -343.950000000000

----------------------------
GEOMETRY OPTIMIZATION CYCLE   2
----------------------------
FINAL SINGLE POINT ENERGY     -343.997280000000

THE OPTIMIZATION HAS CONVERGED
"""


# Mirrors the real ORCA 6 IRC driver output: a banner (no "IRC settings"
# header), dotted settings with periods inside labels and leaders butting the
# label, asterisk-boxed direction banners, and iteration rows without a
# separate step column.
IRC_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

System:
Nr. of atoms                        .... 2
Algorithm: SD (steepest descent step) plus correction
Settings:
Max. no of cycles        MaxIter    .... 30
Direction                           .... Forward and backward
Initial displacement type           .... Energy
  Initial displacement energy change.... 2.000 mEh
Convergence Tolerances:
  Max. Gradient            TolMAXG  ....  2.0000e-03 Eh/bohr
Storing full IRC trajectory in      .... job_IRC_Full_trj.xyz
Storing forward trajectory in       .... job_IRC_F_trj.xyz
Storing backward trajectory in      .... job_IRC_B_trj.xyz

         *************************************************************
         *                          FORWARD IRC                      *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds                0.002000  0.000500
    0     -343.997280    0.000000    0.002000  0.000900
    1     -344.020000  -14.257000    0.001500  0.000700
    2     -344.050000  -33.081000    0.001000  0.000500

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

         *************************************************************
         *                          BACKWARD IRC                     *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds                0.002000  0.000500
    0     -344.015000  -11.123000    0.001600  0.000800
    1     -344.045000  -29.947000    0.001100  0.000550

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

---------------------------------------------------------------
                       IRC PATH SUMMARY
---------------------------------------------------------------
All gradients are in Eh/Bohr.

Step        E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
   1     -344.045000   -29.947000    0.001100  0.000550
   2     -344.015000   -11.123000    0.001600  0.000800
   3     -343.997280     0.000000    0.000200  0.000033 <= TS
   4     -344.020000   -14.257000    0.001500  0.000700
   5     -344.050000   -33.081000    0.001000  0.000500

"""


# Trimmed from a real ORCA 6.1 IRC run with %irc Monitor_Internals: every
# iteration and path-summary row carries one trailing column per monitored
# internal coordinate, after RMS(G) and before the "<= TS" marker.
IRC_MONITOR_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

         *************************************************************
         *                          FORWARD IRC                      *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)  B(O 58,P 20)
Convergence thresholds                0.002000  0.000500
    0     -1613.778811    0.014614    0.008381  0.001008      3.51
    1     -1613.778906   -0.044911    0.001173  0.000128      3.51

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

         *************************************************************
         *                          BACKWARD IRC                     *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)  B(O 58,P 20)
Convergence thresholds                0.002000  0.000500
    0     -1613.778832    0.001343    0.007482  0.001045      3.21
    1     -1613.778931   -0.061075    0.001248  0.000141      3.21

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

---------------------------------------------------------------
                       IRC PATH SUMMARY
---------------------------------------------------------------
All gradients are in Eh/Bohr.

Step        E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)  B(O 58,P 20)
   1     -1613.778931   -0.061075    0.001248  0.000141      3.21
   2     -1613.778832    0.001343    0.007482  0.001045      3.21
   3     -1613.778834    0.000000    0.000003  0.000001      3.36    <= TS
   4     -1613.778811    0.014614    0.008381  0.001008      3.51
   5     -1613.778906   -0.044911    0.001173  0.000128      3.51

"""


def write_irc_inp(path: Path, route: str) -> None:
    path.write_text(f"{route}\n* xyz 0 1\nC 0 0 0\nH 0 0 1\n*\n", encoding="utf-8")


def write_irc_out(
    path: Path,
    *,
    route: str,
    irc_block: str = IRC_BLOCK,
    freq: bool = False,
    opt: bool = False,
) -> None:
    path.write_text(
        "\n".join(
            [
                "                                 Program Version 6.0.1 -  RELEASE  -",
                f"|  1> {route}",
                "|  2> * xyz 0 1",
                "|  3> C 0.0 0.0 0.0",
                "|  4> *",
                IRC_COORDS_BLOCK,
                IRC_OPT_BLOCK if opt else "",
                "FINAL SINGLE POINT ENERGY     -343.997280000000",
                IRC_FREQ_BLOCK if freq else "",
                irc_block,
                "                             ****ORCA TERMINATED NORMALLY****",
                "TOTAL RUN TIME: 0 days 0 hours 12 minutes 0 seconds 0 msec",
            ]
        ),
        encoding="utf-8",
    )


def sp_out_text(
    *,
    route: str = "wB97M-V def2-TZVPP CPCM(toluene)",
    energy: float = -1234.567890123456,
    freq_block: bool = False,
    thermo: bool = False,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        "|  2> * xyz 0 1",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.000000    1.234567   -0.987654",
        "  H      0.123456   -0.654321    2.000000",
        "",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
    ]
    if freq_block:
        lines += ["", "VIBRATIONAL FREQUENCIES", "-----------------------", ""]
        lines += ["     0:      -80.50 cm**-1 ***imaginary mode***", "     1:      120.00 cm**-1"]
        lines += [
            "",
            "NORMAL MODES",
            "------------",
            "",
            "                  0          1",
            "      0       0.700000   0.100000",
            "      1       0.100000   0.000000",
            "      2       0.000000   0.000000",
            "      3       0.500000   0.000000",
            "      4       0.000000   0.200000",
            "      5       0.000000   0.100000",
        ]
    if thermo:
        lines += [
            "--------------------------",
            "THERMOCHEMISTRY AT 298.15K",
            "--------------------------",
            "Zero point energy                ...      0.08843782 Eh",
            "Total enthalpy                   ...  -1234.40000000 Eh",
            "Final Gibbs free energy          ...  -1234.45000000 Eh",
            "G-E(el)                          ...      0.11789012 Eh",
        ]
    lines += [
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def frequency_section(freqs: tuple[float, ...]) -> list[str]:
    return [
        "-----------------------",
        "VIBRATIONAL FREQUENCIES",
        "-----------------------",
        *(f"{index:4d}:   {freq:10.2f} cm**-1" for index, freq in enumerate(freqs)),
        "",
    ]


def si_out_text(
    *,
    route: str = "wB97X-D3 def2-TZVP CPCM(toluene) OptTS Freq",
    energy: float = -1234.567890123456,
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        "|  2> * xyz 0 1",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.000000    1.234567   -0.987654",
        "  H      0.123456   -0.654321    2.000000",
        "",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
        "THE OPTIMIZATION HAS CONVERGED",
    ]
    if freqs:
        lines += ["", "VIBRATIONAL FREQUENCIES", "-----------------------", ""]
        lines += [f"{index:6d}: {value:12.2f} cm**-1" for index, value in enumerate(freqs)]
        lines += [
            "",
            "NORMAL MODES",
            "------------",
            "",
            "                  0          1",
            "      0       0.700000   0.100000",
            "      1       0.100000   0.000000",
            "      2       0.000000   0.000000",
            "      3       0.500000   0.000000",
            "      4       0.000000   0.200000",
            "      5       0.000000   0.100000",
        ]
    if thermo:
        lines += [
            "--------------------------",
            "THERMOCHEMISTRY AT 298.15K",
            "--------------------------",
            "Zero point energy                ...      0.08843782 Eh",
            "Total enthalpy                   ...  -1234.40000000 Eh",
            "Final Gibbs free energy          ...  -1234.45000000 Eh",
            "G-E(el)                          ...      0.11789012 Eh",
        ]
    lines += [
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)
