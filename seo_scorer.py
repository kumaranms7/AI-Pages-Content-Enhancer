"""A self-contained, heuristic on-page content scorer.

Scores only what this tool can actually change. content_units.py only
ever rewrites the *value* of something that already exists - title text,
meta description, headings, paragraphs, list items, table cells, link/
button text, existing image alt text, and content-shaped PHP variables/
array values (see its module docstring for the exact list). It never
adds or removes an HTML tag or attribute, so this scorer deliberately
never scores (or penalizes) anything in that category: canonical tags,
robots meta, viewport meta, schema/JSON-LD, internal link URLs, HTML
structure, CSS classes/ids, or new image alt attributes. Scoring those
would penalize a page for something a rerun of this tool could never fix.

7 categories, each 0-100, combined into one weighted "overall" score:
Content Quality (20%), AI/LLM Visibility (20%), Local SEO Relevance
(15%), UX/Readability (15%), EEAT Signals (15%), Conversion
Optimization (10%), SEO Structure (5%) - plus a small set of flat
penalty deductions (keyword stuffing, thin content, duplicate/over-long
paragraphs, generic content, no location relevance, no CTA) applied to
the overall score afterwards.
"""
import re

CONTENT_ROLES = {
    'title', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'td', 'th',
    'blockquote', 'figcaption', 'dd', 'dt', 'caption', 'summary', 'label',
    'a', 'button', 'body',
}

CATEGORY_ORDER = [
    'content_quality', 'ai_llm_visibility', 'local_seo', 'ux_readability',
    'eeat_signals', 'conversion_optimization', 'seo_structure',
]

CATEGORY_LABELS = {
    'content_quality': 'Content Quality',
    'ai_llm_visibility': 'AI / LLM Visibility',
    'local_seo': 'Local SEO Relevance',
    'ux_readability': 'UX / Readability',
    'eeat_signals': 'EEAT Signals',
    'conversion_optimization': 'Conversion Optimization',
    'seo_structure': 'SEO Structure',
}

CATEGORY_WEIGHTS = {
    'content_quality': 0.20,
    'ai_llm_visibility': 0.20,
    'local_seo': 0.15,
    'ux_readability': 0.15,
    'eeat_signals': 0.15,
    'conversion_optimization': 0.10,
    'seo_structure': 0.05,
}

CATEGORY_ADVICE = {
    'content_quality': "Cover the topic more comprehensively - more distinct subtopics (H2/H3 "
                        "sections), more depth per section, and content that directly answers "
                        "what someone searching for this service wants to know.",
    'ai_llm_visibility': "Make the specific service and location unmistakable, phrase a few "
                          "subheadings as direct questions, and keep sentences concise and "
                          "factual so AI answer engines can confidently quote this page.",
    'local_seo': "Name the service location and service area explicitly and repeatedly - in "
                 "the title, in a heading, and in the body - plus \"serving <area>\"/\"near "
                 "you\" language and a visible phone number or address.",
    'ux_readability': "Shorten sentences and paragraphs, add more headings to break up content, "
                       "and use lists where it helps scannability.",
    'eeat_signals': "Add concrete specifics - years of experience, certifications, licensing, "
                     "insurance, numbers - and make sure contact information is easy to find.",
    'conversion_optimization': "Use more action-oriented CTA text (e.g. 'Get a free quote' "
                                "instead of 'click here'), more of them, a visible phone/email, "
                                "and light urgency language.",
    'seo_structure': "Improve the title tag, meta description, and H1 - make sure each includes "
                      "the focus keyword and is well-sized, and add a couple of H2/H3 "
                      "subheadings with descriptive existing image alt text.",
}

