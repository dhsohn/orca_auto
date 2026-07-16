"""Pins the bot's remote-admission directive tables to the scanner's key sets.

The execution-side scanner (``orca_auto.orca.input_blocks``) is the superset
authority on which ORCA directives reference external files. Remote ingress
must either validate (confine) or explicitly forbid every one of those keys;
a scanner key the bot's tables do not account for is a silent remote
confinement gap. This suite fails the moment a key is added to the scanner
without a matching remote-admission decision.
"""

from __future__ import annotations

import unittest

from orca_auto.flow.bot import remote_admission
from orca_auto.orca.input_blocks import (
    _BLOCK_FILE_REFERENCE_KEYS,
    _SIMPLE_FILE_REFERENCE_KEYS,
    _UNSUPPORTED_EXTERNAL_HOOK_KEYS,
    _UNSUPPORTED_FILE_REFERENCE_KEYS,
)

# Scanner keys the bot handles through dedicated logic instead of a key table.
# At the "%base"/"% base" position the walker validates the value as a
# contained output basename (the ``output_base`` branch of
# validate_orca_file_references); a bare "base" keyword elsewhere is admitted
# without table validation and confined only by the unconditional raw-path
# traversal sweep plus execution-side rejection.
_EXPLICITLY_SPECIAL_CASED = frozenset({"base"})


def _bot_decision(key: str) -> str | None:
    """Classify how remote admission treats one scanner-known directive key.

    Mirrors the walker's runtime precedence: the forbidden-identifier sweep
    fires on every unquoted keyword token before any validated-table branch
    can run, so a key that is both forbidden and table-listed is forbidden in
    practice (e.g. ``neb_restart_gbwname`` — its contained-output table entry
    is shadowed by the ``neb`` prefix ban).
    """

    bare = key.removeprefix("%")
    if remote_admission.remote_orca_identifier_is_forbidden(
        key
    ) or remote_admission.remote_orca_identifier_is_forbidden(bare):
        return "forbidden"
    if (
        key in remote_admission.REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
        or bare in remote_admission.REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS
    ):
        return "validated-input"
    if bare in remote_admission.REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS:
        return "validated-contained-output"
    if bare in _EXPLICITLY_SPECIAL_CASED:
        return "validated-special-case"
    return None


class RemoteAdmissionScannerKeyCoverageTest(unittest.TestCase):
    def test_every_scanner_reference_key_has_a_remote_decision(self) -> None:
        scanner_keys = (
            _SIMPLE_FILE_REFERENCE_KEYS
            | _BLOCK_FILE_REFERENCE_KEYS
            | _UNSUPPORTED_FILE_REFERENCE_KEYS
            | _UNSUPPORTED_EXTERNAL_HOOK_KEYS
        )
        undecided = sorted(key for key in scanner_keys if _bot_decision(key) is None)
        self.assertEqual(
            undecided,
            [],
            "scanner-known file-reference keys with no remote-admission decision "
            "(add each to a validated table, the forbidden set, or — with a "
            f"dedicated confinement branch — the special-case list): {undecided}",
        )

    def test_bot_validated_keys_stay_known_to_the_scanner(self) -> None:
        # The reverse direction: a bot-validated key the scanner no longer (or
        # never) knows is stale policy — either a typo that validates nothing
        # or a leftover after a scanner rename. Forbidden keys are exempt;
        # over-forbidding is safe.
        scanner_known = (
            _SIMPLE_FILE_REFERENCE_KEYS
            | _BLOCK_FILE_REFERENCE_KEYS
            | _UNSUPPORTED_EXTERNAL_HOOK_KEYS
            | _UNSUPPORTED_FILE_REFERENCE_KEYS
        )
        scanner_known_bare = {key.removeprefix("%") for key in scanner_known}
        stale = sorted(
            key
            for key in (
                remote_admission.REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
                | remote_admission.REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS
                | remote_admission.REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS
            )
            if key not in scanner_known and key.removeprefix("%") not in scanner_known_bare
        )
        self.assertEqual(
            stale,
            [],
            f"bot-validated directive keys unknown to the execution scanner: {stale}",
        )

    def test_forbidden_predicate_matches_representative_prefix_families(self) -> None:
        # The startswith families are load-bearing for coverage of the
        # scanner's neb_*/prog* keys; pin their behavior directly.
        for identifier in (
            "neb_end_xyzfile",
            "neb_restart_xyzfile",
            "neb_ts_xyzfile",
            "progscf",
            "prognmr",
            "%compound",
            "compound_file",
        ):
            self.assertTrue(
                remote_admission.remote_orca_identifier_is_forbidden(identifier),
                identifier,
            )
        # "prog" alone is a bare prefix, not a directive; it must NOT be
        # forbidden (len > len("prog") guard).
        self.assertFalse(remote_admission.remote_orca_identifier_is_forbidden("prog"))


if __name__ == "__main__":
    unittest.main()
