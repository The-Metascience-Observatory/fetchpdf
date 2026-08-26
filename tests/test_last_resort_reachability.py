"""`last_resorts_enabled()` must answer for the build it is running in.

The optional source pack ships in some copies of this repository and not
others, and a caller with a licence posture to defend has to be able to prove
which copy it has. That makes reachability a function with a contract rather
than three module attributes every caller re-interprets for itself -- and it
makes it testable here, in a file both copies keep.

Each test sets all three inputs, so it asserts the same thing whether or not
the pack is installed. The behaviour of a real installed pack is covered
beside the pack itself.
"""

from collections import namedtuple

import fetchpdf.fetchpdf as fpd

#: Only `.name` is read. A real entry carries more, but depending on its shape
#: here would couple this file to a pack it is supposed to work without.
_Source = namedtuple("_Source", "name")


class TestReachabilityIsWhatItReports:

    def test_a_build_that_registers_nothing_reaches_nothing(self, monkeypatch):
        monkeypatch.setattr(fpd, "LAST_RESORT_SOURCES", ())
        monkeypatch.setattr(fpd, "_LAST_RESORTS_ENABLED", True)
        monkeypatch.setattr(fpd, "_DISABLED_SOURCES", set())
        assert fpd.last_resorts_enabled() is False

    def test_one_source_left_on_is_enough_to_be_reachable(self, monkeypatch):
        monkeypatch.setattr(fpd, "LAST_RESORT_SOURCES", (_Source("somewhere"),))
        monkeypatch.setattr(fpd, "_LAST_RESORTS_ENABLED", True)
        monkeypatch.setattr(fpd, "_DISABLED_SOURCES", set())
        assert fpd.last_resorts_enabled() is True

    def test_disabling_every_registered_source_reaches_nothing(self, monkeypatch):
        """Named-off is as good as absent: the caller asked what can be reached,
        not what was compiled in."""
        monkeypatch.setattr(fpd, "LAST_RESORT_SOURCES",
                            (_Source("somewhere"), _Source("elsewhere")))
        monkeypatch.setattr(fpd, "_LAST_RESORTS_ENABLED", True)
        monkeypatch.setattr(fpd, "_DISABLED_SOURCES", {"somewhere", "elsewhere"})
        assert fpd.last_resorts_enabled() is False

    def test_disabling_only_some_still_leaves_the_rest_reachable(self, monkeypatch):
        """The failure this guards is answering for the wrong source: a build
        that turns one entry off and reports the whole pack unreachable would
        let the other one run behind an assertion that says it cannot."""
        monkeypatch.setattr(fpd, "LAST_RESORT_SOURCES",
                            (_Source("somewhere"), _Source("elsewhere")))
        monkeypatch.setattr(fpd, "_LAST_RESORTS_ENABLED", True)
        monkeypatch.setattr(fpd, "_DISABLED_SOURCES", {"somewhere"})
        assert fpd.last_resorts_enabled() is True

    def test_the_process_switch_outranks_a_registered_source(self, monkeypatch):
        monkeypatch.setattr(fpd, "LAST_RESORT_SOURCES", (_Source("somewhere"),))
        monkeypatch.setattr(fpd, "_LAST_RESORTS_ENABLED", False)
        monkeypatch.setattr(fpd, "_DISABLED_SOURCES", set())
        assert fpd.last_resorts_enabled() is False

    def test_it_answers_with_a_bool_and_not_a_truthy_object(self):
        """Callers assert `is False`; a generator or an int would pass `not x`
        and fail the identity check that makes the assertion worth writing."""
        assert isinstance(fpd.last_resorts_enabled(), bool)