CTA_VERBS = (
    'get a', 'get your', 'book', 'schedule', 'contact us', 'request', 'buy', 'order', 'sign up',
    'signup', 'start', 'call us', 'call now', 'download', 'subscribe', 'join', 'learn more',
    'discover', 'explore', 'try', 'shop', 'reserve', 'apply', 'claim', 'unlock', 'talk to',
    'speak with', 'get started', 'free quote', 'get a quote', 'find out',
)
EXPERIENCE_WORDS = ('experience', 'years', 'since', 'veteran', 'established', 'decade')
CERTIFICATION_WORDS = (
    'certified', 'licensed', 'accredited', 'credential', 'qualified', 'insured', 'bonded',
    'cissp', 'cisa', 'cism',
)
INDUSTRY_EXPERTISE_WORDS = ('specialist', 'expert', 'industry', 'professional', 'trained')
TRUST_WORDS = (
    'guarantee', 'secure', 'certified', 'accredited', 'trusted', 'testimonial', 'review',
    'award', 'compliance', 'compliant', 'privacy', 'verified', 'official', 'licensed', 'insured',
    'rated', 'satisfaction',
)
URGENCY_WORDS = (
    'today', 'now', 'limited', 'same-day', 'same day', 'emergency', '24/7', '24-hour',
    'fast response', 'quick response', 'immediately', "don't wait", 'act now',
)
LOCAL_SERVICE_PHRASES = (
    'service area', 'serving', 'near you', 'local', 'areas we serve', 'available in',
    'proudly serving', 'serving the', 'in your area',
)
INTENT_SIGNAL_WORDS = (
    'cost', 'price', 'pricing', 'how much', 'process', 'steps', 'what is', 'how to',
    'why', 'benefits', 'quote', 'estimate',
)
PHONE_RE = re.compile(r'(\+?\d[\d\-.\s()]{7,}\d)')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
# Generic "City [State/Region], ST" detector - catches geographic specificity
# even when there's no $location PHP variable to cross-check against. Many
# real pages instead name a region ("South Florida", "Miami-Dade") with no
# comma+abbreviation anywhere, which this alone would never catch - see
# _guess_location for the fallback that covers that case.
LOCATION_PATTERN_RE = re.compile(r'\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+){0,3},\s*[A-Z]{2}\b')
WORD_RE = re.compile(r"[A-Za-z][a-zA-Z'-]*")


def _guess_location(title, prose_text):
    """Best-effort location name when there's no $location PHP variable.

    Tries the trailing 4, then 3, then 2 Title Case words off the title
    (mirroring _guess_keyword's "before the brand suffix" logic) and
    returns the longest one that's actually corroborated by appearing in
    paragraph prose - e.g. for a title ending "...Generator Rental South
    Florida", the 4-word and 3-word trailing phrases won't appear verbatim
    in the body (the real prose says "...Rental in South Florida"), so
    this falls back to the 2-word "South Florida", the real place name,
    instead of over-capturing trailing service words that also happen to
    be capitalized. `prose_text` must be paragraph content only, not the
    full body_text - that also includes the title/h1 text itself
    (CONTENT_ROLES includes 'title'/'h1'), which would let a title's own
    echo in body_text count as "corroboration" even when the location is
    never actually discussed anywhere.
    """
    if not title:
        return ''
    core = re.split(r'\s*[|]\s*', title)[0].strip()
    words = WORD_RE.findall(core)
    prose_lower = prose_text.lower()
    for n in (4, 3, 2):
        if len(words) < n:
            continue
        tail = words[-n:]
        if not all(w[:1].isupper() for w in tail):
            continue
        candidate = ' '.join(tail)
        if candidate.lower() in prose_lower:
            return candidate
    return ''

PSEUDO_LIST_LINE_RE = re.compile(r'^(?:[•\-*]|\d+[.)])\s+')


