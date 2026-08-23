"""Construction-time URL plausibility check for translated service plans.

Cheap static verify: collect every download URL a translated plan mentions and
confirm it actually resolves (HEAD, falling back to a ranged GET) BEFORE any
setup runs. Catches a confidently-wrong plan — a hallucinated download URL, or a
wrong arch-naming that 404s — at construction time. Lives in `envstate` because
network access is allowed here. Pure stdlib; NEVER raises.

Ported verbatim from the validated PoC (poc_translate_diverse.py:verify_plan).
"""

import re as _re


def verify_plan(plan) -> dict:
    """CHEAP STATIC VERIFY: construction-time plausibility check, no setup ever runs.
    Collects every download URL mentioned in the plan and confirms it actually resolves
    (HEAD, falling back to a ranged GET), which catches a confidently-wrong plan — a
    hallucinated download URL, or a wrong arch-naming that 404s — before we ever try it."""
    import urllib.request
    import urllib.error

    _url_re = _re.compile(r"https?://[^\s'\"<>|)]+")

    def _collect(x, found):
        if isinstance(x, str):
            for m in _url_re.findall(x):
                found.append(m.rstrip(".,;:!?)]}\"'"))
        elif isinstance(x, list):
            for item in x:
                _collect(item, found)

    found = []
    _collect(plan.get("install"), found)
    _collect(plan.get("start"), found)
    _collect(plan.get("post"), found)
    urls = [u for u in dict.fromkeys(found)  # unique, order-preserving
            if not _re.search(r"://(localhost|127\.0\.0\.1|0\.0\.0\.0)([:/]|$)", u)]  # skip runtime/loopback

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

    def _check(url):
        last_exc = None
        for method, extra_headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                req = urllib.request.Request(url, method=method,
                                              headers={**headers, **extra_headers})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    if 200 <= status < 400:
                        return status, "ok", ""
                    if method == "HEAD" and status in (403, 405):
                        continue
                    return status, "bad", f"HTTP {status}"
            except urllib.error.HTTPError as e:
                if method == "HEAD" and e.code in (403, 405):
                    continue
                return e.code, "bad", f"HTTP {e.code}"
            except Exception as e:                             # DNS, timeout, connection, ...
                last_exc = e
                continue
        return None, "error", (type(last_exc).__name__ if last_exc is not None else "unknown")

    results = []
    for url in urls:
        try:
            status, state, detail = _check(url)
        except Exception as e:                                 # belt-and-braces: never raise
            status, state, detail = None, "error", type(e).__name__
        results.append({"url": url, "status": status, "state": state, "detail": detail})

    all_ok = all(r["state"] == "ok" for r in results) if results else True
    return {"urls": results, "all_ok": all_ok, "n": len(results)}
