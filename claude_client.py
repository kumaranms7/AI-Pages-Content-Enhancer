"""Claude API integration for SEO-focused content rewriting.

Claude never sees or touches raw HTML/PHP markup - it only receives a
flat list of {id, role, text} content units (built by content_units.py)
and returns {id, new_text} pairs via a JSON-schema-constrained response.
Substitution back into the original file happens in content_units.py, so
structure can never drift regardless of what Claude returns.
"""
import json

import anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_UNITS_PER_REQUEST = 80

# $ per 1M tokens. Cache read/write tokens are billed off the input rate
# (~0.1x for reads, ~1.25x for writes) - included for completeness even
# though these single-shot structured calls don't set cache_control.
PRICING_PER_MILLION = {
    'claude-opus-4-8': {'input': 5.0, 'output': 25.0},
    'claude-opus-4-7': {'input': 5.0, 'output': 25.0},
    'claude-opus-4-6': {'input': 5.0, 'output': 25.0},
    'claude-opus-4-5': {'input': 5.0, 'output': 25.0},
    'claude-sonnet-4-6': {'input': 3.0, 'output': 15.0},
    'claude-sonnet-4-5': {'input': 3.0, 'output': 15.0},
    'claude-haiku-4-5': {'input': 1.0, 'output': 5.0},
}


def empty_usage():
    return {
        'input_tokens': 0, 'output_tokens': 0,
        'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
    }


def merge_usage(total, addition):
    for key in total:
        total[key] += addition.get(key, 0)
    return total


def estimate_cost(usage, model):
    pricing = PRICING_PER_MILLION.get(model, PRICING_PER_MILLION[DEFAULT_MODEL])
    input_cost = usage.get('input_tokens', 0) / 1_000_000 * pricing['input']
    output_cost = usage.get('output_tokens', 0) / 1_000_000 * pricing['output']
    cache_write_cost = usage.get('cache_creation_input_tokens', 0) / 1_000_000 * pricing['input'] * 1.25
    cache_read_cost = usage.get('cache_read_input_tokens', 0) / 1_000_000 * pricing['input'] * 0.1
    return input_cost + output_cost + cache_write_cost + cache_read_cost

UNITS_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "new_text": {"type": "string"},
    },
    "required": ["id", "new_text"],
    "additionalProperties": False,
}

UNITS_ONLY_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {"type": "array", "items": UNITS_ITEMS_SCHEMA},
    },
    "required": ["units"],
    "additionalProperties": False,
}

UNITS_WITH_KEYWORD_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_keyword": {"type": "string"},
        "units": {"type": "array", "items": UNITS_ITEMS_SCHEMA},
    },
    "required": ["focus_keyword", "units"],
    "additionalProperties": False,
}

ROLE_LABELS = {
    'title': 'page <title> tag',
    'meta_description': 'meta description',
    'meta_keywords': 'meta keywords',
    'alt': 'image alt text',
    'h1': 'H1 heading', 'h2': 'H2 heading', 'h3': 'H3 heading',
    'h4': 'H4 heading', 'h5': 'H5 heading', 'h6': 'H6 heading',
    'a': 'link text', 'button': 'button text',
    'p': 'paragraph', 'li': 'list item', 'body': 'text',
}


def _role_label(role):
    return ROLE_LABELS.get(role, role)