def _segment_pseudo_structure(p_texts):
    """Recover heading/list/paragraph structure from inside a <p> unit that
    actually contains multiple blank-line-separated sections - common when
    copy was pasted as one block instead of marked up with real <h2>/<li>
    tags (content_units.py keeps a text node's internal line breaks intact,
    so they're still visible here). This only ever reads text that's
    already there; it never assumes a tag exists that doesn't. Without it,
    a page like that scores as if it had one giant paragraph and zero
    subheadings, even though a human (or another LLM) reading the same
    text would clearly see organized sections.

    Returns (pseudo_headings, pseudo_list_items, pseudo_paragraphs).
    """
    headings, list_items, paragraphs = [], [], []
    for text in p_texts:
        sections = re.split(r'\n\s*\n+', text)
        if len(sections) == 1:
            paragraphs.append(text)
            continue
        for section in sections:
            lines = [ln.strip() for ln in section.split('\n') if ln.strip()]
            if not lines:
                continue
            if all(PSEUDO_LIST_LINE_RE.match(ln) for ln in lines):
                list_items.extend(lines)
                continue
            first, rest = lines[0], lines[1:]
            first_words = first.split()
            looks_like_heading = 2 <= len(first_words) <= 14 and not first.endswith(('.', ',', ';'))
            if looks_like_heading and rest and all(PSEUDO_LIST_LINE_RE.match(ln) for ln in rest):
                # A heading-like line directly introducing a bullet list
                # (e.g. "What sets us apart:" followed by "• Fleet Range...")
                # in the same blank-line-delimited block.
                headings.append(first)
                list_items.extend(rest)
            elif looks_like_heading and rest:
                headings.append(first)
                paragraphs.append(' '.join(rest))
            else:
                paragraphs.append(' '.join(lines))
    return headings, list_items, paragraphs


PENALTIES = {
    'keyword_stuffing': 15,
    'thin_content': 20,
    'duplicate_paragraphs': 10,
    'long_paragraphs': 10,
    'generic_content': 15,
    'no_location_relevance': 20,
    'no_cta': 20,
}
THIN_CONTENT_WORDS = 300
LONG_PARAGRAPH_WORDS = 150
KEYWORD_STUFFING_DENSITY = 0.03  # keyword occurrences / total words


def _clip(x):
    return max(0, min(100, round(x)))


def _guess_keyword(title, h1_list):
    text = title or (h1_list[0] if h1_list else '')
    if not text:
        return ''
    # Drop a trailing " | Brand" / " - Brand" suffix, keep the meaningful part.
    return re.split(r'\s*[|\-–—]\s*', text)[0].strip()


def _count_words(text, word_list):
    t = text.lower()
    return sum(t.count(w) for w in word_list)


def _location_score(text, location_text, exact_points, pattern_points):
    if location_text and location_text.lower() in text.lower():
        return exact_points
    if LOCATION_PATTERN_RE.search(text):
        return pattern_points
    return 0


def _content_quality(word_count, kw, headings, p_count, body_text):
    topical_coverage = min(30, len(headings) * 6)
    search_intent = min(25, _count_words(body_text, INTENT_SIGNAL_WORDS) * 5)
    content_depth = min(20, round(word_count / 600 * 20))
    if kw:
        occurrences = body_text.lower().count(kw.lower())
        keyword_optimization = min(15, occurrences * 3)
        heading_relevance = min(10, sum(1 for h in headings if kw.lower() in h.lower()) * 5)
    else:
        keyword_optimization = 0
        heading_relevance = 3
    score = topical_coverage + search_intent + content_depth + keyword_optimization + heading_relevance
    return _clip(score)


def _ai_llm_visibility(title, body_text, service_text, location_text, kw, headings,
                        avg_sentence_len):
    combined = title + ' ' + body_text
    if service_text and service_text.lower() in combined.lower():
        service_clarity = 20
    elif kw and title and kw.lower() in title.lower():
        service_clarity = 12
    else:
        service_clarity = 4
    location_clarity = _location_score(combined, location_text, 15, 10)
    questions = sum(1 for h in headings if h.strip().endswith('?'))
    question_headings = min(15, questions * 8)
    topical_completeness = min(20, len(headings) * 4)
    if avg_sentence_len and avg_sentence_len <= 20:
        natural_language = 15
    elif avg_sentence_len and avg_sentence_len <= 28:
        natural_language = 8
    else:
        natural_language = 3
    eeat_references = min(15, _count_words(body_text, EXPERIENCE_WORDS + CERTIFICATION_WORDS) * 5)
    score = (service_clarity + location_clarity + question_headings + topical_completeness
             + natural_language + eeat_references)
    return _clip(score)


