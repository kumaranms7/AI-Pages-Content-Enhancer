"""AI Page Content Enhancer - desktop app host.

The UI lives in webui/ as real HTML + Tailwind CSS, rendered in a native
window via pywebview (Edge WebView2 on Windows) - this is what makes real
gradients, glassmorphism blur, box-shadows, and smooth CSS transitions
possible, none of which a Tkinter widget toolkit can do natively.

This file is just the backend: it exposes an Api object to the page's
JavaScript (webui/app.js) over the pywebview bridge, and reuses the
existing, unchanged processing pipeline (content_units.py, seo_scorer.py,
claude_client.py, processor.py) exactly as before. No scoring, rewriting,
or PHP-safety logic changed in this file - only how the UI is rendered.
"""
import csv
import json
import os
import threading
import time
import traceback

import webview

import claude_client
import processor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'enhancer_config.json')

EFFORT_LABELS = {
    'Faster / cheaper': 'low',
    'Balanced (recommended)': 'medium',
    'Highest quality': 'high',
}

MODEL_LABELS = {
    'Claude Sonnet 4.6 (recommended)': 'claude-sonnet-4-6',
    'Claude Opus 4.8 (most capable)': 'claude-opus-4-8',
    'Claude Haiku 4.5 (fastest / cheapest)': 'claude-haiku-4-5',
}


# Kept at module level, NOT as attributes on Api: pywebview's js_api bridge
# introspects every attribute of the exposed object to build the JS-side
# API, and a threading.Event/Thread instance there hangs that introspection
# indefinitely - the window opens and renders, but window.pywebview never
# gets injected, so no event listener in app.js ever attaches (confirmed via
# isolated single-variable bisection: identical class, only difference was
# self.stop_event = threading.Event() vs None).
_stop_event = threading.Event()
_worker = None


class Api:
    def __init__(self):
        self.window = None
        self.is_running = False

    def set_window(self, window):
        self.window = window

    # ------------------------------------------------------------- config

    def load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return {}
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_config(self, cfg):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass
        return True

    # ------------------------------------------------------------ dialogs

    def browse_folder(self):
        result = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else None

    def export_csv(self, rows):
        result = self.window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename='seo_results.csv',
            file_types=('CSV Files (*.csv)', 'All files (*.*)'),
        )
        if not result:
            return None
        target = result[0]
        try:
            with open(target, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['File', 'Status', 'Score Before', 'Score After', 'Focused Keyword',
                                  'Time Taken (s)', 'Tokens', 'Cost (USD)'])
                for r in rows:
                    writer.writerow([r.get('file'), r.get('status_label'), r.get('score_before'),
                                      r.get('score_after'), r.get('focus_keyword'),
                                      r.get('time_taken'), r.get('tokens'), r.get('cost')])
        except OSError as exc:
            return {'error': str(exc)}
        return target

    # --------------------------------------------------------- processing

    def start_processing(self, settings):
        if self.is_running:
            return {'error': 'A batch is already running.'}

        folder = (settings.get('folder') or '').strip()
        api_key = (settings.get('api_key') or '').strip()
        if not folder or not os.path.isdir(folder):
            return {'error': 'Choose a valid folder to scan first.'}
        if not api_key:
            return {'error': 'Enter your Claude API key first.'}

        files = processor.scan_files(folder, settings.get('file_type', 'Both'))
        if not files:
            return {'error': 'No matching PHP/HTML files were found in that folder.'}

        global _worker
        _stop_event.clear()
        self.is_running = True
        model = MODEL_LABELS.get(settings.get('model_label'), claude_client.DEFAULT_MODEL)
        effort = EFFORT_LABELS.get(settings.get('effort_label'), 'medium')
        site_url = (settings.get('site_url') or '').strip()
        site_context = (settings.get('site_context') or '').strip()

        _worker = threading.Thread(
            target=self._run_batch, args=(files, site_url, site_context, api_key, model, effort),
            daemon=True,
        )
        _worker.start()
        return {'total': len(files)}

    def stop_processing(self):
        _stop_event.set()
        return True

    def _push(self, event_type, data):
        if self.window is None:
            return
        payload = json.dumps({'type': event_type, 'data': data})
        try:
            self.window.evaluate_js('window.onPyEvent(%s)' % payload)
        except Exception:
            pass

    def _run_batch(self, files, site_url, site_context, api_key, model, effort):
        total = len(files)
        try:
            for index, path in enumerate(files, start=1):
                if _stop_event.is_set():
                    self._push('log', {'text': 'Stopped by user.'})
                    break

                name = os.path.basename(path)
                self._push('file_start', {'name': name, 'index': index, 'total': total})

                def on_status(score_before=None, message=None, score_after=None, focus_keyword=None):
                    self._push('status', {
                        'score_before': score_before, 'message': message,
                        'score_after': score_after, 'focus_keyword': focus_keyword,
                    })

                result = processor.process_file(path, site_url, site_context, api_key, model,
                                                 effort, on_status=on_status)
                self._push('file_done', {'result': result, 'index': index, 'total': total})
        finally:
            self.is_running = False
            self._push('batch_done', {})


