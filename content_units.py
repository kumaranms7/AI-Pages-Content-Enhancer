"""Structure-preserving parser for PHP/HTML files.

The parser never rewrites markup itself. It masks <?php ... ?> blocks,
walks the remaining HTML as a flat sequence of tag/text parts, and pulls
out only the literal text nodes and a handful of SEO-relevant attributes
(meta description/keywords, image alt text) as "units". Each unit knows
exactly which part it came from, so substituting new text back in never
touches a tag, attribute, class, or id it didn't explicitly target.
"""
import re

PHP_BLOCK_RE = re.compile(r'<\?(?:php\b|=).*?(?:\?>|\Z)', re.DOTALL | re.IGNORECASE)
TAG_OR_COMMENT_RE = re.compile(r'(<!--.*?-->|<[^>]+>)', re.DOTALL)
TAG_NAME_RE = re.compile(r'</?\s*([a-zA-Z][a-zA-Z0-9]*)')
ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("[^"]*"|\'[^\']*\')')

CANONICAL_RE = re.compile(r'<link[^>]*rel=["\']canonical["\']', re.IGNORECASE)
META_ROBOTS_RE = re.compile(r'<meta[^>]*name=["\']robots["\']', re.IGNORECASE)
VIEWPORT_META_RE = re.compile(r'<meta[^>]*name=["\']viewport["\']', re.IGNORECASE)
# has_canonical/has_meta_robots/has_viewport are kept in stats for visibility
# but the scorer never uses them: this app only rewrites existing text/attr
# values, so it can't add a missing canonical/robots/viewport tag - scoring
# on a tag the page can never gain would just penalize pages this tool has
# no way to improve. Same reasoning is why schema/JSON-LD is not extracted
# at all here anymore (the scorer is forbidden from using it as a factor).

# Read-only - used for Local SEO scoring context, never made editable (see
# PHP_HARD_EXCLUDE_VARS below, which these two names are also covered by).
PHP_LOCATION_VAR_RE = re.compile(
    r"\$location\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1\s*;", re.DOTALL | re.IGNORECASE)
PHP_SERVICE_VAR_RE = re.compile(
    r"\$service\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1\s*;", re.DOTALL | re.IGNORECASE)


def extract_php_scoring_context(raw_text):
    """Read-only $location / $service values, for Local SEO scoring and as
    informational (never editable) context handed to Claude during
    rewriting - see PHP_HARD_EXCLUDE_VARS for why these can't be units."""
    loc = PHP_LOCATION_VAR_RE.search(raw_text)
    svc = PHP_SERVICE_VAR_RE.search(raw_text)
    return {
        'location_text': _php_unescape(loc.group(2), loc.group(1)) if loc else '',
        'service_text': _php_unescape(svc.group(2), svc.group(1)) if svc else '',
    }

VOID_TAGS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}
# Text inside these is never visible page content (or, for svg, not prose).
SKIP_CONTENT_TAGS = {'script', 'style', 'noscript', 'svg', 'template'}
HEADING_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
BODY_TAGS = {
    'p', 'li', 'td', 'th', 'blockquote', 'figcaption', 'dd', 'dt',
    'caption', 'summary', 'label', 'a', 'button',
}
# Site header/nav/menu (and anything inside it, including a CTA button
# that lives in the menu) is never made editable, regardless of tag - the
# header repeats across many pages, so per-page rewriting would make
# the site's navigation/branding inconsistent. <header>/<nav> is the
# semantic-HTML case; the class-name check catches div-based markup
# (e.g. class="site-header", class="navbar") that doesn't use those tags.
NAV_EXCLUDE_TAGS = {'header', 'nav'}
NAV_EXCLUDE_CLASS_RE = re.compile(r'\b(?:menu|nav|navbar|header|top[\s_-]?bar)\b', re.IGNORECASE)
# A "top bar" (phone/hours/social strip) commonly sits in the body *before*
# the <header>/<nav> landmark rather than inside it - excluded the same way,
# but by document position rather than nesting. Only takes effect if such a
# landmark exists somewhere on the page at all; otherwise there's no "above
# the header" zone to speak of and nothing extra is excluded by this rule.
HEADER_LANDMARK_RE = re.compile(r'<(?:header|nav)\b', re.IGNORECASE)
GENERIC_ANCHOR_PHRASES = {
    'click here', 'read more', 'here', 'link', 'this', 'more', 'learn more', 'more info',
}