def _local_seo(title, h1s, headings, body_text, location_text, has_contact_info):
    headings_text = ' '.join(h1s + headings)
    location_in_title = _location_score(title, location_text, 20, 12)
    location_in_headings = _location_score(headings_text, location_text, 20, 12)
    location_in_body = _location_score(body_text, location_text, 25, 15)
    nearby_matches = set(LOCATION_PATTERN_RE.findall(body_text))
    if len(nearby_matches) >= 2:
        nearby_cities = 15
    elif len(nearby_matches) == 1:
        nearby_cities = 7
    else:
        nearby_cities = 0
    service_area_language = min(10, _count_words(body_text, LOCAL_SERVICE_PHRASES) * 5)
    contact_references = 10 if has_contact_info else 0
    score = (location_in_title + location_in_headings + location_in_body + nearby_cities
              + service_area_language + contact_references)
    return _clip(score)


def _ux_readability(avg_sentence_len, p_lengths, heading_count, p_count, li_count):
    if avg_sentence_len:
        if avg_sentence_len <= 18:
            sentence_length = 25
        elif avg_sentence_len <= 26:
            sentence_length = 15
        else:
            sentence_length = 5
    else:
        sentence_length = 0
    if p_lengths:
        avg_p_words = sum(p_lengths) / len(p_lengths)
        if avg_p_words <= 80:
            paragraph_length = 25
        elif avg_p_words <= 130:
            paragraph_length = 15
        else:
            paragraph_length = 5
    else:
        paragraph_length = 0
    heading_usage = min(20, (heading_count / max(1, p_count)) * 40)
    content_flow = 15 if p_count >= 3 else (8 if p_count >= 1 else 0)
    scannability = min(15, li_count * 4)
    score = sentence_length + paragraph_length + heading_usage + content_flow + scannability
    return _clip(score)


def _eeat_signals(body_text, has_contact_info):
    experience_references = min(20, _count_words(body_text, EXPERIENCE_WORDS) * 7)
    certifications = min(20, _count_words(body_text, CERTIFICATION_WORDS) * 7)
    trust_signals = min(20, _count_words(body_text, TRUST_WORDS) * 5)
    contact_information = 15 if has_contact_info else 0
    numbers = set(re.findall(r'\b\d{1,4}\b', body_text))
    specific_facts = min(15, len(numbers) * 4)
    industry_expertise = min(10, _count_words(body_text, INDUSTRY_EXPERTISE_WORDS) * 4)
    score = (experience_references + certifications + trust_signals + contact_information
             + specific_facts + industry_expertise)
    return _clip(score)


def _conversion_optimization(cta_mentions, has_contact_info, body_text):
    # cta_mentions counts CTA-verb phrases anywhere in the body text (which
    # already includes link/button text - see CONTENT_ROLES), not just
    # text wrapped in an actual <a>/<button> tag. A page that writes "Call
    # now for a free quote" as plain paragraph text is making exactly the
    # same conversion pitch as one that links it, and Claude controls that
    # wording either way - only whether it's tag-wrapped is out of reach.
    cta_strength = min(30, cta_mentions * 10)
    cta_frequency = min(20, cta_mentions * 4)
    phone_email_visibility = 20 if has_contact_info else 0
    urgency_language = min(15, _count_words(body_text, URGENCY_WORDS) * 8)
    trust_language = min(15, _count_words(body_text, TRUST_WORDS) * 5)
    score = cta_strength + cta_frequency + phone_email_visibility + urgency_language + trust_language
    return _clip(score)


