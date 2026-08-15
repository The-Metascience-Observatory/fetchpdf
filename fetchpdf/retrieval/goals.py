"""What a single walk of the ladder is trying to collect.

The tier walk was winner-take-all: best format, write it, stop. That is right when
a consumer reads one artifact, and wrong for the ones that read two.
agent_for_forensic_metascience takes (body_md, main_pdf, si_pdfs) -- it needs the
PDF for supplements, figures and the rendered page -- while the reason to want XML
alongside it is that the glyph-corruption class its tests quarantine papers over
(broken ToUnicode CMaps mapping '-' to '2' and '=' to '5') is a PDF-only disease
that markup does not have.

So a walk now fills a set of GOALS rather than returning the first hit. The default
is a one-goal set, which reduces to exactly the old behaviour -- there is one code
path, not a flag-selected fork.

A goal names the tiers that can fill it, in preference order. Tiers, not sources:
which sources serve a tier stays in ladder.json, so reordering sources there needs
no change here.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .tiers import Tier


@dataclass
class Goal:
    """One slot to fill, and what filled it."""

    name: str
    tiers: Sequence[Tier]
    path: Optional[str] = None
    artifact: object = None          # retrieval.artifact.Artifact once filled
    preexisting: bool = False        # found on disk rather than fetched now

    @property
    def filled(self) -> bool:
        return self.path is not None

    def accepts(self, tier: Tier) -> bool:
        return tier in self.tiers

    def fill(self, path: str, artifact=None, preexisting: bool = False) -> None:
        self.path = path
        self.artifact = artifact
        self.preexisting = preexisting


@dataclass
class GoalSet:
    """The goals for one walk, plus the questions the engine asks of them."""

    goals: List[Goal] = field(default_factory=list)

    def __iter__(self):
        return iter(self.goals)

    def __len__(self):
        return len(self.goals)

    @property
    def complete(self) -> bool:
        return all(g.filled for g in self.goals)

    @property
    def any_filled(self) -> bool:
        return any(g.filled for g in self.goals)

    def unfilled(self) -> List[Goal]:
        return [g for g in self.goals if not g.filled]

    def goal_for(self, tier: Tier) -> Optional[Goal]:
        """The first UNFILLED goal this tier can fill.

        Unfilled matters: without it a second XML artifact would overwrite the
        first, and the walk would never move on to the PDF.
        """
        for goal in self.goals:
            if not goal.filled and goal.accepts(tier):
                return goal
        return None

    def by_name(self, name: str) -> Optional[Goal]:
        return next((g for g in self.goals if g.name == name), None)

    def wanted_tiers(self) -> List[Tier]:
        """Every tier any unfilled goal would still accept."""
        wanted = []
        for goal in self.unfilled():
            for tier in goal.tiers:
                if tier not in wanted:
                    wanted.append(tier)
        return wanted

    def filled_paths(self) -> List[str]:
        return [g.path for g in self.goals if g.filled]

    def summary(self) -> str:
        """'xml+pdf', or 'pdf (structured: none)' -- for the per-record log line."""
        got = [g.name for g in self.goals if g.filled]
        missing = [g.name for g in self.goals if not g.filled]
        if not missing:
            return "+".join(got)
        return "{} ({}: none)".format("+".join(got) or "nothing", ",".join(missing))


#: The structured half of the collect: best markup available, XML before HTML.
#: T3 LaTeX is deliberately NOT here -- a source tarball is not something a
#: table-extraction pass can read without a TeX toolchain, so it stays a
#: winner-take-all tier rather than a slot to fill alongside the PDF.
_STRUCTURED_TIERS = (Tier.T1_XML, Tier.T2_HTML)


def single_best(tiers: Sequence[Tier]) -> GoalSet:
    """The default: one goal that any tier in the ladder can fill.

    Equivalent to the original stop-at-first-hit walk, expressed as a goal set so
    the engine has one loop instead of two.
    """
    return GoalSet([Goal(name="best", tiers=tuple(tiers))])


def structured_and_pdf(tiers: Sequence[Tier]) -> GoalSet:
    """--get-xml-or-html: a structured copy AND the PDF.

    Ordered structured-first so that when both are still open, a validated T1
    artifact is filed as the structured goal rather than being consumed by a
    broader one.
    """
    structured = [t for t in _STRUCTURED_TIERS if t in tiers]
    pdf = [t for t in (Tier.T5_PDF,) if t in tiers]
    goals = []
    if structured:
        goals.append(Goal(name="structured", tiers=tuple(structured)))
    if pdf:
        goals.append(Goal(name="pdf", tiers=tuple(pdf)))
    # A ladder restricted to neither (nothing but T6/T7, say) would give an empty
    # goal set and a walk that accepts nothing. Fall back to the default rather
    # than silently failing every record.
    return GoalSet(goals) if goals else single_best(tiers)


def build(tiers: Sequence[Tier], get_xml_or_html: bool = False) -> GoalSet:
    """The goal set for this walk."""
    return structured_and_pdf(tiers) if get_xml_or_html else single_best(tiers)