# --- PHP variable / array-literal content (e.g. $title = "..."; or
# 'question' => '...' inside an array) -------------------------------------
#
# Programmatic-SEO pages commonly put real on-page content - title, meta
# description, FAQ text, intro paragraphs - into PHP variables that get
# echoed elsewhere (often in an included head.php/template), rather than
# as literal HTML in the file itself. The whole-block PHP mask above makes
# these invisible to the HTML parser, so they need their own extraction
# pass over the *original* (unmasked) text of each <?php ?> block.
PHP_SCALAR_ASSIGN_RE = re.compile(
    r"\$(\w+)\s*=\s*(['\"])((?:\\.|(?!\2).)*)\2\s*;", re.DOTALL)
PHP_ARRAY_KV_RE = re.compile(
    r"(['\"])(\w+)\1\s*=>\s*(['\"])((?:\\.|(?!\3).)*)\3", re.DOTALL)

# Variables that look like templating tokens, not prose - never rewritten
# even if their value happens to be multi-word.
PHP_HARD_EXCLUDE_VARS = {'location', 'service', 'schema', 'faq_schema'}

# A variable/array-key name must match one of these AND its value must
# look like prose (see _php_looks_like_prose) to be treated as content.
# This keeps slugs, ids, paths, and schema.org type tokens untouched
# without needing a hand-maintained exclude list per page.
PHP_CONTENT_NAME_HINT_RE = re.compile(
    r"title|description|content|faq|question|answer|heading|subtitle|intro|summary|"
    r"caption|paragraph|cta|tagline|excerpt|h[1-6]\b|body|label",
    re.IGNORECASE,
)

PHP_ROLE_HINTS = [
    (re.compile(r'title', re.IGNORECASE), 'title'),
    (re.compile(r'description', re.IGNORECASE), 'meta_description'),
    (re.compile(r'\bquestion\b', re.IGNORECASE), 'h3'),
    (re.compile(r'\banswer\b', re.IGNORECASE), 'p'),
    (re.compile(r'\bh1\b', re.IGNORECASE), 'h1'),
    (re.compile(r'\bh2\b|heading|subtitle', re.IGNORECASE), 'h2'),
    (re.compile(r'cta|tagline', re.IGNORECASE), 'a'),
]


def _php_role_for(name):
    for pattern, role in PHP_ROLE_HINTS:
        if pattern.search(name):
            return role
    return 'body'


def _php_looks_like_prose(value):
    return len(value.split()) >= 3


def _php_unescape(value, quote):
    placeholder = '\x00BSLASH\x00'
    value = value.replace('\\\\', placeholder)
    value = value.replace("\\'", "'").replace('\\"', '"')
    if quote == '"':
        value = value.replace('\\n', '\n').replace('\\t', '\t')
    return value.replace(placeholder, '\\')


def _php_escape(value, quote):
    value = value.replace('\\', '\\\\')
    value = value.replace(quote, '\\' + quote)
    if quote == '"':
        value = value.replace('\n', '\\n').replace('\t', '\\t')
    return value


def extract_php_units(php_map, start_id=0):
    """Scan each masked-out <?php ?> block's original text for content-
    eligible variable/array-literal string values.

    Returns a list of unit dicts (kind='php') compatible with the units
    produced by parse_document - same id/role/text/editable shape, plus
    `token` (which php_map entry it came from) and `original_quoted` (the
    exact quoted substring to replace on write-back).
    """
    units = []
    next_id = start_id

    def consider(name, quote, raw_value, token):
        nonlocal next_id
        if name.lower() in PHP_HARD_EXCLUDE_VARS:
            return
        if NAV_EXCLUDE_CLASS_RE.search(name):
            # A $menu_cta / $nav_label style variable - same "never touch
            # header/nav/menu content" rule as the HTML-side exclusion,
            # just keyed off the variable name since PHP vars carry no DOM
            # position to check against <header>/<nav>/class="menu".
            return
        if not PHP_CONTENT_NAME_HINT_RE.search(name):
            return
        value = _php_unescape(raw_value, quote)
        if not _php_looks_like_prose(value):
            return
        units.append({
            'id': next_id, 'role': _php_role_for(name), 'text': value,
            'kind': 'php', 'token': token, 'quote': quote,
            'original_quoted': quote + raw_value + quote, 'editable': True,
        })
        next_id += 1

    for token, block_text in php_map.items():
        for m in PHP_SCALAR_ASSIGN_RE.finditer(block_text):
            consider(m.group(1), m.group(2), m.group(3), token)
        for m in PHP_ARRAY_KV_RE.finditer(block_text):
            consider(m.group(2), m.group(3), m.group(4), token)

    return units