def _seo_structure(title, meta_desc, h1s, headings, kw, img_total, img_with_alt):
    title_quality = 0.0
    if title:
        title_quality += 10
        if 15 <= len(title) <= 65:
            title_quality += 10
        if kw and kw.lower() in title.lower():
            title_quality += 10
    meta_description_quality = 0.0
    if meta_desc:
        meta_description_quality += 10
        if 50 <= len(meta_desc) <= 165:
            meta_description_quality += 8
        if kw and kw.lower() in meta_desc.lower():
            meta_description_quality += 7
    if len(h1s) == 1:
        h1_relevance = 12 + (8 if kw and kw.lower() in h1s[0].lower() else 0)
    elif len(h1s) > 1:
        h1_relevance = 6
    else:
        h1_relevance = 0
    heading_hierarchy_quality = min(15, len(headings) * 4)
    existing_image_alt_quality = (round(img_with_alt / img_total * 10) if img_total > 0 else 10)
    score = (title_quality + meta_description_quality + h1_relevance
             + heading_hierarchy_quality + existing_image_alt_quality)
    return _clip(score)


def _penalties(word_count, kw, body_text, p_texts, has_contact_info, location_signal_found,
                cta_mentions):
    deductions = []
    if kw:
        density = body_text.lower().count(kw.lower()) / max(1, word_count)
        if density > KEYWORD_STUFFING_DENSITY:
            deductions.append('keyword_stuffing')
    if word_count < THIN_CONTENT_WORDS:
        deductions.append('thin_content')
    normalized = [re.sub(r'\s+', ' ', t.strip().lower()) for t in p_texts if t.strip()]
    if len(normalized) != len(set(normalized)) and len(normalized) > 1:
        deductions.append('duplicate_paragraphs')
    if any(len(re.findall(r"\w+", t)) > LONG_PARAGRAPH_WORDS for t in p_texts):
        deductions.append('long_paragraphs')
    if (not _count_words(body_text, EXPERIENCE_WORDS + CERTIFICATION_WORDS + TRUST_WORDS)
            and not re.search(r'\b\d{1,4}\b', body_text)):
        deductions.append('generic_content')
    if not location_signal_found:
        deductions.append('no_location_relevance')
    if not cta_mentions:
        deductions.append('no_cta')
    return deductions


def _extract_signals(units, stats, focus_keyword):
    """Single parse of `units` into everything every category/penalty needs.

    Kept separate from category_scores so score_content doesn't have to
    parse `units` twice (once for category scores, once for penalties).
    """
    by_role = {}
    for u in units:
        by_role.setdefault(u['role'], []).append(u['text'])

    title = by_role.get('title', [''])[0] if by_role.get('title') else ''
    meta_desc = by_role.get('meta_description', [''])[0] if by_role.get('meta_description') else ''
    h1s = by_role.get('h1', [])
    h2s = by_role.get('h2', [])
    h3s = by_role.get('h3', [])
    h_other = by_role.get('h4', []) + by_role.get('h5', []) + by_role.get('h6', [])
    li_texts = by_role.get('li', [])
    p_texts = by_role.get('p', [])

    kw = (focus_keyword or _guess_keyword(title, h1s)).strip()

    body_text = ' '.join(
        text for role, texts in by_role.items() if role in CONTENT_ROLES for text in texts
    )
    word_count = len(re.findall(r"[A-Za-z0-9'’-]+", body_text))

    sentences = [s for s in re.split(r'(?<=[.!?])\s+', body_text.strip()) if s.strip()]
    avg_sentence_len = (word_count / len(sentences)) if sentences else 0.0

    pseudo_headings, pseudo_list_items, pseudo_paragraphs = _segment_pseudo_structure(p_texts)
    effective_headings = h2s + h3s + pseudo_headings
    effective_li_count = len(li_texts) + len(pseudo_list_items)
    p_lengths = [len(re.findall(r"\w+", t)) for t in pseudo_paragraphs]
    heading_count = len(h1s) + len(effective_headings) + len(h_other)

    prose_text = ' '.join(pseudo_paragraphs)
    location_text = stats.get('location_text', '') or _guess_location(title, prose_text)

    return {
        'title': title, 'meta_desc': meta_desc, 'h1s': h1s, 'headings': effective_headings,
        'li_count': effective_li_count, 'p_texts': pseudo_paragraphs, 'kw': kw,
        'body_text': body_text, 'word_count': word_count, 'avg_sentence_len': avg_sentence_len,
        'p_lengths': p_lengths, 'heading_count': heading_count,
        'has_contact_info': bool(PHONE_RE.search(body_text) or EMAIL_RE.search(body_text)),
        'location_text': location_text,
        'service_text': stats.get('service_text', ''),
        'img_total': stats.get('img_total', 0), 'img_with_alt': stats.get('img_with_alt', 0),
        'cta_mentions': _count_words(body_text, CTA_VERBS),
    }