def _build_prompt(units, site_url, site_context, focus_keyword=None, refinement_notes=None):
    payload = [
        {'id': u['id'], 'role': _role_label(u['role']), 'text': u['text'],
         'original_length': len(u['text'])}
        for u in units
    ]
    lines = [
        "You are an on-page SEO content editor.",
        "Site URL: %s" % (site_url or '(not provided)'),
        "Site context / what the business does: %s" % (site_context or '(not provided)'),
        "",
        "You will rewrite the CONTENT of a webpage only. Rules:",
        "- Do not invent facts, prices, or claims not implied by the existing text or site context.",
        "- Preserve the meaning and intent of each piece of text.",
        "- Keep each replacement within +/-20 characters of the original length (see "
        "\"original_length\" on every unit) - these are short fragments inside a fixed page "
        "layout, not full rewrites, and a length swing beyond that can visibly break the layout.",
        "- Return plain text only - no HTML tags, no markdown, no surrounding quotes.",
        "- Optimize for on-page SEO: work the focus keyword naturally into the title, meta "
        "description, H1, and some body text; write a compelling, keyword-rich meta description "
        "(within the +/-20 character rule above, not a separate length target); write "
        "descriptive (non-generic) link text; keep sentences reasonably short for readability.",
        "- To score well across every graded area in one pass: mention the page's specific "
        "location and service by name more than once (title, a heading, and body - the single "
        "biggest factor); keep sentences concise and factual (under ~20 words); include "
        "concrete credentials/numbers (years of experience, licenses, certifications, "
        "insurance); phrase at least one subheading as a direct question a customer would "
        "search for; use action-oriented CTA text ('Get a free quote', 'Schedule a call') "
        "instead of generic phrases; make sure a phone number or contact reference appears in "
        "the body, not just the header.",
    ]
    if focus_keyword:
        lines.append('- The focus keyword for this page is fixed: "%s". Use it as described above.' % focus_keyword)
    else:
        lines.append(
            "- First, decide a single focus keyword/phrase for this page based on its title, "
            "headings, and the site context. Return it as \"focus_keyword\"."
        )
    if refinement_notes:
        lines.append("")
        lines.append(
            "REVISION PASS - the previous rewrite is not good enough yet. Make concrete, "
            "substantive edits that directly fix each issue below - do not make small cosmetic "
            "tweaks - but still stay within +/-20 characters of \"original_length\" on every "
            "unit. The target is 80+/100 overall, reached in this one revision if possible - "
            "there usually isn't a further pass after this."
        )
        lines.extend("- %s" % note for note in refinement_notes)
    lines.append("")
    lines.append("Content units to rewrite (JSON array of {id, role, text}):")
    lines.append(json.dumps(payload, ensure_ascii=False))
    lines.append("")
    lines.append(
        "Return a \"units\" array of {id, new_text} for every id above"
        + ("." if focus_keyword else ', plus "focus_keyword".')
    )
    return "\n".join(lines)


def _call(client, model, prompt, schema, effort):
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    usage = {
        'input_tokens': getattr(response.usage, 'input_tokens', 0) or 0,
        'output_tokens': getattr(response.usage, 'output_tokens', 0) or 0,
        'cache_creation_input_tokens': getattr(response.usage, 'cache_creation_input_tokens', 0) or 0,
        'cache_read_input_tokens': getattr(response.usage, 'cache_read_input_tokens', 0) or 0,
    }
    return json.loads(text), usage


def _chunks(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def rewrite_units(units, site_url, site_context, api_key, model=DEFAULT_MODEL,
                   effort="medium", on_progress=None):
    """Rewrite a flat list of editable units via Claude.

    Returns (new_text_by_id: dict[int, str], focus_keyword: str | None, usage: dict).
    """
    usage_total = empty_usage()
    editable = [u for u in units if u.get('editable')]
    if not editable:
        return {}, None, usage_total

    client = anthropic.Anthropic(api_key=api_key)
    chunks = _chunks(editable, MAX_UNITS_PER_REQUEST)

    new_text_by_id = {}
    focus_keyword = None

    for idx, chunk in enumerate(chunks):
        if on_progress:
            on_progress("Rewriting content (%d/%d)..." % (idx + 1, len(chunks)))
        if focus_keyword is None:
            prompt = _build_prompt(chunk, site_url, site_context)
            result, usage = _call(client, model, prompt, UNITS_WITH_KEYWORD_SCHEMA, effort)
            focus_keyword = (result.get('focus_keyword') or '').strip()
        else:
            prompt = _build_prompt(chunk, site_url, site_context, focus_keyword=focus_keyword)
            result, usage = _call(client, model, prompt, UNITS_ONLY_SCHEMA, effort)
        merge_usage(usage_total, usage)
        for item in result.get('units', []):
            new_text_by_id[item['id']] = item['new_text']

    return new_text_by_id, focus_keyword, usage_total


def refine_units(units, new_text_by_id, focus_keyword, weak_factor_notes, site_url,
                  site_context, api_key, model=DEFAULT_MODEL, effort="medium"):
    """One more pass: ask Claude to specifically fix the listed weak factors.

    Returns (new_text_by_id: dict[int, str], usage: dict).
    """
    usage_total = empty_usage()
    editable = [u for u in units if u.get('editable') and u['id'] in new_text_by_id]
    if not editable or not weak_factor_notes:
        return new_text_by_id, usage_total

    current = [dict(u, text=new_text_by_id.get(u['id'], u['text'])) for u in editable]
    client = anthropic.Anthropic(api_key=api_key)

    updated = dict(new_text_by_id)
    for chunk in _chunks(current, MAX_UNITS_PER_REQUEST):
        prompt = _build_prompt(chunk, site_url, site_context, focus_keyword=focus_keyword,
                                refinement_notes=weak_factor_notes)
        result, usage = _call(client, model, prompt, UNITS_ONLY_SCHEMA, effort)
        merge_usage(usage_total, usage)
        for item in result.get('units', []):
            updated[item['id']] = item['new_text']
    return updated, usage_total
