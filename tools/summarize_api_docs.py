"""Download the Infomaniak eTickets API docs and print a compact summary.

Used by the 'API docs' workflow (GitHub runners can reach Infomaniak, the
dev environment cannot). Prints the security schemes and every path with
its methods, parameters and summary, so the connector can be aligned on the
real API. Reads public documentation only; no credentials involved.
"""
import json
import re
import sys
from urllib.parse import urljoin

import requests

DOCS_URL = sys.argv[1] if len(sys.argv) > 1 else "https://etickets.infomaniak.com/docs/app/index.html"


def get(url):
    response = requests.get(url, timeout=30)
    print(f"# GET {url} -> {response.status_code} ({len(response.content)} bytes, "
          f"{response.headers.get('content-type')})")
    return response


def find_spec_urls(html, base):
    candidates = set(re.findall(r"""["']([^"']+\.(?:json|ya?ml))["']""", html))
    candidates |= set(re.findall(r"""url\s*[:=]\s*["']([^"']+)["']""", html))
    return [urljoin(base, c) for c in candidates]


def load_spec(response):
    try:
        return response.json()
    except ValueError:
        try:
            import yaml  # noqa: PLC0415
            return yaml.safe_load(response.text)
        except Exception:
            return None


def summarize(spec):
    print("\n## info:", json.dumps(spec.get("info", {}), ensure_ascii=False)[:500])
    print("## servers / host / basePath:", spec.get("servers"), spec.get("host"), spec.get("basePath"))
    security = spec.get("securityDefinitions") or (spec.get("components") or {}).get("securitySchemes")
    print("## security schemes:", json.dumps(security, ensure_ascii=False, indent=1))
    print("## global security:", spec.get("security"))
    params = (spec.get("parameters") or {}) | ((spec.get("components") or {}).get("parameters") or {})
    if params:
        print("## shared parameters:")
        for name, p in params.items():
            print(f"  {name}: in={p.get('in')} name={p.get('name')} required={p.get('required')} "
                  f"- {(p.get('description') or '')[:150]}")
    print("## paths:")
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            plist = []
            for p in op.get("parameters", []):
                if "$ref" in p:
                    plist.append(p["$ref"].split("/")[-1])
                else:
                    plist.append(f"{p.get('in')}:{p.get('name')}{'*' if p.get('required') else ''}")
            print(f"  {method.upper():6} {path}  [{', '.join(plist)}]  "
                  f"sec={op.get('security')}  {(op.get('summary') or op.get('description') or '')[:140]}")


def main():
    page = get(DOCS_URL)
    html = page.text
    print("## page title:", re.findall(r"<title>(.*?)</title>", html, re.S)[:1])
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    print("## page text (start):", re.sub(r"\s+", " ", text)[:3000])
    for url in find_spec_urls(html, DOCS_URL):
        response = get(url)
        if response.status_code != 200:
            continue
        spec = load_spec(response)
        if isinstance(spec, dict) and ("paths" in spec or "openapi" in spec or "swagger" in spec):
            summarize(spec)
            return
    scripts = re.findall(r"""src=["']([^"']+)["']""", html)
    print("## no OpenAPI spec in the page; scripts referenced:", scripts)
    for script in scripts:
        bundle = get(urljoin(DOCS_URL, script)).text
        # Docs assets the bundle loads (spec, markdown, json).
        assets = sorted(set(re.findall(r"""["']([\w./-]+\.(?:json|ya?ml|md))["']""", bundle)))
        print("## assets referenced by the bundle:", assets[:80])
        for asset in assets:
            response = get(urljoin(DOCS_URL, asset))
            spec = load_spec(response) if response.status_code == 200 else None
            if isinstance(spec, dict) and ("paths" in spec or "openapi" in spec or "swagger" in spec):
                summarize(spec)
                return
        print_snippets(bundle)


KEYWORDS = ("credential", "Authorization", "header", "/api/", "api/shop", "Bearer", "token", "mandatory")


def print_snippets(text, width=220, limit=120):
    """Context around auth / endpoint keywords inside a JS bundle."""
    seen, count = set(), 0
    for keyword in KEYWORDS:
        for match in re.finditer(re.escape(keyword), text):
            start = max(0, match.start() - width // 2)
            snippet = re.sub(r"\s+", " ", text[start:start + width])
            key = snippet[:80]
            if key in seen:
                continue
            seen.add(key)
            print(f"[{keyword}] ...{snippet}...")
            count += 1
            if count >= limit:
                return
    paths = sorted(set(re.findall(r"""["'`](/(?:api/)?[a-z_]+(?:/[a-z_{}:]+)*)["'`]""", text)))
    print("## path-like strings:", paths[:200])


if __name__ == "__main__":
    main()