def _category_scores_from_signals(s):
    return {
        'content_quality': _content_quality(s['word_count'], s['kw'], s['headings'],
                                             max(1, len(s['p_texts'])), s['body_text']),
        'ai_llm_visibility': _ai_llm_visibility(s['title'], s['body_text'], s['service_text'],
                                                 s['location_text'], s['kw'], s['headings'],
                                                 s['avg_sentence_len']),
        'local_seo': _local_seo(s['title'], s['h1s'], s['headings'], s['body_text'],
                                 s['location_text'], s['has_contact_info']),
        'ux_readability': _ux_readability(s['avg_sentence_len'], s['p_lengths'],
                                           s['heading_count'], max(1, len(s['p_texts'])),
                                           s['li_count']),
        'eeat_signals': _eeat_signals(s['body_text'], s['has_contact_info']),
        'conversion_optimization': _conversion_optimization(
            s['cta_mentions'], s['has_contact_info'], s['body_text']),
        'seo_structure': _seo_structure(s['title'], s['meta_desc'], s['h1s'], s['headings'],
                                         s['kw'], s['img_total'], s['img_with_alt']),
    }


def category_scores(units, stats, focus_keyword):
    """Compute all 7 category scores (0-100) for a list of content units."""
    return _category_scores_from_signals(_extract_signals(units, stats, focus_keyword))


def overall_from_categories(categories):
    total = sum(categories[c] * CATEGORY_WEIGHTS[c] for c in CATEGORY_WEIGHTS)
    return _clip(total)


def score_content(units, stats, focus_keyword):
    """Returns (overall_score: int 0-100, categories: dict[name] -> int 0-100).

    Penalty deductions (keyword stuffing, thin content, duplicate/over-long
    paragraphs, generic content, no location relevance, no CTA) are applied
    to the weighted overall score after combining categories, per the
    PENALTIES step in the rubric this mirrors - they are about page-level
    red flags rather than any single category.
    """
    s = _extract_signals(units, stats, focus_keyword)
    categories = _category_scores_from_signals(s)
    overall = overall_from_categories(categories)

    location_signal_found = bool(
        _location_score(s['title'] + ' ' + s['body_text'], s['location_text'], 1, 1)
        or _count_words(s['body_text'], LOCAL_SERVICE_PHRASES)
    )
    triggered = _penalties(s['word_count'], s['kw'], s['body_text'], s['p_texts'],
                            s['has_contact_info'], location_signal_found, s['cta_mentions'])
    overall = _clip(overall - sum(PENALTIES[p] for p in triggered))
    return overall, categories


def weak_factor_notes(categories, focus_keyword, threshold=100):
    """Plain-English notes for every category that isn't yet perfect.

    This threshold drives the refinement loop in processor.py: once every
    category clears it, notes comes back empty and refinement stops. It
    is deliberately set to 100 (not just "close to" the loop's target) so
    that as long as the *weighted* overall score is below the target,
    there is always at least one category to report on - if every single
    category were already at 100, the overall would be 100 too, which
    the loop's own target check already catches. A looser threshold (e.g.
    90, or even 97) can let every category "pass" while overall is still
    short of target, silently halting refinement too early.
    """
    notes = []
    for key in CATEGORY_ORDER:
        score = categories.get(key, 0)
        if score < threshold:
            label = CATEGORY_LABELS.get(key, key)
            advice = CATEGORY_ADVICE.get(key, '')
            notes.append('%s scored %d/100. %s' % (label, score, advice))
    if focus_keyword:
        notes = [n.replace('the focus keyword', 'the focus keyword "%s"' % focus_keyword) for n in notes]
    return notes
