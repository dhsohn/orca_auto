"""The presentation-only detail kind of an ORCA input (queue ``Detail`` column)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.input_syntax import orca_route_lines
from orca_auto.orca.job_type import job_type_from_routes
from orca_auto.orca.queue_detail import QUEUE_DETAIL_KINDS, queue_detail_kind

_GEOMETRY = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"


def _kind(text: str, name: str = "calc.inp") -> str:
    # The input path is only a label: the kind comes from the supplied lines.
    return queue_detail_kind(Path("/nonexistent") / name, text.splitlines())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The confirmed defect: a method-only route is ORCA's implicit single point.
        ("! wB97X-D3BJ def2-TZVP TightSCF\n" + _GEOMETRY, "sp"),
        # Several route lines of one job are still one single point.
        ("! B3LYP\n%maxcore 1000\n! def2-SVP TightSCF\n" + _GEOMETRY, "sp"),
        # Comments never add a keyword.
        ("! B3LYP def2-SVP # IRC and TS later\n" + _GEOMETRY, "sp"),
        # %mdci configures a coupled-cluster single point; it is no %md block.
        ("! DLPNO-CCSD(T) def2-TZVP def2-TZVP/C\n%mdci\n  MaxIter 200\nend\n" + _GEOMETRY, "sp"),
        ("# ! IRC\n! B3LYP def2-SVP\n" + _GEOMETRY, "sp"),
        ("! B3LYP def2-SVP IRC\n" + _GEOMETRY, "irc"),
        ("! B3LYP def2-SVP\n%maxcore 1000\n! IRC\n" + _GEOMETRY, "irc"),
        ("! B3LYP def2-SVP NEB\n" + _GEOMETRY, "neb"),
        ("! B3LYP def2-SVP NEB-CI\n" + _GEOMETRY, "neb"),
        ("! B3LYP def2-SVP ZOOM-NEB-CI\n" + _GEOMETRY, "neb"),
    ],
    ids=[
        "method-only",
        "method-only-several-route-lines",
        "route-comment",
        "mdci-block",
        "commented-route-line",
        "irc",
        "irc-on-later-route-line",
        "neb",
        "neb-ci",
        "zoom-neb-ci",
    ],
)
def test_detail_kind_classifies_inputs_the_coarse_type_calls_other(
    text: str, expected: str
) -> None:
    lines = text.splitlines()

    # The coarse scientific label keeps its meaning for every one of these.
    assert job_type_from_routes(orca_route_lines(lines)) == "other"
    assert _kind(text) == expected
    assert expected in QUEUE_DETAIL_KINDS


@pytest.mark.parametrize(
    "text",
    [
        "! HF STO-3G MD\n%md\n  Run 10\nend\n" + _GEOMETRY,
        "! B3LYP def2-SVP EnGrad\n" + _GEOMETRY,
        "! B3LYP def2-SVP NumGrad\n" + _GEOMETRY,
        "! B3LYP def2-SVP ScanTS\n" + _GEOMETRY,
        "! XTB GOAT\n" + _GEOMETRY,
        "! B3LYP def2-SVP NEB IRC\n" + _GEOMETRY,
        "! B3LYP def2-SVP\n%geom Scan\n  B 0 1 = 0.7, 1.2, 6\nend\nend\n" + _GEOMETRY,
        "! B3LYP def2-SVP\n%method\n  RunTyp Gradient\nend\n" + _GEOMETRY,
        "! B3LYP def2-SVP\n" + _GEOMETRY + "$new_job\n! B3LYP def2-SVP\n" + _GEOMETRY,
        '%compound "steps.cmp"\n! B3LYP def2-SVP\n' + _GEOMETRY,
        "! B3LYP def2-SVP Compound\n" + _GEOMETRY,
    ],
    ids=[
        "md",
        "engrad",
        "numgrad",
        "scants",
        "goat",
        "neb-and-irc",
        "unrelaxed-scan",
        "runtyp",
        "new-job",
        "compound-block",
        "compound-route",
    ],
)
def test_detail_kind_never_calls_unsupported_or_compound_inputs_a_single_point(text: str) -> None:
    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "unknown"


_HF = "! HF STO-3G\n"
# Inline geometry after a nested %geom Constraints sub-block, whose own end
# must not close %geom early.
_NESTED_GEOM = "%geom\n  Constraints\n    { B 0 1 C }\n  end\nend\n"


@pytest.mark.parametrize(
    "block",
    [
        "%freq AnFreq true end\n",
        "%freq NumFreq true end\n",
        "%freq\n  AnFreq true\nend\n",
        "%freq\n  NumFreq true\nend\n",
        "%freq AnFreq = true end\n",
        "%FREQ anfreq TRUE END\n",
        "% freq NumFreq true end\n",
        "%freq\n  NumFreq\n  true\nend\n",
        "%freq AnFreq false NumFreq true end\n",
        "%freq AnFreq true AnFreq false end\n",
        "%freq Temp 298.15 # comment # AnFreq true end\n",
        _NESTED_GEOM + "%freq AnFreq true end\n",
        "%freq\n  Constraints\n  end\n  AnFreq true\nend\n",
        # Missing, unrecognized or quoted values and quoted keys are no "false".
        "%freq AnFreq end\n",
        "%freq NumFreq\nend\n",
        "%freq AnFreq yes end\n",
        "%freq NumFreq 1 end\n",
        "%freq AnFreq on end\n",
        '%freq AnFreq "false" end\n',
        '%freq "AnFreq" false end\n',
        # A switch the closed %freq body does not hold, or an unclosed %freq.
        "%freq end AnFreq true\n",
        "%freq Temp 298.15 end AnFreq false\n",
        "%method AnFreq true end\n",
        "%freq AnFreq false\n",
    ],
    ids=[
        "anfreq-inline",
        "numfreq-inline",
        "anfreq-multiline",
        "numfreq-multiline",
        "equals",
        "case-insensitive",
        "spaced-header",
        "value-on-next-row",
        "one-of-two-enabled",
        "duplicate-key",
        "after-closed-inline-comment",
        "after-nested-geom-block",
        "after-nested-sub-block",
        "missing-value-inline",
        "missing-value-multiline",
        "yes",
        "one",
        "on",
        "quoted-value",
        "quoted-key",
        "after-end",
        "false-after-end",
        "other-block",
        "unclosed-block",
    ],
)
def test_detail_kind_never_calls_a_frequency_switch_a_single_point(block: str) -> None:
    text = _HF + block + _GEOMETRY

    # The coarse scientific type stays "other"; only the display refuses SP.
    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "unknown"


@pytest.mark.parametrize(
    "block",
    [
        "%freq AnFreq false end\n",
        "%freq NumFreq false end\n",
        "%freq\n  AnFreq = false\n  NumFreq FALSE\nend\n",
        "%freq AnFreq false # AnFreq true\nend\n",
        "%freq # NumFreq true # AnFreq false end\n",
        "# %freq AnFreq true end\n",
        "%freq Temp 298.15 end\n",
        _NESTED_GEOM + "%freq AnFreq false end\n",
        "%freq\n  Constraints\n  end\n  NumFreq false\nend\n",
        "%scf MaxIter 500 end\n",
        "%geom MaxIter 10 # TS_search EF\nend\n",
    ],
    ids=[
        "anfreq-false",
        "numfreq-false",
        "both-false-multiline",
        "true-only-in-comment",
        "true-only-in-closed-comment",
        "commented-out-block",
        "freq-block-without-switch",
        "after-nested-geom-block",
        "after-nested-sub-block",
        "scf-block",
        "ts-search-only-in-comment",
    ],
)
def test_detail_kind_keeps_a_method_only_input_with_disabled_switches_a_single_point(
    block: str,
) -> None:
    assert _kind(_HF + block + _GEOMETRY) == "sp"


@pytest.mark.parametrize(
    "text",
    [
        _HF + "%geom TS_search EF end\n" + _GEOMETRY,
        _HF + "%geom\n  TS_Search EF\nend\n" + _GEOMETRY,
        _HF + "%geom TS_search end\n" + _GEOMETRY,
        _HF + _NESTED_GEOM + "%geom TS_search EF end\n" + _GEOMETRY,
    ],
    ids=["inline", "multiline", "missing-value", "after-nested-geom-block"],
)
def test_detail_kind_never_calls_a_geom_ts_search_a_single_point(text: str) -> None:
    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "unknown"


@pytest.mark.parametrize(
    "keyword",
    [
        # ORCA 6.1 run types besides Energy (manual, "Run Types") and aliases.
        "EnergyGrad",
        "CIM",
        "PrintThermoChem",
        "PropertiesOnly",
        "EDA",
        "NMScan",
        "NormalModeScan",
        "NMGrad",
        "NMGradient",
        "MTR",
        "MT",
        "ModeTrajectory",
    ],
)
def test_detail_kind_never_calls_another_run_type_a_single_point(keyword: str) -> None:
    text = f"! HF STO-3G {keyword}\n" + _GEOMETRY

    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "unknown"


# The admitted ESD(FLUOR) input of the execution-binding tests and the ORCA 6.1
# manual's vertical-gradient ESD(ABS) example: both run the ESD module after
# the single point (derivatives, spectra, rates).
_ESD_FLUOR = (
    "! B3LYP def2-SVP ESD(FLUOR)\n"
    '%esd\n  GSHessian "S0.hess"\n  ESHessian "S1.hess"\nend\n'
    "* xyzfile 0 1 g.xyz\n"
)
_ESD_ABS_VG = (
    "! B3LYP DEF2-SVP TIGHTSCF ESD(ABS)\n"
    "%TDDFT NROOTS 5 IROOT 1 END\n"
    '%ESD\n  GSHESSIAN "BEN.hess"\n  DOHT TRUE\n  HESSFLAG VG # DEFAULT\nEND\n'
    "* XYZFILE 0 1 BEN.xyz\n"
)


@pytest.mark.parametrize(
    "text",
    [
        _ESD_FLUOR,
        _ESD_ABS_VG,
        # Every ESD alias of the manual, in any case and spacing.
        *(
            f"! B3LYP def2-SVP {keyword}\n" + _GEOMETRY
            for keyword in (
                "ESD(ABS)",
                "ESD(FLUOR)",
                "ESD(PHOSP)",
                "ESD(ISC)",
                "ESD(IC)",
                "ESD(RR)",
                "ESD(RRAMAN)",
                "esd(abs)",
                "Esd(Fluor)",
                # Bare, spaced, unclosed and unknown forms are no plain energy either.
                "ESD",
                "ESD (ABS)",
                "ESD( ABS )",
                "ESD(ABS",
                "ESD(UNKNOWN)",
            )
        ),
        "! B3LYP def2-SVP\n%maxcore 1000\n! ESD(ABS)\n" + _GEOMETRY,
        "!ESD(FLUOR) B3LYP def2-SVP\n" + _GEOMETRY,
        # A %esd block configures the ESD module; it has no documented off form.
        _HF + "%esd HessFlag VG end\n" + _GEOMETRY,
        _HF + "%esd\n  HessFlag VG\n  DoHT true\nend\n" + _GEOMETRY,
        _HF + "% esd HessFlag VG end\n" + _GEOMETRY,
        _HF + "%ESD HESSFLAG VG END\n" + _GEOMETRY,
        _HF + "%esd end\n" + _GEOMETRY,
        _HF + "%esd\n  DoHT true\n" + _GEOMETRY,
    ],
    ids=[
        "established-fluor",
        "manual-abs-vertical-gradient",
        "abs",
        "fluor",
        "phosp",
        "isc",
        "ic",
        "rr",
        "rraman",
        "lower-case",
        "mixed-case",
        "bare",
        "spaced",
        "spaced-inside",
        "unclosed",
        "unknown",
        "later-route-line",
        "compact-route-marker",
        "esd-block-inline",
        "esd-block-multiline",
        "esd-block-spaced-header",
        "esd-block-upper-case",
        "esd-block-empty",
        "esd-block-unclosed",
    ],
)
def test_detail_kind_never_calls_an_esd_request_a_single_point(text: str) -> None:
    # The coarse scientific type stays "other"; only the display refuses SP.
    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "unknown"


@pytest.mark.parametrize(
    "text",
    [
        '! B3LYP def2-SVP "ESD(ABS)"\n' + _GEOMETRY,
        "! B3LYP def2-SVP # ESD(ABS)\n" + _GEOMETRY,
        "! B3LYP # ESD(FLUOR) # def2-SVP\n" + _GEOMETRY,
        _HF + "# %esd HessFlag VG end\n" + _GEOMETRY,
        "! B3LYP DEF2-SVP TIGHTSCF\n%TDDFT NROOTS 5 IROOT 1 END\n" + _GEOMETRY,
        "! B3LYP def2-SVP NOESD\n" + _GEOMETRY,
    ],
    ids=[
        "quoted-route-text",
        "route-comment",
        "closed-route-comment",
        "commented-esd-block",
        "tddft-only",
        "esd-inside-another-token",
    ],
)
def test_detail_kind_keeps_esd_text_that_requests_nothing_a_single_point(text: str) -> None:
    assert _kind(text) == "sp"


def test_detail_kind_calls_the_single_point_alias_a_single_point() -> None:
    # "SinglePoint" is the manual's alias of the Energy run type.
    text = "! HF STO-3G SinglePoint\n" + _GEOMETRY

    assert job_type_from_routes(orca_route_lines(text.splitlines())) == "other"
    assert _kind(text) == "sp"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("! Opt\n" + _GEOMETRY, "opt"),
        ("! Opt Freq\n" + _GEOMETRY, "opt+freq"),
        ("! OptTS Freq\n" + _GEOMETRY, "ts+freq"),
        ("! B3LYP def2-SVP OptTS Freq IRC\n" + _GEOMETRY, "ts+freq+irc"),
        ("! NEB-TS Freq IRC\n" + _GEOMETRY, "neb-ts+freq+irc"),
        ("! NEB-TS IRC\n" + _GEOMETRY, "neb-ts+irc"),
        ("! B3LYP def2-SVP OptTS IRC\n" + _GEOMETRY, "ts+irc"),
        ("! B3LYP def2-SVP OptTS\n! IRC\n" + _GEOMETRY, "ts+irc"),
        ("! NEB-TS\n" + _GEOMETRY, "neb-ts"),
        ("! ZOOM-NEB-TS Freq\n" + _GEOMETRY, "neb-ts+freq"),
        ("! B3LYP def2-SVP Freq\n" + _GEOMETRY, "freq"),
        ("! SP IRC\n" + _GEOMETRY, "unknown"),
        ("! B3LYP def2-SVP Energy\n" + _GEOMETRY, "sp"),
    ],
    ids=[
        "opt",
        "opt-freq",
        "optts-freq",
        "optts-freq-irc",
        "neb-ts-freq-irc",
        "neb-ts-irc",
        "optts-irc",
        "optts-then-irc",
        "neb-ts",
        "zoom-neb-ts-freq",
        "freq",
        "sp-irc",
        "energy",
    ],
)
def test_detail_kind_records_supported_operations_for_named_coarse_types(
    text: str, expected: str
) -> None:
    lines = text.splitlines()
    assert job_type_from_routes(orca_route_lines(lines)) != "other"
    assert _kind(text) == expected
    assert expected in QUEUE_DETAIL_KINDS


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        _GEOMETRY,
        "%pal nprocs 2 end\n" + _GEOMETRY,
        "# ! B3LYP def2-SVP\n" + _GEOMETRY,
    ],
    ids=["empty", "blank", "geometry-only", "no-route", "comment-only-route"],
)
def test_detail_kind_without_route_lines_is_no_evidence(text: str) -> None:
    assert _kind(text) == ""


@pytest.mark.parametrize("name", ["sp.inp", "irc.inp", "ts.inp", "tsp.inp", "opt_freq.inp"])
def test_detail_kind_ignores_the_input_name(name: str) -> None:
    # A file name is never operation evidence, here or in the queue table.
    assert _kind("! B3LYP def2-SVP\n" + _GEOMETRY, name) == "sp"
    assert _kind("! B3LYP def2-SVP IRC\n" + _GEOMETRY, name) == "irc"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("! HF STO-3G NumFreq\n" + _GEOMETRY, "freq"),
        ("! HF STO-3G AnFreq\n" + _GEOMETRY, "freq"),
        ("! hf sto-3g NUMFREQ\n" + _GEOMETRY, "freq"),
        ("! Opt NumFreq\n" + _GEOMETRY, "opt+freq"),
        ("! OPT anfreq\n" + _GEOMETRY, "opt+freq"),
        ("! OptTS AnFreq\n" + _GEOMETRY, "ts+freq"),
        ("! optts numfreq\n" + _GEOMETRY, "ts+freq"),
        ("! NEB-TS NumFreq IRC\n" + _GEOMETRY, "neb-ts+freq+irc"),
        ("! zoom-neb-ts anfreq irc\n" + _GEOMETRY, "neb-ts+freq+irc"),
        # Block-only frequency on method-only input stays unknown (not route aliases).
        ("! HF STO-3G\n%freq AnFreq true end\n" + _GEOMETRY, "unknown"),
    ],
    ids=[
        "route-numfreq",
        "route-anfreq",
        "route-numfreq-mixed-case",
        "opt-route-numfreq",
        "opt-route-anfreq-case",
        "optts-route-anfreq",
        "optts-route-numfreq-case",
        "neb-ts-route-numfreq-irc",
        "zoom-neb-ts-route-anfreq-irc",
        "block-anfreq-stays-unknown",
    ],
)
def test_detail_kind_treats_route_numfreq_anfreq_as_freq_not_block_switches(
    text: str, expected: str
) -> None:
    assert _kind(text) == expected
    assert expected in QUEUE_DETAIL_KINDS


@pytest.mark.parametrize(
    ("route", "label"),
    [
        ("! OptTS Freq", "TS+Freq"),
        ("! Opt Freq", "Opt+Freq"),
        ("! SP", "SP"),
        ("! IRC", "IRC"),
        ("! NEB", "NEB"),
        ("! NEB-TS", "NEB-TS"),
        ("! Freq", "Freq"),
        ("! B3LYP def2-SVP", "SP"),
        ("! OptTS IRC", "TS+IRC"),
    ],
    ids=[
        "optts-freq",
        "opt-freq",
        "sp",
        "irc",
        "neb",
        "neb-ts",
        "freq",
        "implicit-sp",
        "optts-irc",
    ],
)
def test_detail_kind_labels_match_queue_detail_text(route: str, label: str) -> None:
    from orca_auto.activity_labels import queue_detail_text
    from orca_auto.orca.app_ids import ORCA_TASK_KIND

    lines = [route, "* xyz 0 1", "H 0 0 0", "*"]
    metadata = {
        "task_kind": ORCA_TASK_KIND,
        "job_type": job_type_from_routes(orca_route_lines(lines)),
        "detail_kind": queue_detail_kind(Path("/nonexistent/job.inp"), lines),
    }
    assert queue_detail_text({"engine": "orca", "metadata": metadata}) == label
