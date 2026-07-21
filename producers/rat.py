# producers/rat.py
#
# NON-PRODUCER gate for the rat baseline (design §3, method "Open call" — LOCKED inline-only).
# rat builds a live libkit Env but persists NO rebuildable artifact (base_image=null + head_sha=""
# in 177/177 real runs, and its pytest-installing template preamble is dropped), so it is NOT
# produce->measure-able. It is gated OUT of the fresh-container bench (measurable=False) so it can
# never emit a shadowing EBSR-0; its own live libkit score is preserved in live_scores.json.
#
# NO docker build, NO pytest, NO scoring, NO synthesis — produce() is a pure declaration.
from __future__ import annotations

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


class RatProducer:
    """rat is INLINE-ONLY (design §3 open call, LOCKED): no rebuildable artifact.
    base_image=null + head_sha="" in 177/177 real runs, and its pytest-installing template
    preamble is dropped, so it is NOT produce->measure-able. It still runs for its own live
    libkit score (kept in live_scores.json); it is gated OUT of the fresh-container bench so it
    can never emit a shadowing EBSR-0."""
    name = "rat"
    needs_llm = True
    measurable = False

    def __init__(self, llm: str | None = None, **kw):   # tolerate extra kwargs from get(name, **kw)
        self.llm = llm

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        return ProducedEnv(repo=repo, dockerfile=None, status="unmeasurable",
                           note="rat: inline-only (no rebuildable artifact; base_image/head_sha not persisted)",
                           conformance="native", producer_name=self.name)
