"""Small shared helpers for the web tests (imported like ``sampledata``)."""

import json
import re


def jsonld(body: str) -> list[dict]:
    """Every ld+json block on a page, parsed."""
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)]
