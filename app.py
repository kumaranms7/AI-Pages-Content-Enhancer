"""AI Page Content Enhancer - desktop app (Tkinter/customtkinter UI).

Native-widget UI, no browser engine involved. This replaces webview_app.py
(the pywebview + HTML/Tailwind version, kept on disk but disabled for now
because it hung on the user's machine - see webview_app.py's own history/
comments for the root cause that WAS fixed there). This file reuses the
same unchanged backend modules: content_units.py, seo_scorer.py,
claude_client.py, processor.py.
"""
import csv
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image

import claude_client
import processor
import seo_scorer

# Two different base paths once bundled by PyInstaller:
# - APP_DIR: next to the actual .exe, persists across runs - where the
#   user's editable settings file belongs (PyInstaller's bundle contents
#   get re-extracted/are read-only, so writing config there would either
#   fail or silently vanish on the next launch).
# - RESOURCE_DIR: sys._MEIPASS, where --add-data assets were bundled
#   (read-only is fine - logo/icon are never written to, only read).
# Both fall back to the script's own directory when running unfrozen.
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR

BASE_DIR = APP_DIR
CONFIG_PATH = os.path.join(APP_DIR, 'enhancer_config.json')
ASSETS_DIR = os.path.join(RESOURCE_DIR, 'assets')
LOGO_PATH = os.path.join(ASSETS_DIR, 'logo.webp')
ICON_PATH = os.path.join(ASSETS_DIR, 'icon.ico')

# Tailwind-inspired palette (kept consistent with the webview version).
COLOR_PRIMARY = '#7C3AED'
COLOR_PRIMARY_DARK = '#4F46E5'
COLOR_SECONDARY = '#3B82F6'
COLOR_ACCENT = '#EC4899'
COLOR_SUCCESS = '#22C55E'
COLOR_WARNING = '#F59E0B'
COLOR_DANGER = '#EF4444'
COLOR_SIDEBAR = '#1E293B'
COLOR_SIDEBAR_LIGHT = '#334155'
COLOR_BG = '#F1F5F9'
COLOR_CARD = '#FFFFFF'
COLOR_TEXT = '#0F172A'
COLOR_TEXT_MUTED = '#64748B'
COLOR_BORDER = '#E2E8F0'

MODEL_LABELS = {
    'Claude Sonnet 4.6 (recommended)': 'claude-sonnet-4-6',
    'Claude Opus 4.8 (most capable)': 'claude-opus-4-8',
    'Claude Haiku 4.5 (fastest / cheapest)': 'claude-haiku-4-5',
}
EFFORT_LABELS = {
    'Faster / cheaper': 'low',
    'Balanced (recommended)': 'medium',
    'Highest quality': 'high',
}
FILE_TYPES = ('Both', 'PHP', 'HTML')

STATUS_STYLES = {
    'updated': (COLOR_SUCCESS, 'Updated'),
    'error': (COLOR_DANGER, 'Error'),
}


def format_duration(seconds):
    """141.4 -> '2 mins 21 secs' (or '45 secs' under a minute)."""
    total_seconds = round(seconds or 0)
    mins, secs = divmod(total_seconds, 60)
    if mins:
        return '%d min%s %d sec%s' % (mins, '' if mins == 1 else 's', secs, '' if secs == 1 else 's')
    return '%d sec%s' % (secs, '' if secs == 1 else 's')


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def load_sidebar_logo(max_height=80, max_width=300):
    """CTkImage for assets/logo.webp, aspect-scaled to fit the sidebar, or
    None if the file isn't there yet (caller falls back to a text label).
    """
    if not os.path.exists(LOGO_PATH):
        return None
    try:
        img = Image.open(LOGO_PATH)
        img.load()
    except Exception:
        return None
    ratio = min(max_height / img.height, max_width / img.width, 1.0)
    size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