LOG_PATH = os.path.join(BASE_DIR, 'app_debug.log')


def _log(message):
    """Milestone/error logging so a hang can be diagnosed even when no
    console is visible (e.g. launched by double-click). Check
    app_debug.log next to this file if the window opens but never
    becomes responsive."""
    line = '%s %s' % (time.strftime('%H:%M:%S'), message)
    print(line, flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def main():
    try:
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            f.write('')  # fresh log each run
    except OSError:
        pass
    _log('main() starting - BASE_DIR=%s' % BASE_DIR)

    index_path = os.path.join(BASE_DIR, 'webui', 'index.html')
    styles_path = os.path.join(BASE_DIR, 'webui', 'styles.css')
    for required in (index_path, styles_path):
        if not os.path.exists(required):
            _log('FATAL: required file missing: %s' % required)
            return
    _log('required webui files found OK')

    try:
        api = Api()
        _log('Api created, calling webview.create_window()...')
        window = webview.create_window(
            'AI Page Content Enhancer', url=index_path, js_api=api,
            width=1400, height=880, min_size=(1150, 720), background_color='#F8FAFC',
        )
        api.set_window(window)
        window.events.initialized += lambda: _log('window initialized (WebView2 environment ready)')
        window.events.shown += lambda: _log('window shown')
        window.events.loaded += lambda: _log('page fully loaded (HTML/CSS/JS ready) - if it hangs '
                                              'AFTER this line, the problem is in the running app, '
                                              'not startup')

        def _bridge_probe():
            # Runs regardless of whether events.loaded ever fires, so we can
            # tell apart: (a) evaluate_js itself not working at all (Python
            # -> JS direction broken), (b) app.js never executing (HTML/JS
            # loading itself stuck), (c) app.js ran but window.pywebview (the
            # JS -> Python bridge object) never got injected - the most
            # likely explanation for "window shown" being the last line ever
            # logged: the page renders, but no click handler ever got
            # attached because that only happens inside the pywebviewready
            # handler, which never fires.
            time.sleep(4)
            try:
                probe = window.evaluate_js(
                    "JSON.stringify({readyState: document.readyState, "
                    "appJsLoaded: !!window.__appJsLoaded, "
                    "pywebviewDefined: typeof window.pywebview !== 'undefined'})"
                )
                _log('BRIDGE PROBE result: %s' % probe)
            except Exception:
                _log('BRIDGE PROBE FAILED (evaluate_js itself raised):\n' + traceback.format_exc())

        threading.Thread(target=_bridge_probe, daemon=True).start()
        _log('create_window() returned OK, entering webview.start() main loop...')
        # Deliberately plain webview.start() with no kwargs: debug=True (and
        # private_mode=False) both make pywebview switch from plain file://
        # URLs to serving the app through a local Bottle HTTP server on
        # 127.0.0.1 - a new local network listener that corporate EDR/
        # antivirus is more likely to flag or block than a simple file load,
        # and isn't what this app has actually been tested with.
        webview.start()
        _log('webview.start() returned - window closed normally')
    except Exception:
        _log('FATAL exception:\n' + traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
