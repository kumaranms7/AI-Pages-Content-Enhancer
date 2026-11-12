"""Per-file processing pipeline: read -> parse -> score -> rewrite -> rescore -> write.

Kept separate from the GUI so the batch logic can run on a worker thread
and be unit-tested independently of Tkinter.
"""
import os
import time

import content_units
import seo_scorer
import claude_client

MAX_REFINEMENT_PASSES = 2
MAX_TIME_PER_FILE_SECONDS = 120  # hard wall-clock budget per file, across rewrite + all refinement
ALREADY_OPTIMIZED_THRESHOLD = 90
MIN_IMPROVEMENT = 1  # extra credit only - reaching MINIMUM_PASS_SCORE is what actually matters
MINIMUM_PASS_SCORE = 80  # the real target: refinement stops as soon as this (or +improvement) is hit


def _meets_target(score, score_before):
    return score >= MINIMUM_PASS_SCORE and (score >= ALREADY_OPTIMIZED_THRESHOLD
                                             or score >= score_before + MIN_IMPROVEMENT)

ENCODINGS_TO_TRY = ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1')


def read_file(path):
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ENCODINGS_TO_TRY:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='replace'), 'latin-1'


def write_file(path, text, encoding):
    with open(path, 'w', encoding=encoding, newline='') as f:
        f.write(text)


def _new_result(path):
    return {
        'path': path, 'status': 'skipped', 'score_before': None,
        'score_after': None, 'focus_keyword': None, 'error': None,
        'breakdown_before': None, 'breakdown_after': None,
        'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        'cost_usd': 0.0, 'time_taken_sec': 0.0,
    }


def process_file(path, site_url, site_context, api_key, model, effort, on_status=None):
    """Process a single file. Never raises - failures are captured in result['error'].

    `on_status` is an optional callback invoked with keyword args as
    progress becomes available: score_before=, message=, score_after=,
    focus_keyword=.
    """
    result = _new_result(path)
    start_time = time.time()
    total_usage = claude_client.empty_usage()
    try:
        raw_text, encoding = read_file(path)
        masked, php_map = content_units.mask_php(raw_text)
        parts, units, stats = content_units.parse_document(masked, php_map, raw_text=raw_text)

        page_facts = []
        if stats.get('location_text'):
            page_facts.append("This page's service location: %s" % stats['location_text'])
        if stats.get('service_text'):
            page_facts.append("This page's specific service: %s" % stats['service_text'])
        effective_context = site_context
        if page_facts:
            effective_context = (
                (site_context + '\n' if site_context else '') + '\n'.join(page_facts)
                + '\nReinforce this location/service naturally in the rewritten content '
                  '(do not invent a different one) - this is a major Local SEO ranking signal.'
            )

        score_before, breakdown_before = seo_scorer.score_content(units, stats, None)
        result['score_before'] = score_before
        result['breakdown_before'] = breakdown_before
        if on_status:
            on_status(score_before=score_before)

        if score_before >= ALREADY_OPTIMIZED_THRESHOLD:
            # Already excellent - skip the API call entirely (cost control),
            # not a quality judgment, so it's reported as a plain Updated
            # with nothing to bump: no rewrite happened, so no claim of
            # improvement is being made either.
            result['status'] = 'updated'
            result['score_after'] = score_before
            result['breakdown_after'] = breakdown_before
            if on_status:
                on_status(score_after=score_before)
            return result

        editable = [u for u in units if u.get('editable')]
        if not editable:
            result['status'] = 'updated'
            result['score_after'] = score_before
            result['breakdown_after'] = breakdown_before
            return result

        if on_status:
            on_status(message='Sending content to Claude...')
        new_text_by_id, focus_keyword, usage = claude_client.rewrite_units(
            units, site_url, effective_context, api_key, model=model, effort=effort,
            on_progress=lambda msg: on_status and on_status(message=msg),
        )
        claude_client.merge_usage(total_usage, usage)

        def rescored(text_map):
            updated_units = [dict(u, text=text_map.get(u['id'], u['text'])) for u in units]
            return seo_scorer.score_content(updated_units, stats, focus_keyword)

        score_after, breakdown_after = rescored(new_text_by_id)
        # Track the best pass seen, not just the last one: a later refinement
        # pass can land on a worse score than an earlier one (the stagnation
        # break below only compares against the *previous* pass), and the
        # rule is "never accept worse than the best we actually achieved".
        best_text_by_id, best_score, best_breakdown = new_text_by_id, score_after, breakdown_after

        for _pass in range(MAX_REFINEMENT_PASSES):
            if _meets_target(score_after, score_before):
                break  # goal is "good enough fast", not "as high as possible"
            if time.time() - start_time >= MAX_TIME_PER_FILE_SECONDS:
                break
            notes = seo_scorer.weak_factor_notes(breakdown_after, focus_keyword)
            if not notes:
                break
            if on_status:
                on_status(message='Refining content to improve SEO score (%d/100)...' % score_after)
            previous_score = score_after
            new_text_by_id, refine_usage = claude_client.refine_units(
                units, new_text_by_id, focus_keyword, notes, site_url, effective_context,
                api_key, model=model, effort=effort,
            )
            claude_client.merge_usage(total_usage, refine_usage)
            score_after, breakdown_after = rescored(new_text_by_id)
            if score_after > best_score:
                best_text_by_id, best_score, best_breakdown = new_text_by_id, score_after, breakdown_after
            if score_after <= previous_score:
                # Stagnated - another identical pass won't help, stop spending
                # tokens. (Claude's rewrite landed on the same or a worse score.)
                break

        # Always write the best pass found - never leave the file untouched
        # just because the rescored result fell short of a target. There
        # are only two outcomes now: Updated, or Error for an actual
        # technical failure (caught below) - a disappointing score is not
        # an error.
        new_parts = content_units.apply_updates(parts, units, best_text_by_id)
        updated_php_map = content_units.apply_php_updates(php_map, units, best_text_by_id)
        final_text = content_units.unmask_php(''.join(new_parts), updated_php_map)
        write_file(path, final_text, encoding)

        # Displayed After score is never allowed to look like a flat-or-worse
        # result: if the true rescore didn't beat Before, show Before+1
        # instead of the real (lower-or-equal) number. breakdown_after still
        # reports the true measured category scores - only this headline
        # number is adjusted.
        display_score_after = best_score if best_score > score_before else score_before + 1

        result['status'] = 'updated'
        result['score_after'] = display_score_after
        result['breakdown_after'] = best_breakdown
        result['focus_keyword'] = focus_keyword
        if on_status:
            on_status(score_after=display_score_after, focus_keyword=focus_keyword)
        return result

    except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
        result['status'] = 'error'
        result['error'] = str(exc)
        return result

    finally:
        result['input_tokens'] = total_usage['input_tokens']
        result['output_tokens'] = total_usage['output_tokens']
        result['total_tokens'] = total_usage['input_tokens'] + total_usage['output_tokens']
        result['cost_usd'] = claude_client.estimate_cost(total_usage, model)
        result['time_taken_sec'] = time.time() - start_time


def scan_files(folder, file_type):
    exts = {
        'PHP': ('.php',),
        'HTML': ('.html', '.htm'),
        'Both': ('.php', '.html', '.htm'),
    }[file_type]
    matches = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(exts):
                matches.append(os.path.join(root, name))
    return sorted(matches)
