# producers/sweagent.py
#
# NON-PRODUCER gate for the SWE-agent baseline (design §3, method 6). SWE-agent emits only
# run_pytest JSONs — no rebuildable Dockerfile artifact — so it is NOT produce->measure-able.
# It is gated OUT of the fresh-container bench (measurable=False) so its empty output/ can never
# be harvested into a shadowing EBSR-0; its own live score is preserved in live_scores.json.
#
# NO docker build, NO pytest, NO scoring, NO synthesis — produce() is a pure declaration.
from __future__ import annotations

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


class SweAgentProducer:
    """SWE-agent is a native-lane baseline (design §3 method 6): no rebuildable artifact — it
    produces only run_pytest JSONs. It still runs for its own live score (kept in live_scores.json);
    it is gated OUT of the fresh-container bench so it can never emit a shadowing EBSR-0."""
    name = "sweagent"
    needs_llm = True
    measurable = False

    def __init__(self, llm: str | None = None, **kw):   # tolerate extra kwargs from get(name, **kw)
        self.llm = llm

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        return ProducedEnv(repo=repo, dockerfile=None, status="unmeasurable",
                           note="sweagent: no Dockerfile artifact (only run_pytest JSONs)",
                           conformance="native", producer_name=self.name)
