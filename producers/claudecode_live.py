# producers/claudecode_live.py
#
# NON-PRODUCER gate for the LIVE claudecode baseline (design §3, method 5). The live claude variant
# mutates an env in place and leaves NO static artifact, so it is NOT produce->measure-able. It is
# gated OUT of the fresh-container bench (measurable=False) so it can never emit a shadowing EBSR-0;
# its own live score is preserved in live_scores.json.
#
# NOTE: name="claudecode" — the LIVE variant. It is DISTINCT from the produce-side
# claudecode-dockerfile producer (name="claudecode-dockerfile"), which does emit a Dockerfile.
#
# NO docker build, NO pytest, NO scoring, NO synthesis — produce() is a pure declaration.
from __future__ import annotations

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


class ClaudeCodeLiveProducer:
    """Live claudecode is a native-lane baseline (design §3 method 5): live in-place mutation with
    no static artifact. It still runs for its own live score (kept in live_scores.json); it is gated
    OUT of the fresh-container bench so it can never emit a shadowing EBSR-0. Distinct from the
    claudecode-dockerfile producer."""
    name = "claudecode"
    needs_llm = True
    measurable = False

    def __init__(self, llm: str | None = None, **kw):   # tolerate extra kwargs from get(name, **kw)
        self.llm = llm

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        return ProducedEnv(repo=repo, dockerfile=None, status="unmeasurable",
                           note="claudecode (live): live in-place mutation, no static artifact",
                           conformance="native", producer_name=self.name)