def apply_php_updates(php_map, units, new_text_by_id):
    """Return a new php_map with content-eligible variable values replaced.

    Only ever touches the exact quoted literal that was extracted - PHP
    code, variable names, array structure, and untouched variables
    ($location, $service, $schema, ...) are byte-for-byte unchanged.
    """
    updated = dict(php_map)
    for unit in units:
        if unit.get('kind') != 'php':
            continue
        new_text = new_text_by_id.get(unit['id'])
        if new_text is None or new_text == unit['text']:
            continue
        quote = unit['quote']
        new_quoted = quote + _php_escape(new_text, quote) + quote
        block_text = updated[unit['token']]
        updated[unit['token']] = block_text.replace(unit['original_quoted'], new_quoted, 1)
    return updated


def mask_php(content):
    """Replace every <?php ... ?> / <?= ... ?> block with an opaque token.

    Returns (masked_content, php_map) where php_map maps token -> original
    PHP source. Tokens use NUL bytes so they can never collide with real
    HTML/PHP text and are trivially detectable later.
    """
    php_map = {}

    def _replace(match):
        token = '\x00PHP%d\x00' % len(php_map)
        php_map[token] = match.group(0)
        return token

    return PHP_BLOCK_RE.sub(_replace, content), php_map


def unmask_php(content, php_map):
    for token, original in php_map.items():
        content = content.replace(token, original)
    return content


def _tag_name(tag_str):
    m = TAG_NAME_RE.match(tag_str)
    return m.group(1).lower() if m else None


def _is_closing(tag_str):
    return tag_str.startswith('</')


def _is_self_closing(tag_str):
    return tag_str.rstrip().endswith('/>')


def _parse_attrs(tag_str):
    attrs = {}
    for name, raw_value in ATTR_RE.findall(tag_str):
        attrs[name.lower()] = (raw_value[0], raw_value[1:-1])  # (quote_char, value)
    return attrs


