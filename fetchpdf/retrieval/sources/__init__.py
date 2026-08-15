"""Per-tier retrieval sources.

Every source is a plain function with the same shape:

    def fetch_x(ids: IdentifierSet, ctx: RetrievalContext) -> Optional[Artifact]

Return None to decline (not applicable, nothing found, request failed). Never
raise into the engine, and never write to disk -- the engine decides what is
accepted, after the classifier and the validator have had their say. A source
that wrote its own output would be able to smuggle an unvalidated artifact past
both.

Which tiers a source serves, and which identifiers it needs, are declared in
ladder.json rather than here.
"""