def ensure_window_icon_path():
    """Path to the .ico used for the window/taskbar icon (Tkinter's
    iconbitmap needs .ico on Windows, not webp/png).

    assets/icon.ico is treated as user-provided and is never generated or
    overwritten here - if it exists, it's used as-is, no questions asked.
    It's only auto-derived (cropped from assets/logo.webp) as a temporary
    stand-in when no icon.ico has been placed yet. Returns None if neither
    file exists.
    """
    if os.path.exists(ICON_PATH):
        return ICON_PATH
    if not os.path.exists(LOGO_PATH):
        return None
    try:
        img = Image.open(LOGO_PATH).convert('RGBA')
        side = min(img.width, img.height)
        square = img.crop((
            (img.width - side) // 2, (img.height - side) // 2,
            (img.width - side) // 2 + side, (img.height - side) // 2 + side,
        ))
        fallback_path = os.path.join(ASSETS_DIR, '_logo_derived_icon.ico')
        square.save(fallback_path, format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        return fallback_path
    except Exception:
        return None


class DetailWindow(ctk.CTkToplevel):
    def __init__(self, master, result):
        super().__init__(master)
        self.title(os.path.basename(result['path']))
        self.geometry('560x520')
        self.configure(fg_color=COLOR_BG)
        self.transient(master)

        header = ctk.CTkLabel(self, text=os.path.basename(result['path']),
                               font=ctk.CTkFont(size=16, weight='bold'), text_color=COLOR_TEXT)
        header.pack(anchor='w', padx=20, pady=(20, 4))

        color, label = STATUS_STYLES.get(result['status'], (COLOR_TEXT_MUTED, result['status']))
        sub = ctk.CTkLabel(self, text=label, text_color=color, font=ctk.CTkFont(size=13, weight='bold'))
        sub.pack(anchor='w', padx=20, pady=(0, 12))

        if result.get('error'):
            ctk.CTkLabel(self, text='Error: %s' % result['error'], text_color=COLOR_DANGER,
                         wraplength=500, justify='left').pack(anchor='w', padx=20, pady=(0, 12))

        scores = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=10)
        scores.pack(fill='x', padx=20, pady=(0, 12))
        ctk.CTkLabel(scores, text='Score before -> after', text_color=COLOR_TEXT_MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky='w', padx=14, pady=(10, 0))
        before = result.get('score_before')
        after = result.get('score_after')
        ctk.CTkLabel(scores, text='%s -> %s' % (before if before is not None else '-',
                                                  after if after is not None else '-'),
                     font=ctk.CTkFont(size=20, weight='bold'), text_color=COLOR_PRIMARY_DARK
                     ).grid(row=1, column=0, sticky='w', padx=14, pady=(0, 10))
        if result.get('focus_keyword'):
            ctk.CTkLabel(scores, text='Focus keyword: "%s"' % result['focus_keyword'],
                         text_color=COLOR_TEXT).grid(row=2, column=0, sticky='w', padx=14, pady=(0, 10))

        breakdown = result.get('breakdown_after') or result.get('breakdown_before')
        if breakdown:
            table_frame = ctk.CTkScrollableFrame(self, fg_color=COLOR_CARD, corner_radius=10)
            table_frame.pack(fill='both', expand=True, padx=20, pady=(0, 20))
            before_breakdown = result.get('breakdown_before') or {}
            for row, key in enumerate(seo_scorer.CATEGORY_ORDER):
                label_text = seo_scorer.CATEGORY_LABELS.get(key, key)
                b = before_breakdown.get(key)
                a = breakdown.get(key)
                ctk.CTkLabel(table_frame, text=label_text, text_color=COLOR_TEXT, anchor='w'
                             ).grid(row=row, column=0, sticky='w', padx=10, pady=6)
                ctk.CTkLabel(table_frame, text='%s -> %s' % (b if b is not None else '-', a),
                             text_color=COLOR_TEXT_MUTED, anchor='e'
                             ).grid(row=row, column=1, sticky='e', padx=10, pady=6)
            table_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(self, text='Close', command=self.destroy, fg_color=COLOR_PRIMARY,
                       hover_color=COLOR_PRIMARY_DARK).pack(pady=(0, 16))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode('light')
        self.title('AI Page Content Enhancer')
        self.geometry('1400x880')
        self.minsize(1150, 720)
        self.configure(fg_color=COLOR_BG)

        icon_path = ensure_window_icon_path()
        if icon_path:
            try:
                self.iconbitmap(default=icon_path)
            except tk.TclError:
                pass  # e.g. non-Windows or a malformed .ico - title bar just keeps the default icon

        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.is_running = False
        self.results = []
        self.totals = {'discovered': 0, 'completed': 0, 'errors': 0, 'tokens': 0, 'cost': 0.0}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self._load_settings()
        self.after(100, self._poll_queue)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ------------------------------------------------------------ sidebar

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, fg_color=COLOR_SIDEBAR, corner_radius=0, width=320)
        sidebar.grid(row=0, column=0, sticky='nswe')
        sidebar.grid_propagate(False)

        scroll = ctk.CTkScrollableFrame(sidebar, fg_color='transparent')
        scroll.pack(fill='both', expand=True, padx=16, pady=16)
        # CTkScrollableFrame always creates a CTkScrollbar; there's no public
        # option to omit it (customtkinter 5.2.2). Hiding it via grid_remove()
        # only removes it from view - mousewheel scrolling is bound globally
        # via bind_all("<MouseWheel>", ...) inside customtkinter itself,
        # entirely independent of the scrollbar widget, so scrolling still
        # works with nothing visible to drag.
        scroll._scrollbar.grid_remove()

        self.sidebar_logo_image = load_sidebar_logo()  # kept on self - CTkImage must outlive the widget
        if self.sidebar_logo_image is not None:
            ctk.CTkLabel(scroll, image=self.sidebar_logo_image, text='').pack(anchor='w', pady=(4, 18))
        else:
            ctk.CTkLabel(scroll, text='✨ AI Page Content Enhancer',
                         font=ctk.CTkFont(size=16, weight='bold'),
                         text_color='#FFFFFF', wraplength=270, justify='left'
                         ).pack(anchor='w', pady=(4, 18))

        self._section(scroll, 'SOURCE')
        folder_row = ctk.CTkFrame(scroll, fg_color='transparent')
        folder_row.pack(fill='x', pady=(4, 4))
        self.folder_var = tk.StringVar()
        ctk.CTkEntry(folder_row, textvariable=self.folder_var, placeholder_text='Choose a folder...'
                      ).pack(side='left', fill='x', expand=True)
        ctk.CTkButton(folder_row, text='Browse', width=70, command=self._browse_folder,
                       fg_color=COLOR_SIDEBAR_LIGHT, hover_color=COLOR_PRIMARY
                       ).pack(side='left', padx=(6, 0))

        self._label(scroll, 'File type')
        self.file_type_var = tk.StringVar(value='Both')
        ctk.CTkOptionMenu(scroll, values=list(FILE_TYPES), variable=self.file_type_var,
                          fg_color=COLOR_SIDEBAR_LIGHT, button_color=COLOR_PRIMARY,
                          button_hover_color=COLOR_PRIMARY_DARK).pack(fill='x', pady=(0, 14))

        self._section(scroll, 'SITE DETAILS')
        self._label(scroll, 'Site URL')
        self.site_url_var = tk.StringVar()
        ctk.CTkEntry(scroll, textvariable=self.site_url_var,
                      placeholder_text='https://example.com').pack(fill='x', pady=(0, 10))
        self._label(scroll, 'Site context / what the business does')
        self.site_context_box = ctk.CTkTextbox(scroll, height=70, fg_color=COLOR_SIDEBAR_LIGHT,
                                                text_color='#FFFFFF')
        self.site_context_box.pack(fill='x', pady=(0, 14))

        self._section(scroll, 'CLAUDE API')
        self._label(scroll, 'API key')
        key_row = ctk.CTkFrame(scroll, fg_color='transparent')
        key_row.pack(fill='x', pady=(0, 6))
        self.api_key_var = tk.StringVar()
        self.api_key_entry = ctk.CTkEntry(key_row, textvariable=self.api_key_var, show='*')
        self.api_key_entry.pack(side='left', fill='x', expand=True)
        self._key_visible = False
        self.toggle_key_btn = ctk.CTkButton(key_row, text='Show', width=56, command=self._toggle_key_visible,
                                             fg_color='#0F172A', hover_color='#1E293B')
        self.toggle_key_btn.pack(side='left', padx=(6, 0))
        self.remember_key_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(scroll, text='Remember key on this device', variable=self.remember_key_var,
                         text_color='#CBD5E1', fg_color=COLOR_PRIMARY).pack(anchor='w', pady=(0, 14))

        self._section(scroll, 'CLAUDE MODEL')
        self.model_var = tk.StringVar(value=list(MODEL_LABELS)[0])
        ctk.CTkOptionMenu(scroll, values=list(MODEL_LABELS), variable=self.model_var,
                          fg_color=COLOR_SIDEBAR_LIGHT, button_color=COLOR_PRIMARY,
                          button_hover_color=COLOR_PRIMARY_DARK).pack(fill='x', pady=(0, 14))

        self._section(scroll, 'REWRITE QUALITY')
        self.effort_var = tk.StringVar(value=list(EFFORT_LABELS)[1])
        ctk.CTkOptionMenu(scroll, values=list(EFFORT_LABELS), variable=self.effort_var,
                          fg_color=COLOR_SIDEBAR_LIGHT, button_color=COLOR_PRIMARY,
                          button_hover_color=COLOR_PRIMARY_DARK).pack(fill='x', pady=(0, 14))

        footer = ctk.CTkFrame(scroll, fg_color='#475569', corner_radius=10)
        footer.pack(fill='x', pady=(8, 0))
        ctk.CTkLabel(footer, text='Premium AI Power', text_color='#FFFFFF',
                     font=ctk.CTkFont(size=12, weight='bold')).pack(anchor='w', padx=12, pady=(10, 0))
        ctk.CTkLabel(footer, text='Scores, rewrites and refines content automatically.',
                     text_color='#CBD5E1', font=ctk.CTkFont(size=11), wraplength=260, justify='left'
                     ).pack(anchor='w', padx=12, pady=(2, 10))

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color='#94A3B8',
                     font=ctk.CTkFont(size=11, weight='bold')).pack(anchor='w', pady=(4, 6))

    def _label(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color='#CBD5E1', font=ctk.CTkFont(size=12)
                     ).pack(anchor='w', pady=(0, 4))

    def _toggle_key_visible(self):
        self._key_visible = not self._key_visible
        self.api_key_entry.configure(show='' if self._key_visible else '*')
        self.toggle_key_btn.configure(text='Hide' if self._key_visible else 'Show')

    # --------------------------------------------------------------- main

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color=COLOR_BG, corner_radius=0)
        main.grid(row=0, column=1, sticky='nswe')
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(main, fg_color='transparent')
        header.grid(row=0, column=0, sticky='we', padx=24, pady=(20, 10))
        ctk.CTkLabel(header, text='Content Enhancement Dashboard',
                     font=ctk.CTkFont(size=22, weight='bold'), text_color=COLOR_TEXT).pack(side='left')

        process_card = ctk.CTkFrame(main, fg_color=COLOR_CARD, corner_radius=12)
        process_card.grid(row=1, column=0, sticky='we', padx=24, pady=(0, 14))
        btn_row = ctk.CTkFrame(process_card, fg_color='transparent')
        btn_row.pack(fill='x', padx=18, pady=(16, 8))
        # text_color was left at customtkinter's default (a dim near-white,
        # and an even dimmer gray74 once disabled) - low contrast against
        # saturated purple/red and the actual cause of "hard to see" here,
        # not the fg_color itself. Explicit white + a brighter disabled
        # color fixes that; bigger font/height also helps readability.
        self.start_btn = ctk.CTkButton(btn_row, text='Start Processing', command=self._start_processing,
                                        fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_DARK,
                                        text_color='#FFFFFF', text_color_disabled='#E2E8F0',
                                        font=ctk.CTkFont(size=15, weight='bold'),
                                        width=180, height=44)
        self.start_btn.pack(side='left')
        self.stop_btn = ctk.CTkButton(btn_row, text='Stop', command=self._stop_processing,
                                       fg_color=COLOR_DANGER, hover_color='#DC2626',
                                       text_color='#FFFFFF', text_color_disabled='#E2E8F0',
                                       font=ctk.CTkFont(size=15, weight='bold'),
                                       width=120, height=44, state='disabled')
        self.stop_btn.pack(side='left', padx=(10, 0))

        self.progress_bar = ctk.CTkProgressBar(process_card, progress_color=COLOR_PRIMARY)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill='x', padx=18, pady=(8, 6))

        self.status_label = ctk.CTkLabel(process_card, text='Idle - choose a folder and click Start.',
                                          text_color=COLOR_TEXT_MUTED, anchor='w')
        self.status_label.pack(fill='x', padx=18, pady=(0, 16))

        kpi_row = ctk.CTkFrame(main, fg_color='transparent')
        kpi_row.grid(row=2, column=0, sticky='we', padx=24, pady=(0, 14))
        for i in range(5):
            kpi_row.grid_columnconfigure(i, weight=1)
        self.kpi_values = {}
        kpi_defs = [('discovered', 'Discovered', COLOR_SECONDARY), ('completed', 'Completed', COLOR_SUCCESS),
                    ('errors', 'Errors', COLOR_DANGER), ('tokens', 'Tokens', COLOR_ACCENT),
                    ('cost', 'Est. Cost', COLOR_PRIMARY)]
        for i, (key, label, color) in enumerate(kpi_defs):
            card = ctk.CTkFrame(kpi_row, fg_color=COLOR_CARD, corner_radius=12)
            card.grid(row=0, column=i, sticky='we', padx=(0 if i == 0 else 6, 0))
            ctk.CTkLabel(card, text=label, text_color=COLOR_TEXT_MUTED, font=ctk.CTkFont(size=11)
                         ).pack(anchor='w', padx=14, pady=(12, 0))
            value_label = ctk.CTkLabel(card, text='0', text_color=color,
                                        font=ctk.CTkFont(size=22, weight='bold'))
            value_label.pack(anchor='w', padx=14, pady=(0, 12))
            self.kpi_values[key] = value_label

        actions_row = ctk.CTkFrame(main, fg_color='transparent')
        actions_row.grid(row=3, column=0, sticky='we', padx=24, pady=(0, 6))
        ctk.CTkLabel(actions_row, text='Results', font=ctk.CTkFont(size=15, weight='bold'),
                     text_color=COLOR_TEXT).pack(side='left')
        ctk.CTkButton(actions_row, text='Export CSV', command=self._export_csv, width=110,
                       fg_color=COLOR_SECONDARY, hover_color='#2563EB').pack(side='right')

        table_card = ctk.CTkFrame(main, fg_color=COLOR_CARD, corner_radius=12)
        table_card.grid(row=4, column=0, sticky='nswe', padx=24, pady=(0, 14))
        table_card.grid_rowconfigure(0, weight=1)
        table_card.grid_columnconfigure(0, weight=1)
        self._build_table(table_card)

        log_card = ctk.CTkFrame(main, fg_color='#0F172A', corner_radius=12, height=140)
        log_card.grid(row=5, column=0, sticky='we', padx=24, pady=(0, 10))
        log_card.grid_propagate(False)
        self.log_box = ctk.CTkTextbox(log_card, fg_color='#0F172A', text_color='#94E2D5',
                                       font=ctk.CTkFont(family='Consolas', size=11))
        self.log_box.pack(fill='both', expand=True, padx=10, pady=10)
        self.log_box.configure(state='disabled')

        self.statusbar = ctk.CTkLabel(main, text='Ready', text_color=COLOR_TEXT_MUTED, anchor='w',
                                       font=ctk.CTkFont(size=11))
        self.statusbar.grid(row=6, column=0, sticky='we', padx=24, pady=(0, 10))

    def _build_table(self, parent):
        style = ttk.Style(self)
        style.theme_use('default')
        # Default ttk Treeview font is tiny (~9pt) and there was no override
        # here at all - that's the actual cause of the table being hard to
        # read, not just a general "make things bigger" request. Sized for
        # an older-adult audience: larger row/heading font plus a taller
        # row height so the bigger text isn't cramped or clipped.
        style.configure('Custom.Treeview', background=COLOR_CARD, fieldbackground=COLOR_CARD,
                         foreground=COLOR_TEXT, rowheight=40, borderwidth=0,
                         font=('Segoe UI', 13))
        style.configure('Custom.Treeview.Heading', background='#F8FAFC', foreground=COLOR_TEXT_MUTED,
                         font=('Segoe UI', 13, 'bold'), borderwidth=0)
        style.map('Custom.Treeview',
                  background=[('selected', COLOR_PRIMARY_DARK)],
                  foreground=[('selected', '#FFFFFF')])

        columns = ('file', 'status', 'before', 'after', 'keyword', 'time', 'tokens', 'cost')
        headers = {'file': 'File', 'status': 'Status', 'before': 'Before', 'after': 'After',
                   'keyword': 'Keyword', 'time': 'Time', 'tokens': 'Tokens', 'cost': 'Cost'}
        self.tree = ttk.Treeview(parent, columns=columns, show='headings', style='Custom.Treeview')
        col_widths = {'file': 200, 'time': 150}
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=col_widths.get(col, 120), anchor='w' if col == 'file' else 'center')
        self.tree.grid(row=0, column=0, sticky='nswe', padx=2, pady=2)
        vsb = ttk.Scrollbar(parent, orient='vertical', command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind('<Double-1>', self._on_row_double_click)

    # ------------------------------------------------------------- config

    def _load_settings(self):
        cfg = load_config()
        self.folder_var.set(cfg.get('folder', ''))
        self.file_type_var.set(cfg.get('file_type', 'Both'))
        self.site_url_var.set(cfg.get('site_url', ''))
        self.site_context_box.insert('1.0', cfg.get('site_context', ''))
        self.model_var.set(cfg.get('model_label', list(MODEL_LABELS)[0]))
        self.effort_var.set(cfg.get('effort_label', list(EFFORT_LABELS)[1]))
        if cfg.get('remember_key'):
            self.remember_key_var.set(True)
            self.api_key_var.set(cfg.get('api_key', ''))

    def _save_settings(self):
        cfg = {
            'folder': self.folder_var.get(),
            'file_type': self.file_type_var.get(),
            'site_url': self.site_url_var.get(),
            'site_context': self.site_context_box.get('1.0', 'end').strip(),
            'model_label': self.model_var.get(),
            'effort_label': self.effort_var.get(),
            'remember_key': self.remember_key_var.get(),
            'api_key': self.api_key_var.get() if self.remember_key_var.get() else '',
        }
        save_config(cfg)

    # ------------------------------------------------------------ actions

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)

    def _log(self, text):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', text + '\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def _start_processing(self):
        if self.is_running:
            return
        folder = self.folder_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror('AI Page Content Enhancer', 'Choose a valid folder to scan first.')
            return
        if not api_key:
            messagebox.showerror('AI Page Content Enhancer', 'Enter your Claude API key first.')
            return

        files = processor.scan_files(folder, self.file_type_var.get())
        if not files:
            messagebox.showerror('AI Page Content Enhancer', 'No matching PHP/HTML files were found in that folder.')
            return

        self._save_settings()
        self.results = []
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.totals = {'discovered': len(files), 'completed': 0, 'errors': 0, 'tokens': 0, 'cost': 0.0}
        self._refresh_kpis()
        self.progress_bar.set(0)
        self._log('Starting batch: %d files discovered.' % len(files))

        self.stop_event.clear()
        self.is_running = True
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')

        model = MODEL_LABELS.get(self.model_var.get(), claude_client.DEFAULT_MODEL)
        effort = EFFORT_LABELS.get(self.effort_var.get(), 'medium')
        site_url = self.site_url_var.get().strip()
        site_context = self.site_context_box.get('1.0', 'end').strip()

        self.worker = threading.Thread(
            target=self._run_batch, args=(files, site_url, site_context, api_key, model, effort),
            daemon=True,
        )
        self.worker.start()

    def _stop_processing(self):
        self.stop_event.set()
        self.statusbar.configure(text='Stopping after the current file...')

    def _export_csv(self):
        if not self.results:
            messagebox.showinfo('AI Page Content Enhancer', 'No results to export yet.')
            return
        target = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='seo_results.csv',
                                                filetypes=(('CSV Files', '*.csv'), ('All files', '*.*')))
        if not target:
            return
        try:
            with open(target, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['File', 'Status', 'Before', 'After', 'Keyword Focused'])
                for r in self.results:
                    _, status_label = STATUS_STYLES.get(r['status'], (None, r['status']))
                    writer.writerow([r['path'], status_label, r.get('score_before'), r.get('score_after'),
                                      r.get('focus_keyword')])
        except OSError as exc:
            messagebox.showerror('AI Page Content Enhancer', 'Could not save file: %s' % exc)

    def _on_row_double_click(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        if 0 <= index < len(self.results):
            DetailWindow(self, self.results[index])

    def _on_close(self):
        self.stop_event.set()
        self._save_settings()
        self.destroy()

    # --------------------------------------------------------- worker thread

    def _run_batch(self, files, site_url, site_context, api_key, model, effort):
        total = len(files)
        try:
            for index, path in enumerate(files, start=1):
                if self.stop_event.is_set():
                    self.event_queue.put({'type': 'log', 'text': 'Stopped by user.'})
                    break

                name = os.path.basename(path)
                self.event_queue.put({'type': 'file_start', 'name': name, 'index': index, 'total': total})

                def on_status(score_before=None, message=None, score_after=None, focus_keyword=None):
                    self.event_queue.put({'type': 'status', 'message': message})

                result = processor.process_file(path, site_url, site_context, api_key, model,
                                                 effort, on_status=on_status)
                self.event_queue.put({'type': 'file_done', 'result': result, 'index': index, 'total': total})
        finally:
            self.event_queue.put({'type': 'batch_done'})

    # ------------------------------------------------------------- queue

    def _poll_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_event(self, event):
        etype = event['type']
        if etype == 'log':
            self._log(event['text'])
        elif etype == 'file_start':
            self.statusbar.configure(text='Processing %s (%d/%d)...' % (event['name'], event['index'], event['total']))
        elif etype == 'status':
            if event.get('message'):
                self.status_label.configure(text=event['message'])
        elif etype == 'file_done':
            self._on_file_done(event['result'], event['index'], event['total'])
        elif etype == 'batch_done':
            self._on_batch_done()

    def _on_file_done(self, result, index, total):
        self.results.append(result)
        color, status_label = STATUS_STYLES.get(result['status'], (COLOR_TEXT_MUTED, result['status']))
        self.tree.insert('', 'end', values=(
            os.path.basename(result['path']), status_label,
            result.get('score_before', '-'), result.get('score_after', '-'),
            result.get('focus_keyword') or '-', format_duration(result.get('time_taken_sec', 0)),
            result.get('total_tokens', 0), '$%.4f' % result.get('cost_usd', 0),
        ))
        self.totals['completed'] += 1
        if result['status'] == 'error':
            self.totals['errors'] += 1
        self.totals['tokens'] += result.get('total_tokens', 0)
        self.totals['cost'] += result.get('cost_usd', 0)
        self._refresh_kpis()
        self.progress_bar.set(index / total if total else 0)
        self._log('[%d/%d] %s -> %s' % (index, total, os.path.basename(result['path']), status_label))

    def _on_batch_done(self):
        self.is_running = False
        self.start_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        self.status_label.configure(text='Batch complete.')
        self.statusbar.configure(text='Done - %d processed, %d errors.' % (self.totals['completed'], self.totals['errors']))
        self._log('Batch finished.')

    def _refresh_kpis(self):
        self.kpi_values['discovered'].configure(text=str(self.totals['discovered']))
        self.kpi_values['completed'].configure(text=str(self.totals['completed']))
        self.kpi_values['errors'].configure(text=str(self.totals['errors']))
        self.kpi_values['tokens'].configure(text=str(self.totals['tokens']))
        self.kpi_values['cost'].configure(text='$%.2f' % self.totals['cost'])


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