def parse_document(masked_content, php_map=None, raw_text=None):
    """Split into raw parts and extract editable content units.

    Returns (parts, units, stats).
    - `parts`: list of strings alternating text/tag content, in document
      order. Reconstruction only ever replaces individual list entries.
    - `units`: dicts referencing a part (and, for attributes, the exact
      original attribute substring) plus a `role` used for SEO scoring.
      When `php_map` is given, content-eligible PHP variable/array-literal
      values (see extract_php_units) are appended with continuing ids.
    - `stats`: image/link/schema/local-SEO signals used by the scorer.
      `raw_text` (the original, pre-PHP-mask source) is used for
      $location/$service extraction, since both are commonly PHP-
      assembled (see extract_php_scoring_context); it falls back to
      masked_content if not given (won't see PHP-only vars in that case).
    """
    php_source = raw_text if raw_text is not None else masked_content
    parts = TAG_OR_COMMENT_RE.split(masked_content)
    units = []
    stats = {
        'img_total': 0, 'img_with_alt': 0, 'link_total': 0, 'link_generic': 0,
        'link_internal': 0, 'link_external': 0,
        'has_canonical': bool(CANONICAL_RE.search(masked_content)),
        'has_meta_robots': bool(META_ROBOTS_RE.search(masked_content)),
        'has_viewport': bool(VIEWPORT_META_RE.search(masked_content)),
    }
    stats.update(extract_php_scoring_context(php_source))
    tag_stack = []
    # Parallel to tag_stack: whether the tag at the same stack position (or
    # any of its ancestors) is inside an excluded header/nav/menu region.
    exclude_stack = []
    # True once the first <header>/<nav> landmark has been entered, or from
    # the start if the page has no such landmark at all (nothing "above the
    # header" to exclude in that case). Mutable cell, flipped on encounter.
    seen_header_landmark = [not bool(HEADER_LANDMARK_RE.search(masked_content))]
    next_id = [0]

    def new_id():
        next_id[0] += 1
        return next_id[0] - 1

    def in_skipped_context():
        return any(t in SKIP_CONTENT_TAGS for t in tag_stack)

    def in_excluded_context():
        return bool(exclude_stack and exclude_stack[-1])

    for i, part in enumerate(parts):
        if not part:
            continue

        if part.startswith('<!--'):
            continue

        if part.startswith('<'):
            name = _tag_name(part)
            if not name:
                continue
            if _is_closing(part):
                if name in tag_stack:
                    while tag_stack and tag_stack[-1] != name:
                        tag_stack.pop()
                        if exclude_stack:
                            exclude_stack.pop()
                    if tag_stack:
                        tag_stack.pop()
                        if exclude_stack:
                            exclude_stack.pop()
                continue

            attrs = _parse_attrs(part)
            class_value = attrs.get('class', (None, ''))[1]
            before_header = 'body' in tag_stack and not seen_header_landmark[0]
            is_excluded = (
                in_excluded_context() or name in NAV_EXCLUDE_TAGS
                or bool(NAV_EXCLUDE_CLASS_RE.search(class_value))
                or before_header
            )
            if name in NAV_EXCLUDE_TAGS:
                seen_header_landmark[0] = True

            if name == 'meta':
                meta_name = attrs.get('name', (None, ''))[1].lower()
                meta_property = attrs.get('property', (None, ''))[1].lower()
                role = None
                if meta_name == 'description' or meta_property == 'og:description':
                    role = 'meta_description'
                elif meta_name == 'keywords':
                    role = 'meta_keywords'
                if role and 'content' in attrs:
                    quote, value = attrs['content']
                    if value.strip():
                        units.append({
                            'id': new_id(), 'role': role, 'text': value,
                            'kind': 'attr', 'part_index': i,
                            'attr_name': 'content', 'quote': quote,
                            'editable': True,
                        })

            elif name == 'img':
                stats['img_total'] += 1
                if 'alt' in attrs:
                    quote, value = attrs['alt']
                    if value.strip():
                        stats['img_with_alt'] += 1
                        if not is_excluded:
                            units.append({
                                'id': new_id(), 'role': 'alt', 'text': value,
                                'kind': 'attr', 'part_index': i,
                                'attr_name': 'alt', 'quote': quote,
                                'editable': True,
                            })

            elif name == 'a':
                stats['link_total'] += 1
                href = attrs.get('href', (None, ''))[1].strip()
                if href and not href.startswith(('#', 'tel:', 'mailto:', 'javascript:')):
                    if href.startswith(('http://', 'https://', '//')):
                        stats['link_external'] += 1
                    else:
                        stats['link_internal'] += 1

            if name not in VOID_TAGS and not _is_self_closing(part):
                tag_stack.append(name)
                exclude_stack.append(is_excluded)
            continue

        # Text node between tags.
        if in_skipped_context():
            continue
        if in_excluded_context():
            continue
        if 'body' in tag_stack and not seen_header_landmark[0]:
            # Loose text directly under <body> (no wrapping tag) before the
            # header/nav landmark is reached - same "above the header" rule,
            # covering the case the exclude_stack inheritance above doesn't
            # (that's only set on opening tags, not on bare text nodes).
            continue
        if '\x00PHP' in part:
            # Mixed literal text + PHP echo (e.g. "Welcome <?php echo $x; ?>") -
            # too risky to let an LLM rewrite without losing the PHP token.
            continue
        stripped = part.strip()
        if not stripped:
            continue

        top = tag_stack[-1] if tag_stack else None
        if top is None:
            continue
        if top == 'title':
            role = 'title'
        elif top in HEADING_TAGS:
            role = top
        elif top in BODY_TAGS:
            role = top
            if top == 'a' and stripped.lower() in GENERIC_ANCHOR_PHRASES:
                stats['link_generic'] += 1
        elif top == 'head':
            continue
        else:
            role = 'body'

        lead = part[:len(part) - len(part.lstrip())]
        trail = part[len(part.rstrip()):]
        units.append({
            'id': new_id(), 'role': role, 'text': stripped,
            'kind': 'text', 'part_index': i,
            'lead': lead, 'trail': trail,
            'editable': True,
        })

    if php_map:
        units.extend(extract_php_units(php_map, start_id=next_id[0]))

    return parts, units, stats


def apply_updates(parts, units, new_text_by_id):
    """Return a new parts list with editable HTML units substituted in place.

    PHP-kind units (see extract_php_units) are not part of `parts` - they
    are substituted separately via apply_php_updates.
    """
    out = list(parts)
    for unit in units:
        if unit['kind'] == 'php':
            continue
        new_text = new_text_by_id.get(unit['id'])
        if new_text is None or new_text == unit['text']:
            continue
        i = unit['part_index']
        if unit['kind'] == 'text':
            out[i] = unit['lead'] + new_text + unit['trail']
        else:
            quote = unit['quote']
            entity = '&quot;' if quote == '"' else '&#39;'
            safe_value = new_text.replace(quote, entity)
            old_attr = '%s=%s%s%s' % (unit['attr_name'], quote, unit['text'], quote)
            new_attr = '%s=%s%s%s' % (unit['attr_name'], quote, safe_value, quote)
            out[i] = out[i].replace(old_attr, new_attr, 1)
    return out
