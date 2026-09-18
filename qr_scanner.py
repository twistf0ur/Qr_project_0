"""ScanCalc - a barcode / QR scanner with a dark, technical ttkbootstrap UI.

The capture engine (OpenCV + pyzbar inside a worker thread) is unchanged; the
ScanCalc shell is layered on top of it:

    * segmented scan modes          BARCODE / QR / BATCH
    * live status chip row          FPS, sensor resolution, auto-exposure, zoom
    * camera viewport               rounded panel with a live detect overlay
    * product card                  name / price / size / aisle + ADD
    * running calculation           running total, item tally, tax estimate
    * budget tracker                remaining budget and a usage bar
    * recently scanned list         qty steppers, per-line price, clear history
    * bottom navigation             SCAN / CART / HISTORY / BUDGET
"""

import math
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winsound  # built-in on Windows - plays the .wav beep with no extra packages
from collections import deque
from datetime import datetime

import cv2
import ttkbootstrap as tb
from PIL import Image, ImageTk
from pyzbar.pyzbar import ZBarSymbol, decode
from ttkbootstrap.constants import BOTH, E, EW, LEFT, N, W, X, YES
from ttkbootstrap.style import Colors, ThemeDefinition

sound_file_path = os.path.join(os.path.dirname(__file__), 'beep-01a.wav')

THEME_NAME = 'scancalc'

# --------------------------------------------------------------- palette ---
BG = '#0a0e12'          # window / page background
SURFACE = '#111a20'     # card background
SURFACE_2 = '#16222a'   # raised rows inside cards
SURFACE_3 = '#1d2b34'   # chips and inactive segments
BORDER = '#1e2c35'
FG = '#e9f2f0'
MUTED = '#7c8c94'
ACCENT = '#3ddc84'      # the mockup's green
ACCENT_DEEP = '#2bb96c'
INFO = '#5ad1ff'
WARN = '#ffb86b'
DANGER = '#ff6b6b'

# ---------------------------------------------------------------- config ---
TAX_RATE = 0.07          # displayed as "TAX EST."
DEFAULT_BUDGET = 50.00
MAX_HISTORY = 50         # rows kept in the recently-scanned list

# Scan modes: label -> pyzbar symbol types accepted. None means "accept all".
SCAN_MODES = (
    ('BARCODE', (
        'EAN13', 'EAN8', 'UPCA', 'UPCE', 'CODE128', 'CODE39', 'CODE93',
        'I25', 'CODABAR', 'DATABAR', 'DATABAR_EXP',
    )),
    ('QR', ('QRCODE',)),
    ('BATCH', None),
)

# Everything zbar is able to report - used by BATCH mode, which accepts all.
ALL_SYMBOLS = tuple(
    member.name for member in ZBarSymbol
    if member not in (ZBarSymbol.NONE, ZBarSymbol.PARTIAL)
)


def symbol_enums(names):
    """Map pyzbar symbol *names* to ZBarSymbol members (unknown names dropped).

    pyzbar's decode() only accepts ZBarSymbol members - handing it strings
    raises ArgumentError, so every mode list is converted before use.
    """
    if not names:
        return None
    wanted = []
    for name in names:
        member = ZBarSymbol.__members__.get(name.upper())
        if member is not None and member not in wanted:
            wanted.append(member)
    return wanted or None

# --------------------------------------------------------------- catalog ---
# Offline demo catalogue. Anything not listed here is still accepted - see
# guess_product() - so the running calculation works for real scans too.
CATALOG = {
    '0735005653195': ('Sparkling Water Lime', 4.99, '32 fl oz', 'Aisle 4'),
    '0735005653218': ('Sparkling Water Grape', 4.99, '32 fl oz', 'Aisle 4'),
    '012000161155': ('Cola Classic', 2.49, '12 fl oz', 'Aisle 2'),
    '049000042528': ('Ginger Ale', 2.19, '12 fl oz', 'Aisle 2'),
    '028400090896': ('Kettle Chips Sea Salt', 3.79, '8 oz', 'Aisle 7'),
    '041190015238': ('Whole Grain Bread', 3.29, '24 oz', 'Bakery'),
    '070470074091': ('Greek Yoghurt Plain', 5.49, '32 oz', 'Dairy'),
    '030000010959': ('Toothpaste Fresh Mint', 4.29, '6 oz', 'Aisle 11'),
    '088396003151': ('Notebook A5 Dotted', 6.99, '160 pages', 'Stationery'),
    '5060466517814': ('USB-C Cable 1m', 9.99, 'Braided', 'Electronics'),
}


def guess_product(code):
    """Return (name, price, size, aisle, estimated) for a scanned code."""
    entry = CATALOG.get(code)
    if entry is not None:
        name, price, size, aisle = entry
        return name, price, size, aisle, False
    # Deterministic, clearly-flagged estimate so unknown codes still tally up.
    price = 1.99 + (sum(code.encode('utf-8')) * 7 % 900) / 100.0
    name = 'Unlisted Item {}'.format(code[-4:]) if len(code) >= 4 else 'Unlisted Item'
    return name, round(price, 2), 'no data', 'Unmapped', True


def money(value):
    """Format a number as a price string."""
    return '${:,.2f}'.format(value)


def symbol_type(raw_type):
    """pyzbar reports e.g. 'EAN13'; the overlay shows 'UPC-A' style labels."""
    return {
        'UPCA': 'UPC-A', 'UPCE': 'UPC-E', 'EAN13': 'EAN-13', 'EAN8': 'EAN-8',
        'QRCODE': 'QR', 'CODE128': 'CODE-128', 'CODE39': 'CODE-39',
        'CODE93': 'CODE-93', 'I25': 'ITF', 'ITF': 'ITF', 'CODABAR': 'CODABAR',
        'DATABAR': 'GS1', 'DATABAR_EXP': 'GS1-EXP',
    }.get(raw_type, raw_type)


# ------------------------------------------------------- capture engine ----
class CameraEngine:
    """Owns the camera and decodes symbols off the UI thread.

    Grabbing never waits for decoding: only the freshest frame is offered to
    the decoder each iteration, which keeps the preview at full rate and stops
    a slow decode from backing the frame queue up.
    """

    def __init__(self, frame_queue, result_queue):
        self.frame_queue = frame_queue
        self.result_queue = result_queue
        self.capture = None
        self.backend = 'none'
        self.width, self.height = 0, 0
        self.error = None
        self.decoder_started = False
        self._thread = None
        self._stop = threading.Event()
        self._symbols = None
        self._pending = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='scancalc-camera',
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def set_symbols(self, symbols):
        """Restrict recognition to a tuple of pyzbar symbol names (None = all)."""
        with self._lock:
            self._symbols = symbol_enums(symbols)

    def submit(self, frame):
        """Offer a fresh frame to the decoder; only the newest one is kept."""
        with self._lock:
            self._pending = frame

    # -- worker ------------------------------------------------------------
    def _open(self):
        """Try the default backend, then DSHOW/MSMF, verifying pyzbar works."""
        for api, label in ((cv2.CAP_ANY, 'default'), (cv2.CAP_DSHOW, 'dshow'),
                           (cv2.CAP_MSMF, 'msmf')):
            capture = cv2.VideoCapture(0, api)
            if not capture.isOpened():
                capture.release()
                continue
            ok, frame = capture.read()
            if not ok or frame is None:
                capture.release()
                continue
            try:
                decode(frame)
            except Exception as exc:                      # pragma: no cover
                self.error = 'pyzbar: {}'.format(exc)
                capture.release()
                return None
            self.decoder_started = True
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.backend = label
            self.error = None
            return capture
        return None

    def _run(self):
        self.capture = self._open()
        if self.capture is None:
            if self.error is None:
                self.error = 'no camera on index 0'
            return
        self.width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        self.height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
        misses = 0
        while not self._stop.is_set():
            ok, frame = self.capture.read()
            if not ok or frame is None:
                misses += 1
                if misses == 25:
                    self.error = self.error or 'camera stopped returning frames'
                time.sleep(0.03)
                continue
            misses = 0
            self.submit(frame.copy())
            if not self.frame_queue.full():
                self.frame_queue.put(frame)

        # drain the decoder and release the device
        with self._lock:
            self._pending = None
        try:
            self.capture.release()
        finally:
            self.capture = None

    def decode_loop(self, running, symbol_types, min_interval=0.05):
        """UI-thread tick that decodes the most recent frame and reports hits."""
        if not running() or not self.decoder_started:
            return
        with self._lock:
            frame = self._pending
            self._pending = None
            symbols = self._symbols
        if frame is None:
            return
        try:
            found = decode(frame, symbols=symbols)
        except Exception as exc:                          # pragma: no cover
            self.error = 'decode: {}'.format(exc)
            return
        now = time.time()
        for item in found:
            try:
                text = item.data.decode('utf-8')
            except UnicodeDecodeError:
                text = item.data.decode('latin-1', 'replace')
            if not text:
                continue
            rect = item.rect
            points = [(p.x, p.y) for p in getattr(item, 'polygon', [])]
            payload = {
                'text': text,
                'type': item.type,
                'rect': (rect.left, rect.top, rect.width, rect.height),
                'polygon': points,
                'at': now,
            }
            if not symbol_types or item.type in symbol_types:
                self.result_queue.put(payload)


# ------------------------------------------------------- nav button --------
class NavButton(tk.Canvas):
    """Bottom-navigation button: icon over label, accent when selected."""

    def __init__(self, parent, icon, label, command, fonts, color=MUTED,
                 stretch=True):
        super().__init__(parent, highlightthickness=0, borderwidth=0,
                         background=outer_bg(parent), height=52)
        self._icon, self._label, self._command = icon, label, command
        self._fonts = fonts
        self._color = color
        self._stretch = stretch
        self._selected = False
        self._last_w = 0
        self._hover = False
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<Button-1>', lambda _e: self._layout(ACCENT))
        self.bind('<ButtonRelease-1>', self._on_release)
        if stretch:
            self.bind('<Configure>', self._on_configure)

    def set_selected(self, selected):
        self._selected = bool(selected)
        self._layout()

    def is_selected(self):
        return self._selected

    def _on_enter(self, _event=None):
        self._hover = True
        self._layout()

    def _on_leave(self, _event=None):
        self._hover = False
        self._layout()

    def _on_configure(self, event):
        if event.width > 1 and event.width != self._last_w:
            self._last_w = event.width
            self._layout()

    def _on_release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._layout()
        if inside and self._command is not None:
            self._command()

    def _layout(self, override=None):
        self.delete('all')
        width = self._last_w if (self._stretch and self._last_w > 1) else 90
        height = 52
        if self._selected:
            color = ACCENT
        elif override:
            color = override
        elif self._hover:
            color = FG
        else:
            color = self._color
        if self._selected:
            round_rect(self, width / 2 - 12, 4, width / 2 + 12, 7, 2, fill=ACCENT,
                       outline='', tags='bg')
        self.create_text(width / 2, 24, text=self._icon, fill=color,
                         font=self._fonts.i(13), tags='bg')
        self.create_text(width / 2, 41, text=self._label, fill=color,
                         font=self._fonts.d(8, 'bold'), tags='bg')


# ------------------------------------------------------------- the app -----
class ScanCalcApp:
    """Dark ScanCalc shell around the OpenCV + pyzbar capture engine."""

    def __init__(self, root):
        self.root = root
        self.fonts = Fonts()
        self.frames = queue.Queue(maxsize=2)
        self.results = queue.Queue()
        self.engine = CameraEngine(self.frames, self.results)

        # session state
        self.mode_index = 0
        self.mode_symbols = set(SCAN_MODES[0][1])
        self.scanning = True
        self.multi_scan = False
        self.auto_exposure = True
        self.lamp_on = False
        self.beep_enabled = True
        self.zoom_steps = (1.0, 1.25, 1.5, 2.0, 3.0)
        self.zoom_index = 2                       # 1.5x, matching the mockup
        self.history = deque(maxlen=MAX_HISTORY)  # scanned lines, newest first
        self.budget = DEFAULT_BUDGET
        self.pending_code = None
        self.last_payload = None
        self.last_code = None
        self.last_scan_at = 0.0
        self.overlay_until = 0.0
        self.duplicate_window = 2.5
        self.fps = 0.0
        self.page_tab = 'SCAN'
        self.rows = {}
        self._frame_times = deque(maxlen=30)
        self._photo = None
        self._frame_size = (0, 0)
        self._crop_origin = (0, 0)
        self._overlay_shown = False
        self.last_source = None
        self._closing = False
        self._dirty = True
        self._flash_until = 0.0
        self._last_chip_refresh = 0.0
        self._last_viewport_draw = 0.0

        self._build_shell()
        self._build_page()
        self._bind_shortcuts()
        self._refresh_totals()
        self._refresh_chips()
        self._refresh_history()
        self.engine.set_symbols(self.mode_symbols)
        self.engine.start()
        self.root.after(60, self._tick)

    @property
    def zoom(self):
        return self.zoom_steps[self.zoom_index]

    # ---------------------------------------------------------- shell ------
    def _build_shell(self):
        """Pinned bottom navigation plus the scrollable page above it."""
        self.root.configure(background=BG)

        nav_bar = tk.Frame(self.root, background=BG)
        nav_bar.pack(side='bottom', fill=X)
        tk.Frame(nav_bar, background=BORDER, height=1).pack(fill=X)

        nav_row = tk.Frame(nav_bar, background=BG)
        nav_row.pack(fill=X, pady=(6, 8), padx=10)
        nav_items = (
            ('SCAN', '\ue8b2', lambda: self._nav('SCAN')),
            ('CART', '\ue7bf', lambda: self._nav('CART')),
            ('HISTORY', '\ue81c', lambda: self._nav('HISTORY')),
            ('BUDGET', '\ue7ba', lambda: self._nav('BUDGET')),
        )
        self.nav_buttons = {}
        for label, icon, command in nav_items:
            button = NavButton(nav_row, icon, label, command, self.fonts)
            button.pack(side='left', fill=X, expand=YES)
            self.nav_buttons[label] = button
        self.nav_buttons['SCAN'].set_selected(True)

        # ScrolledFrame is itself the content frame - children pack into it.
        self.sf = tb.ScrolledFrame(master=self.root, bootstyle='primary',
                                   auto_hide=True)
        self.sf.pack(side='top', fill=BOTH, expand=YES)

    def _bind_shortcuts(self):
        bindings = (
            ('<Escape>', lambda _e: self._toggle_scan()),
            ('<space>', lambda _e: self._space_key()),
            ('<Control-plus>', lambda _e: self._cycle_zoom(1)),
            ('<Control-equal>', lambda _e: self._cycle_zoom(1)),
            ('<Control-minus>', lambda _e: self._cycle_zoom(-1)),
            ('<Control-m>', lambda _e: self._toggle_multi()),
            ('<Control-l>', lambda _e: self._focus_manual()),
            ('<Control-r>', lambda _e: self._reset_session()),
            ('<Control-Key-1>', lambda _e: self._set_mode(0)),
            ('<Control-Key-2>', lambda _e: self._set_mode(1)),
            ('<Control-Key-3>', lambda _e: self._set_mode(2)),
            ('<Control-MouseWheel>', self._on_ctrl_wheel),
        )
        for sequence, handler in bindings:
            self.root.bind(sequence, handler)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    def _space_key(self):
        """Space toggles scanning, unless the caret is in the manual entry."""
        if self.root.focus_get() is self.manual_entry:
            return
        self._toggle_scan()

    def _on_ctrl_wheel(self, event):
        self._cycle_zoom(1 if event.delta > 0 else -1)
        return 'break'

    # ----------------------------------------------------------- page ------
    def _build_page(self):
        self.page = tk.Frame(self.sf, background=BG)
        self.page.pack(fill=BOTH, expand=YES, padx=16, pady=(14, 10))
        self._build_header(self.page)
        self._build_chip_row(self.page)
        self._build_viewport(self.page)
        self._build_product_card(self.page)
        self._build_action_row(self.page)
        self._build_totals(self.page)
        self._build_budget(self.page)
        self._build_history(self.page)

    def _icon_button(self, parent, icon, command, fill=SURFACE_3, fg=MUTED,
                     selected=False, tooltip=None):
        """Small round icon toggle used in the header and status row."""
        button = PillButton(
            parent, text='', command=command, fill=fill, fg=fg,
            hover_fill=SURFACE_2, hover_fg=FG, active_fill=ACCENT, active_fg=BG,
            icon=icon, icon_font=self.fonts.i(11), radius=15, padx=11, pady=8,
            outline=None)
        button.set_selected(selected)
        if tooltip:
            tb.ToolTip(button, text=tooltip, bootstyle='primary')
        return button

    def _build_header(self, parent):
        header = tk.Frame(parent, background=BG)
        header.pack(fill=X)

        logo = tk.Canvas(header, width=36, height=36, background=BG,
                         highlightthickness=0)
        logo.pack(side='left')
        round_rect(logo, 1, 1, 35, 35, 11, fill=ACCENT, outline='')
        for x0, x1, y0, y1 in ((10, 12, 10, 26), (15, 17, 10, 26),
                               (20, 21, 10, 26), (24, 26, 13, 23)):
            logo.create_rectangle(x0, y0, x1, y1, fill=BG, outline='')

        titles = tk.Frame(header, background=BG)
        titles.pack(side='left', padx=(10, 0))
        tk.Label(titles, text='SCANCALC', background=BG, foreground=FG,
                 font=self.fonts.d(13, 'bold')).pack(anchor=W)
        tk.Label(titles, text='OFFLINE POINT-OF-SCAN', background=BG,
                 foreground=MUTED, font=self.fonts.m(7)).pack(anchor=W)

        # segmented scan-mode control
        segment = RoundedCard(header, fill=SURFACE_3, radius=15, padding=(4, 4),
                              outline=BORDER)
        segment.pack(side='left', fill=X, expand=YES, padx=14)
        self.mode_buttons = []
        for index, (label, _symbols) in enumerate(SCAN_MODES):
            button = PillButton(
                segment.body, text=label, command=lambda i=index: self._set_mode(i),
                fill=SURFACE_3, fg=MUTED, hover_fill=SURFACE_2, hover_fg=FG,
                active_fill=ACCENT, active_fg=BG, font=self.fonts.d(8, 'bold'),
                radius=12, padx=10, pady=6, stretch=True)
            button.pack(side='left', fill=X, expand=YES, padx=1)
            self.mode_buttons.append(button)
        self.mode_buttons[0].set_selected(True)

        self.sound_button = self._icon_button(
            header, '\ue767', self._toggle_sound, selected=False,
            tooltip='Beep on detect (Ctrl+B off)')
        self.sound_button.pack(side='right', padx=(6, 0))
        self.lamp_button = self._icon_button(
            header, '\ue706', self._toggle_lamp,
            tooltip='Torch / auto-exposure assist')
        self.lamp_button.pack(side='right')

    def _build_chip_row(self, parent):
        row = tk.Frame(parent, background=BG)
        row.pack(fill=X, pady=(12, 0))

        self.fps_chip = Chip(row, text='-- FPS OPTICAL', fill=SURFACE_2,
                             fg=ACCENT, dot=ACCENT, font=self.fonts.m(8))
        self.fps_chip.pack(side='left')
        self.sensor_chip = Chip(row, text='-- x --', fill=SURFACE_2, fg=MUTED,
                                font=self.fonts.m(8))
        self.sensor_chip.pack(side='left', padx=(6, 0))

        self.exposure_button = PillButton(
            row, text='AUTO-EXPOSURE: ON', command=self._toggle_exposure,
            fill=SURFACE_2, fg=MUTED, hover_fill=SURFACE_3, hover_fg=FG,
            active_fill=SURFACE_3, active_fg=ACCENT, font=self.fonts.m(8),
            radius=13, padx=11, pady=7, icon='\ue9d9', icon_font=self.fonts.i(9),
            outline=BORDER)
        self.exposure_button.pack(side='left', padx=(6, 0))
        self.exposure_button.set_selected(True)

        self.zoom_button = PillButton(
            row, text='1.5x', command=self._cycle_zoom,
            fill=SURFACE_2, fg=FG, hover_fill=SURFACE_3, hover_fg=ACCENT,
            active_fill=ACCENT, active_fg=BG, font=self.fonts.m(8),
            radius=13, padx=10, pady=7, outline=BORDER)
        self.zoom_button.pack(side='right')

        self.mode_chip = Chip(row, text='ALL SYMBOLS', fill=SURFACE_2, fg=MUTED,
                              font=self.fonts.m(8))
        self.mode_chip.pack(side='right', padx=(0, 6))

    # ------------------------------------------------------- viewport ------
    def _build_viewport(self, parent):
        self.viewport = tk.Canvas(parent, background=BG, highlightthickness=0,
                                  height=330)
        self.viewport.pack(fill=X, pady=(12, 0))
        self.viewport.bind('<Configure>', lambda _event: self._draw_viewport())
        self._photo_id = None

    def _image_rect(self):
        """Where the frame is drawn inside the viewport (letterboxed)."""
        width, height = self.viewport.winfo_width(), self.viewport.winfo_height()
        frame_w, frame_h = self._frame_size
        if width <= 4 or height <= 4 or not frame_w or not frame_h:
            return None
        scale = min(width / float(frame_w), height / float(frame_h))
        draw_w, draw_h = int(frame_w * scale), int(frame_h * scale)
        return ((width - draw_w) // 2, (height - draw_h) // 2, draw_w, draw_h)

    def _overlay_visible(self):
        return (self.last_payload is not None and self.scanning
                and time.time() < self.overlay_until)

    def _draw_viewport(self):
        """Paint the rounded viewport, the live frame and the scan overlay."""
        canvas = self.viewport
        canvas.delete('all')
        self._photo_id = None
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 4 or height <= 4:
            return

        round_rect(canvas, 1, 1, width - 2, height - 2, 18, fill='#05080a',
                   outline=ACCENT if time.time() < self._flash_until else BORDER,
                   width=2 if time.time() < self._flash_until else 1)

        rect = self._image_rect()
        if rect and self._photo is not None:
            x0, y0, draw_w, draw_h = rect
            self._photo_id = canvas.create_image(x0, y0, anchor='nw',
                                                 image=self._photo)
        else:
            self._draw_no_signal(width, height)
            rect = (int(width * 0.03), int(height * 0.06), int(width * 0.94),
                    int(height * 0.88))

        self._draw_brackets(canvas, rect)
        if self._overlay_visible():
            self._draw_overlay(canvas, rect)
        if not self.scanning:
            self._draw_paused(width, height)

    def _draw_no_signal(self, width, height):
        canvas = self.viewport
        message = self.engine.error or 'waiting for first frame'
        canvas.create_text(width / 2, height / 2 - 12, text='\ue722',
                           fill=MUTED, font=self.fonts.i(24))
        canvas.create_text(width / 2, height / 2 + 16, text='CAMERA OFFLINE',
                           fill=FG, font=self.fonts.d(12, 'bold'))
        canvas.create_text(width / 2, height / 2 + 36, text=message, fill=MUTED,
                           font=self.fonts.m(8))

    def _draw_brackets(self, canvas, rect, length=26, inset=10):
        """The four corner brackets of the scan overlay."""
        x0, y0, draw_w, draw_h = rect
        x1, y1 = x0 + draw_w, y0 + draw_h
        x0, y0 = x0 + inset, y0 + inset
        x1, y1 = x1 - inset, y1 - inset
        colour = ACCENT if self.scanning else WARN
        for (sx, sy, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                                 (x0, y1, 1, -1), (x1, y1, -1, -1)):
            canvas.create_line(sx, sy, sx + dx * length, sy, fill=colour, width=2)
            canvas.create_line(sx, sy, sx, sy + dy * length, fill=colour, width=2)

    def _draw_paused(self, width, height):
        canvas = self.viewport
        round_rect(canvas, width / 2 - 76, height / 2 - 15, width / 2 + 76,
                   height / 2 + 15, 15, fill=SURFACE_2, outline=WARN, width=1)
        canvas.create_text(width / 2, height / 2,
                           text='SCAN PAUSED  \u2022  SPACE TO RESUME', fill=WARN,
                           font=self.fonts.m(8))

    def _draw_overlay(self, canvas, rect):
        """Detect chip, detection box and the decoded payload read-out."""
        x0, y0, draw_w, draw_h = rect
        payload = self.last_payload
        label, price, _size, _aisle, estimated = guess_product(payload['text'])
        accent = WARN if estimated else ACCENT
        title = '{} \u2022 {}'.format(
            symbol_type(payload['type']),
            'UNLISTED EST.' if estimated else 'VALID DETECT')

        # the raw detection box, mapped from frame space into the viewport
        frame_w, frame_h = self._frame_size
        if frame_w and frame_h and payload.get('rect'):
            scale_x, scale_y = draw_w / float(frame_w), draw_h / float(frame_h)
            left, top, box_w, box_h = payload['rect']
            offset_x, offset_y = self._crop_origin   # zoom crop, if any
            canvas.create_rectangle(x0 + (left - offset_x) * scale_x,
                                    y0 + (top - offset_y) * scale_y,
                                    x0 + (left + box_w - offset_x) * scale_x,
                                    y0 + (top + box_h - offset_y) * scale_y,
                                    outline=accent, width=2, dash=(4, 3))

        panel_w = min(max(int(draw_w * 0.86), 220), 520)
        panel_h = 92
        px0 = x0 + (draw_w - panel_w) // 2
        py0 = y0 + draw_h - panel_h - 18
        round_rect(canvas, px0, py0, px0 + panel_w, py0 + panel_h, 14,
                   fill=SURFACE, outline=accent, width=1)

        chip_w = 22 + tkfont.Font(font=self.fonts.m(8)).measure(title)
        round_rect(canvas, px0 + 14, py0 + 12, px0 + 14 + chip_w, py0 + 34, 11,
                   fill=accent, outline='')
        canvas.create_text(px0 + 14 + chip_w / 2, py0 + 23, text=title, fill=BG,
                           font=self.fonts.m(8))
        canvas.create_text(px0 + panel_w - 14, py0 + 23, text=money(price),
                           anchor=E, fill=FG, font=self.fonts.d(10, 'bold'))

        # the decoded payload, straight from the external barcode - nothing
        # is generated here, the scanner only reads what the camera sees
        text = payload['text']
        if len(text) > 46:
            text = text[:43] + '...'
        canvas.create_text(px0 + 14, py0 + 46, anchor=W, text=text, fill=FG,
                           font=self.fonts.m(11, 'bold'))
        canvas.create_text(px0 + 14, py0 + panel_h - 10, text=label, anchor=W,
                           fill=MUTED, font=self.fonts.m(8))
        canvas.create_text(px0 + panel_w - 14, py0 + panel_h - 10,
                           text='{} CHARS'.format(len(payload['text'])),
                           anchor=E, fill=MUTED, font=self.fonts.m(8))

    # ---------------------------------------------------- product card -----
    def _build_product_card(self, parent):
        self.product_card = RoundedCard(parent, fill=SURFACE, radius=18,
                                        padding=(16, 14))
        self.product_card.pack(fill=X, pady=(14, 0))
        body = self.product_card.body
        body.grid_columnconfigure(1, weight=1)

        self.tile = tk.Canvas(body, width=48, height=48, background=SURFACE,
                              highlightthickness=0)
        self.tile.grid(row=0, column=0, rowspan=2, sticky=N, padx=(0, 14))

        self.product_name = tk.Label(body, text='AWAITING SCAN', background=SURFACE,
                                     foreground=MUTED, font=self.fonts.d(13, 'bold'),
                                     anchor=W, justify=LEFT)
        self.product_name.grid(row=0, column=1, sticky=EW)
        self.product_meta = tk.Label(body, text='point a barcode at the camera',
                                     background=SURFACE, foreground=MUTED,
                                     font=self.fonts.m(8), anchor=W, justify=LEFT)
        self.product_meta.grid(row=1, column=1, sticky=EW, pady=(2, 0))

        self.aisle_chip = Chip(body, text='NO AISLE', fill=SURFACE_3, fg=MUTED,
                               font=self.fonts.d(8, 'bold'), radius=11, padx=10,
                               pady=4)
        self.aisle_chip.grid(row=2, column=1, sticky=W, pady=(8, 0))

        right = tk.Frame(body, background=SURFACE)
        right.grid(row=0, column=2, rowspan=3, sticky=E, padx=(12, 0))
        self.product_price = tk.Label(right, text='$0.00', background=SURFACE,
                                      foreground=MUTED, font=self.fonts.d(19, 'bold'),
                                      anchor=E)
        self.product_price.pack(anchor=E)
        self.add_button = PillButton(
            right, text='ADD', command=self._add_current, fill=ACCENT, fg=BG,
            hover_fill=ACCENT_DEEP, hover_fg=BG, active_fill=ACCENT_DEEP,
            active_fg=BG, font=self.fonts.d(9, 'bold'), radius=13, padx=18,
            pady=7, icon='\ue710', icon_font=self.fonts.i(11))
        self.add_button.pack(anchor=E, pady=(8, 0))
        self._draw_tile(MUTED)
        self._set_product(None)

    def _draw_tile(self, colour):
        """The little rounded product tile at the left of the card."""
        canvas = self.tile
        canvas.delete('all')
        round_rect(canvas, 1, 1, 47, 47, 14, fill=SURFACE_3, outline='')
        canvas.create_text(24, 24, text='\ue7bf', fill=colour,
                           font=self.fonts.i(17))

    def _set_product(self, product):
        """Show `product` (name, price, size, aisle, estimated) or the placeholder."""
        if product is None:
            self.product_name.configure(text='AWAITING SCAN', foreground=MUTED)
            self.product_meta.configure(text='point a barcode at the camera')
            self.product_price.configure(text='$0.00', foreground=MUTED)
            self.aisle_chip.update_chip(text='NO AISLE', fill=SURFACE_3, fg=MUTED)
            self.add_button.set_state('disabled')
            self._draw_tile(MUTED)
            return
        name, price, size, aisle, estimated = product
        self.product_name.configure(text=name,
                                    foreground=WARN if estimated else FG)
        tax_note = 'Estimate - unlisted code' if estimated else 'Tax Included'
        self.product_meta.configure(text='{} \u2022 {}'.format(size, tax_note))
        self.product_price.configure(text=money(price), foreground=FG)
        self.aisle_chip.update_chip(text=aisle, fill=SURFACE_3,
                                    fg=WARN if estimated else ACCENT)
        self.add_button.set_state('normal')
        self._draw_tile(WARN if estimated else ACCENT)

    # -------------------------------------------------------- actions ------
    def _build_action_row(self, parent):
        row = tk.Frame(parent, background=BG)
        row.pack(fill=X, pady=(12, 0))
        self.manual_button = PillButton(
            row, text='ENTER CODE MANUALLY', command=self._toggle_manual,
            fill=SURFACE, fg=FG, hover_fill=SURFACE_2, hover_fg=ACCENT,
            active_fill=ACCENT, active_fg=BG, font=self.fonts.d(9, 'bold'),
            radius=15, padx=14, pady=10, icon='\ue765',
            icon_font=self.fonts.i(11), outline=BORDER, stretch=True)
        self.manual_button.pack(side='left', fill=X, expand=YES, padx=(0, 6))

        self.multi_button = PillButton(
            row, text='MULTI-SCAN', command=self._toggle_multi, fill=SURFACE,
            fg=MUTED, hover_fill=SURFACE_2, hover_fg=FG, active_fill=ACCENT,
            active_fg=BG, font=self.fonts.d(9, 'bold'), radius=15, padx=14,
            pady=10, icon='\ue9d9', icon_font=self.fonts.i(11), outline=BORDER,
            stretch=True)
        self.multi_button.pack(side='left', fill=X, expand=YES, padx=(6, 0))
        tb.ToolTip(self.multi_button, bootstyle='primary', text=(
            'OFF: every code stops in this card until you press ADD.\n'
            'ON: each new code is added to the calculation automatically.'))

        # inline manual entry, revealed by the button above (Ctrl+L)
        self.manual_row = tk.Frame(parent, background=BG)
        self.manual_entry = tb.Entry(self.manual_row, bootstyle='primary',
                                     font=self.fonts.m(10))
        self.manual_entry.pack(side='left', fill=X, expand=YES, ipady=5)
        self.manual_entry.bind('<Return>', lambda _event: self._submit_manual())
        submit = PillButton(self.manual_row, text='SCAN',
                            command=self._submit_manual, fill=ACCENT, fg=BG,
                            hover_fill=ACCENT_DEEP, hover_fg=BG,
                            active_fill=ACCENT_DEEP, active_fg=BG,
                            font=self.fonts.d(9, 'bold'), radius=13, padx=16,
                            pady=8)
        submit.pack(side='left', padx=(8, 0))
        close = PillButton(self.manual_row, text='', command=self._toggle_manual,
                           fill=SURFACE, fg=MUTED, hover_fill=SURFACE_2,
                           hover_fg=DANGER, active_fill=DANGER, active_fg=BG,
                           font=self.fonts.d(9, 'bold'), radius=13, padx=11,
                           pady=8, icon='\ue711', icon_font=self.fonts.i(11),
                           outline=BORDER)
        close.pack(side='left', padx=(6, 0))

    def _toggle_manual(self):
        """Show or hide the inline manual-entry field."""
        if self.manual_row.winfo_manager():
            self.manual_row.pack_forget()
            self.manual_button.set_selected(False)
        else:
            self.manual_row.pack(fill=X, pady=(10, 0))
            self.manual_button.set_selected(True)
            self.manual_entry.focus_set()

    def _focus_manual(self):
        if not self.manual_row.winfo_manager():
            self._toggle_manual()
        else:
            self.manual_entry.focus_set()

    def _submit_manual(self):
        """Treat a typed code exactly like a camera hit."""
        code = self.manual_entry.get().strip()
        if not code:
            return
        self.manual_entry.delete(0, 'end')
        self._accept_code(code, 'MANUAL', None)

    # --------------------------------------------------------- totals ------
    def _build_totals(self, parent):
        self.totals_card = RoundedCard(parent, fill=SURFACE, radius=18,
                                       padding=(16, 12))
        self.totals_card.pack(fill=X, pady=(12, 0))
        body = self.totals_card.body
        body.grid_columnconfigure(0, weight=1)

        caption = tk.Frame(body, background=SURFACE)
        caption.grid(row=0, column=0, sticky=EW)
        tk.Label(caption, text='RUNNING CALCULATION', background=SURFACE,
                 foreground=MUTED, font=self.fonts.d(9, 'bold')).pack(side='left')
        self.items_chip = Chip(caption, text='0 items tally', fill=SURFACE_3,
                               fg=MUTED, font=self.fonts.m(8), radius=11,
                               padx=10, pady=4)
        self.items_chip.pack(side='right')

        self.total_label = tk.Label(body, text='$0.00', background=SURFACE,
                                    foreground=FG, font=self.fonts.d(30, 'bold'),
                                    anchor=W)
        self.total_label.grid(row=1, column=0, sticky=W, pady=(4, 0))

        meta = tk.Frame(body, background=SURFACE)
        meta.grid(row=2, column=0, sticky=EW, pady=(2, 0))
        self.subtotal_label = tk.Label(meta, text='subtotal $0.00', background=SURFACE,
                                       foreground=MUTED, font=self.fonts.m(8))
        self.subtotal_label.pack(side='left')
        self.tax_label = tk.Label(meta, text='TAX EST. +$0.00', background=SURFACE,
                                  foreground=WARN, font=self.fonts.m(8, 'bold'))
        self.tax_label.pack(side='right')

        self.undo_button = PillButton(
            body, text='UNDO LAST', command=self._undo_last, fill=SURFACE_2,
            fg=MUTED, hover_fill=SURFACE_3, hover_fg=DANGER, active_fill=DANGER,
            active_fg=BG, font=self.fonts.d(8, 'bold'), radius=12, padx=12,
            pady=6, icon='\ue7a7', icon_font=self.fonts.i(10))
        self.undo_button.grid(row=3, column=0, sticky=W, pady=(10, 0))
        self.undo_button.set_state('disabled')

    # --------------------------------------------------------- budget ------
    def _build_budget(self, parent):
        self.budget_card = RoundedCard(parent, fill=SURFACE, radius=18,
                                       padding=(16, 12))
        self.budget_card.pack(fill=X, pady=(12, 0))
        body = self.budget_card.body
        body.grid_columnconfigure(0, weight=1)

        top = tk.Frame(body, background=SURFACE)
        top.grid(row=0, column=0, sticky=EW)
        self.budget_value = tk.Label(top, text=money(self.budget), background=SURFACE,
                                     foreground=FG, font=self.fonts.d(17, 'bold'))
        self.budget_value.pack(side='left')
        tk.Label(top, text='BUDGET', background=SURFACE, foreground=MUTED,
                 font=self.fonts.m(8)).pack(side='left', padx=(8, 0), pady=(4, 0))

        self.budget_left = tk.Label(top, text='all of it left', background=SURFACE,
                                    foreground=ACCENT, font=self.fonts.d(11, 'bold'))
        self.budget_left.pack(side='right')
        self.budget_percent = tk.Label(top, text='0%', background=SURFACE,
                                       foreground=MUTED, font=self.fonts.m(8))
        self.budget_percent.pack(side='right', padx=(0, 8), pady=(3, 0))

        self.budget_bar = tk.Canvas(body, height=10, background=SURFACE,
                                    highlightthickness=0)
        self.budget_bar.grid(row=1, column=0, sticky=EW, pady=(10, 0))
        self.budget_bar.bind('<Configure>', lambda _e: self._draw_budget_bar())

        self.budget_edit = PillButton(
            body, text='SET BUDGET', command=self._edit_budget, fill=SURFACE_2,
            fg=MUTED, hover_fill=SURFACE_3, hover_fg=FG, active_fill=ACCENT,
            active_fg=BG, font=self.fonts.d(8, 'bold'), radius=12, padx=12,
            pady=6, icon='\ue713', icon_font=self.fonts.i(10))
        self.budget_edit.grid(row=2, column=0, sticky=W, pady=(10, 0))

    def _draw_budget_bar(self):
        """Rounded budget usage bar: accent under budget, danger when over."""
        canvas = self.budget_bar
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 4 or height <= 2:
            return
        round_rect(canvas, 0, 0, width, height, height / 2, fill=SURFACE_3,
                   outline='')
        spent, _remaining, _total, _percent = self._budget_state()
        fill = ACCENT if spent <= self.budget else DANGER
        fraction = min(spent / self.budget, 1.0) if self.budget > 0 else 0.0
        used = int(width * fraction)
        if used >= 3:
            round_rect(canvas, 0, 0, used, height, height / 2, fill=fill, outline='')

    # -------------------------------------------------------- history ------
    def _build_history(self, parent):
        self.history_card = RoundedCard(parent, fill=SURFACE, radius=18,
                                        padding=(16, 12))
        self.history_card.pack(fill=X, pady=(12, 4))

        head = tk.Frame(self.history_card.body, background=SURFACE)
        head.pack(fill=X)
        tk.Label(head, text='RECENTLY SCANNED', background=SURFACE, foreground=MUTED,
                 font=self.fonts.d(9, 'bold')).pack(side='left')
        self.clear_button = PillButton(
            head, text='CLEAR HISTORY', command=self._clear_history, fill=SURFACE_2,
            fg=MUTED, hover_fill=SURFACE_3, hover_fg=DANGER, active_fill=DANGER,
            active_fg=BG, font=self.fonts.d(8, 'bold'), radius=12, padx=12,
            pady=5, icon='\ue74d', icon_font=self.fonts.i(10))
        self.clear_button.pack(side='right')

        self.history_list = tk.Frame(self.history_card.body, background=SURFACE)
        self.history_list.pack(fill=X, pady=(10, 0))

        self.history_empty = tk.Frame(self.history_list, background=SURFACE)
        tk.Label(self.history_empty, text='nothing scanned yet', background=SURFACE,
                 foreground=MUTED, font=self.fonts.m(9)).pack(anchor=W, pady=(6, 2))
        tk.Label(self.history_empty, background=SURFACE, foreground=MUTED,
                 font=self.fonts.m(8), justify=LEFT,
                 text='Hold a barcode up to the camera - hits land here with\n'
                      'the price already tallied.').pack(anchor=W)
        self.rows = {}

    def _refresh_history(self):
        """Rebuild the recently-scanned rows from `self.history`."""
        for row in self.rows.values():
            row['frame'].destroy()
        self.rows = {}

        if not self.history:
            self.history_empty.pack(fill=X)
            self._sync_card_height(self.history_card)
            return
        self.history_empty.pack_forget()

        for entry in list(self.history)[:8]:
            self._build_history_row(entry)
        self._sync_card_height(self.history_card)

    def _build_history_row(self, entry):
        """One scanned line: tile, name, quantity stepper and line price."""
        code = entry['code']
        row = tk.Frame(self.history_list, background=SURFACE)
        row.pack(fill=X, pady=(0, 2))
        self.rows[code] = {'frame': row}

        tile = tk.Canvas(row, width=34, height=34, background=SURFACE,
                         highlightthickness=0)
        tile.pack(side='left', padx=(0, 10))
        round_rect(tile, 1, 1, 33, 33, 10, fill=SURFACE_3, outline='')
        tile.create_text(17, 17, text='\ue7bf', fill=MUTED, font=self.fonts.i(13))

        text = tk.Frame(row, background=SURFACE)
        text.pack(side='left', fill=X, expand=YES)
        name = entry['name']
        if len(name) > 26:
            name = name[:24] + '...'
        tk.Label(text, text=name, background=SURFACE, foreground=FG,
                 font=self.fonts.b(9, 'bold'), anchor=W).pack(anchor=W)
        tk.Label(text, text='{} \u2022 {}'.format(entry['size'], entry['aisle']),
                 background=SURFACE, foreground=MUTED, font=self.fonts.m(7),
                 anchor=W).pack(anchor=W)

        remove = PillButton(row, text='', command=lambda c=code: self._remove_item(c),
                            fill=SURFACE, fg=MUTED, hover_fill=SURFACE_2,
                            hover_fg=DANGER, active_fill=DANGER, active_fg=BG,
                            radius=9, padx=7, pady=6, icon='\ue74d',
                            icon_font=self.fonts.i(10))
        remove.pack(side='right', padx=(8, 0))

        stepper = tk.Frame(row, background=SURFACE)
        stepper.pack(side='right', padx=(10, 0))
        tk.Label(stepper, text=money(entry['price'] * entry['qty']), background=SURFACE,
                 foreground=FG, font=self.fonts.d(10, 'bold'), width=8,
                 anchor=E).pack(side='right')
        minus = PillButton(stepper, text='\u2212',
                           command=lambda c=code: self._change_qty(c, -1),
                           fill=SURFACE_2, fg=MUTED, hover_fill=SURFACE_3,
                           hover_fg=DANGER, active_fill=DANGER, active_fg=BG,
                           font=self.fonts.b(10, 'bold'), radius=9, padx=8, pady=3)
        minus.pack(side='right', padx=(6, 0))
        tk.Label(stepper, text=str(entry['qty']), background=SURFACE, foreground=FG,
                 font=self.fonts.d(11, 'bold'), width=3).pack(side='right', padx=(6, 0))
        plus = PillButton(stepper, text='+', command=lambda c=code: self._change_qty(c, 1),
                          fill=SURFACE_2, fg=MUTED, hover_fill=SURFACE_3,
                          hover_fg=ACCENT, active_fill=ACCENT, active_fg=BG,
                          font=self.fonts.b(10, 'bold'), radius=9, padx=8, pady=3)
        plus.pack(side='right', padx=(6, 0))

    def _sync_card_height(self, card):
        """Let a rounded card hug its children again after a rebuild."""
        card.update_idletasks()
        target = card.body.winfo_reqheight() + 2 * card._pady
        if target > 1:
            card.configure(height=target)
        card._paint()

    # ------------------------------------------------------- calculation ---
    def _budget_state(self):
        """Return (spent, remaining, total_with_tax, percent_of_budget_left)."""
        spent = sum(entry['price'] * entry['qty'] for entry in self.history)
        total = spent * (1.0 + TAX_RATE)
        remaining = self.budget - total
        percent = (remaining / self.budget * 100.0) if self.budget > 0 else 0.0
        return spent, remaining, total, percent

    def _refresh_totals(self):
        """Recompute the running calculation, budget card and undo button."""
        spent, remaining, total, percent = self._budget_state()
        items = sum(entry['qty'] for entry in self.history)

        self.total_label.configure(text=money(total))
        self.subtotal_label.configure(text='subtotal {}'.format(money(spent)))
        self.tax_label.configure(text='TAX EST. +{}'.format(money(total - spent)))
        self.items_chip.update_chip(
            text='{} item{} tally'.format(items, '' if items == 1 else 's'))

        over = remaining < 0
        self.budget_left.configure(
            text='{} {}'.format(money(abs(remaining)), 'over' if over else 'left'),
            foreground=DANGER if over else ACCENT)
        self.budget_percent.configure(text='{:.0f}%'.format(max(percent, 0.0)))
        self.budget_value.configure(text=money(self.budget))
        self.undo_button.set_state('normal' if self.history else 'disabled')
        self._draw_budget_bar()

    def _refresh_chips(self):
        """Update the status pills (called on a throttle from the tick loop)."""
        if self.engine.error:
            self.fps_chip.update_chip(text='CAMERA OFFLINE', fill=SURFACE_2,
                                      fg=DANGER, dot=DANGER)
        elif not self.scanning:
            self.fps_chip.update_chip(text='SCAN PAUSED', fill=SURFACE_2,
                                      fg=WARN, dot=WARN)
        else:
            self.fps_chip.update_chip(text='{:.0f} FPS OPTICAL'.format(self.fps),
                                      fill=SURFACE_2, fg=ACCENT, dot=ACCENT)

        width, height = self.engine.width, self.engine.height
        self.sensor_chip.update_chip(
            text='{} x {}'.format(width, height) if width and height else 'no signal')

        self.mode_chip.update_chip(text=SCAN_MODES[self.mode_index][0])
        self.zoom_button.set_text('{:g}x'.format(self.zoom))
        self.exposure_button.set_text(
            'AUTO-EXPOSURE: {}'.format('ON' if self.auto_exposure else 'OFF'))
        self.exposure_button.set_selected(self.auto_exposure)
        self.sound_button.set_selected(self.beep_enabled)
        self.lamp_button.set_selected(self.lamp_on)

    # ---------------------------------------------------------- scanning ---
    def _accept_code(self, code, source, rect=None):
        """A code arrived (camera or manual): display it, then honour MULTI-SCAN."""
        name, price, size, aisle, estimated = guess_product(code)
        self.last_source = source
        self._set_product((name, price, size, aisle, estimated))
        self.pending_code = code
        if self.beep_enabled:
            self._beep()
        if self.multi_scan:
            self._add_current()

    def _add_current(self):
        """Push the pending code onto the running calculation."""
        code = self.pending_code
        if code is None:
            return
        for entry in self.history:
            if entry['code'] == code:
                entry['qty'] += 1
                break
        else:
            name, price, size, aisle, estimated = guess_product(code)
            self.history.appendleft({
                'code': code, 'name': name, 'price': price, 'size': size,
                'aisle': aisle, 'estimated': estimated, 'qty': 1,
                'stamp': datetime.now(),
            })
        self._refresh_history()
        self._refresh_totals()

    def _change_qty(self, code, delta):
        """Step one line up or down; dropping below one removes the line."""
        for entry in list(self.history):
            if entry['code'] != code:
                continue
            entry['qty'] += delta
            if entry['qty'] < 1:
                self.history.remove(entry)
            break
        self._refresh_history()
        self._refresh_totals()

    def _remove_item(self, code):
        """Drop a line from the running calculation entirely."""
        for entry in list(self.history):
            if entry['code'] == code:
                self.history.remove(entry)
                break
        self._refresh_history()
        self._refresh_totals()

    def _undo_last(self):
        """Step the most recent line back by one."""
        if self.history:
            self._change_qty(self.history[0]['code'], -1)

    def _clear_history(self):
        """Empty the recently-scanned list and the running calculation."""
        self.history.clear()
        self.pending_code = None
        self.last_code = None
        self._set_product(None)
        self._refresh_history()
        self._refresh_totals()

    def _edit_budget(self):
        """Ask for a new session budget with a themed dialog."""
        value = tb.Querybox.get_float(
            'Session budget in dollars:', 'SET BUDGET', initialvalue=self.budget,
            minvalue=1.0, maxvalue=100000.0, parent=self.root)
        if value:
            self.budget = float(value)
            self._refresh_totals()

    def _reset_session(self):
        """Clear the calculation and put the budget back to its default."""
        if self.history:
            answer = tb.Messagebox.yesno('Clear the running calculation and the '
                                         'recently-scanned list?', 'RESET SESSION',
                                         parent=self.root)
            if answer != 'Yes':
                return
        self._clear_history()
        self.budget = DEFAULT_BUDGET
        self._refresh_totals()

    def _beep(self):
        """Beep on a successful detect using the bundled .wav."""
        try:
            if os.path.exists(sound_file_path):
                winsound.PlaySound(sound_file_path,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except RuntimeError:                              # pragma: no cover
            pass

    # ----------------------------------------------------------- controls --
    def _set_mode(self, index):
        """Switch the accepted symbol set (BARCODE / QR / BATCH)."""
        self.mode_index = index % len(SCAN_MODES)
        label, symbols = SCAN_MODES[self.mode_index]
        self.mode_symbols = set(symbols) if symbols else set(ALL_SYMBOLS)
        self.engine.set_symbols(symbols)
        for position, button in enumerate(self.mode_buttons):
            button.set_selected(position == self.mode_index)
        self.mode_chip.update_chip(text=label)
        self.last_payload = None
        self._dirty = True

    def _toggle_scan(self):
        self.scanning = not self.scanning
        self._refresh_chips()
        self._dirty = True

    def _toggle_multi(self):
        """Tallies every hit while on; adopts the displayed item when turned on.

        The displayed item is only adopted if it is not already in the
        calculation, so flipping the switch back and forth cannot inflate a
        quantity.
        """
        self.multi_scan = not self.multi_scan
        self.multi_button.set_selected(self.multi_scan)
        if not self.multi_scan or self.pending_code is None:
            return
        if self.pending_code not in [entry['code'] for entry in self.history]:
            self._add_current()

    def _toggle_sound(self):
        self.beep_enabled = not self.beep_enabled
        self.sound_button.set_selected(self.beep_enabled)
        if self.beep_enabled:
            self._beep()

    def _toggle_exposure(self):
        self.auto_exposure = not self.auto_exposure
        self._apply_camera_props()
        self._refresh_chips()

    def _toggle_lamp(self):
        self.lamp_on = not self.lamp_on
        self._apply_camera_props()
        self._refresh_chips()

    def _apply_camera_props(self):
        """Push exposure/brightness to the driver when it supports them."""
        capture = self.engine.capture
        if capture is None:
            return
        try:
            if hasattr(cv2, 'CAP_PROP_AUTO_EXPOSURE'):
                capture.set(cv2.CAP_PROP_AUTO_EXPOSURE,
                            0.75 if self.auto_exposure else 0.25)
            if hasattr(cv2, 'CAP_PROP_BRIGHTNESS'):
                capture.set(cv2.CAP_PROP_BRIGHTNESS, 160 if self.lamp_on else 128)
        except Exception:                                 # pragma: no cover
            pass

    def _cycle_zoom(self, delta=1):
        self.zoom_index = (self.zoom_index + delta) % len(self.zoom_steps)
        self.zoom_button.set_text('{:g}x'.format(self.zoom))
        self._dirty = True

    # ------------------------------------------------------------ nav ------
    def _nav(self, tab):
        """Bottom navigation: highlight the tab and scroll its card into view."""
        self.page_tab = tab
        for label, button in self.nav_buttons.items():
            button.set_selected(label == tab)
        targets = {'SCAN': self.viewport, 'CART': self.totals_card,
                   'HISTORY': self.history_card, 'BUDGET': self.budget_card}
        self._scroll_to(targets.get(tab))

    def _scroll_to(self, widget):
        """Scroll the page so `widget` sits near the top of the viewport."""
        if widget is None:
            return
        self.sf.update_idletasks()
        content = float(self.sf.winfo_reqheight())
        if content <= 1:
            return
        fraction = (widget.winfo_y() - 10) / content
        self.sf.yview_moveto(max(0.0, min(1.0, fraction)))

    # ------------------------------------------------------- frame loop ----
    def _tick(self):
        """Pump frames, decode results, fps and redraws on one 60 ms cadence."""
        if self._closing:
            return
        now = time.time()
        frame = None
        while True:
            try:
                frame = self.frames.get_nowait()
            except queue.Empty:
                break
        if frame is not None:
            self._frame_times.append(now)
            self._prepare_photo(frame)
            self._dirty = True

        self.engine.decode_loop(lambda: self.scanning, self.mode_symbols)
        self._drain_results()

        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            self.fps = (len(self._frame_times) - 1) / span if span > 0 else 0.0

        if self._dirty and now - self._last_viewport_draw >= 0.033:
            self._draw_viewport()
            self._last_viewport_draw = now
            self._dirty = False
        elif self._overlay_shown and now >= self.overlay_until:
            # the detect overlay timed out - repaint without it
            self._draw_viewport()

        if now - self._last_chip_refresh >= 0.5:
            self._refresh_chips()
            self._last_chip_refresh = now

        self.root.after(60, self._tick)

    def _prepare_photo(self, frame):
        """Colour-convert the newest frame, apply the zoom crop and publish it."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        if self.zoom > 1.0:
            crop_w, crop_h = max(int(width / self.zoom), 16), max(int(height / self.zoom), 16)
            origin_x, origin_y = (width - crop_w) // 2, (height - crop_h) // 2
            rgb = rgb[origin_y:origin_y + crop_h, origin_x:origin_x + crop_w]
        else:
            origin_x, origin_y = 0, 0
        self._crop_origin = (origin_x, origin_y)
        self._frame_size = (rgb.shape[1], rgb.shape[0])

        image = Image.fromarray(rgb)
        if self._photo is None or self._photo.width() != rgb.shape[1] \
                or self._photo.height() != rgb.shape[0]:
            self._photo = ImageTk.PhotoImage(image)
        else:
            self._photo.paste(image)

    def _drain_results(self):
        while True:
            try:
                payload = self.results.get_nowait()
            except queue.Empty:
                return
            self._handle_result(payload)

    def _handle_result(self, payload):
        """Rate-limit duplicate hits, then hand the code to the calculation."""
        now = time.time()
        code = payload['text']
        self.last_payload = payload
        self.overlay_until = now + 2.5
        self._overlay_shown = True
        self._dirty = True
        if code == self.last_code and now - self.last_scan_at < self.duplicate_window:
            self.last_scan_at = now
            return
        self.last_code = code
        self.last_scan_at = now
        self._flash_until = now + 0.35
        self._accept_code(code, symbol_type(payload['type']), payload.get('rect'))

    def on_close(self):
        """Stop the camera thread before tearing the window down."""
        if self._closing:
            return
        self._closing = True
        try:
            self.engine.stop()
        finally:
            self.root.destroy()


# ----------------------------------------------------------- widget kit ----
def round_rect(canvas, x0, y0, x1, y1, radius, **kwargs):
    """Draw a rounded rectangle as a single exact-arc polygon on `canvas`."""
    radius = max(0, min(radius, int(min(x1 - x0, y1 - y0) / 2)))
    points = []
    corners = (
        (x1 - radius, y0 + radius, -90), (x1 - radius, y1 - radius, 0),
        (x0 + radius, y1 - radius, 90), (x0 + radius, y0 + radius, 180),
    )
    for cx, cy, start in corners:
        for step in range(9):
            angle = math.radians(start + step * 90 / 8.0)
            points.append(cx + radius * math.cos(angle))
            points.append(cy + radius * math.sin(angle))
    return canvas.create_polygon(points, **kwargs)


def outer_bg(widget, fallback=BG):
    """Background of `widget`, so canvases blend into their parent."""
    try:
        return widget.cget('background')
    except tk.TclError:
        return fallback


class Chip(tk.Canvas):
    """A rounded status pill: optional leading dot/icon plus a short label."""

    def __init__(self, parent, text='', fill=SURFACE_3, fg=FG,
                 font=('Segoe UI', 8), radius=13, padx=11, pady=6, dot=None,
                 icon=None, icon_font=('Segoe UI', 9), outline=None, **kwargs):
        super().__init__(parent, highlightthickness=0, borderwidth=0,
                         background=outer_bg(parent), **kwargs)
        self._fill, self._fg, self._font = fill, fg, font
        self._radius, self._padx, self._pady = radius, padx, pady
        self._dot, self._icon, self._icon_font = dot, icon, icon_font
        self._outline = outline
        self._text = text
        self._layout()

    def update_chip(self, text=None, fill=None, fg=None, dot=None, icon=None):
        """Update the pill contents (kept apart from tk's configure())."""
        if text is not None:
            self._text = text
        if fill is not None:
            self._fill = fill
        if fg is not None:
            self._fg = fg
        if dot is not None:
            self._dot = dot
        if icon is not None:
            self._icon = icon
        self._layout()

    def _layout(self):
        self.delete('all')
        font = tkfont.Font(font=self._font)
        icon_font = tkfont.Font(font=self._icon_font)
        width = self._padx * 2 + font.measure(self._text)
        if self._dot:
            width += 11
        if self._icon:
            width += icon_font.measure(self._icon) + 5
        height = self._pady * 2 + font.metrics('linespace')

        kwargs = {'fill': self._fill, 'tags': 'bg'}
        if self._outline:
            kwargs.update(outline=self._outline, width=1)
        round_rect(self, 1, 1, width - 1, height - 1, self._radius, **kwargs)

        x = self._padx
        if self._dot:
            cy = height / 2 + 1
            self.create_oval(x, cy - 3, x + 6, cy + 3, fill=self._dot,
                             outline='', tags='bg')
            x += 11
        if self._icon:
            self.create_text(x, height / 2, text=self._icon, anchor=W,
                             fill=self._fg, font=self._icon_font, tags='bg')
            x += icon_font.measure(self._icon) + 5
        self.create_text(x, height / 2, text=self._text, anchor=W, fill=self._fg,
                         font=self._font, tags='bg')
        self.configure(width=width, height=height)


class PillButton(tk.Canvas):
    """Rounded button with hover states, an optional icon and a selected look."""

    def __init__(self, parent, text='', command=None, fill=SURFACE_3, fg=FG,
                 hover_fill=SURFACE, hover_fg=FG, active_fill=ACCENT,
                 active_fg=BG, font=('Segoe UI', 9, 'bold'), radius=15, padx=14,
                 pady=8, icon=None, icon_font=('Segoe Fluent Icons', 10),
                 outline=None, width=None, dot=None, disabled_fg=MUTED,
                 stretch=False, **kwargs):
        super().__init__(parent, highlightthickness=0, borderwidth=0,
                         background=outer_bg(parent), **kwargs)
        self._command = command
        self._idle = (fill, fg)
        self._hover = (hover_fill, hover_fg)
        self._active = (active_fill, active_fg)
        self._selected = False
        self._state = 'normal'
        self._font, self._icon_font = font, icon_font
        self._radius, self._padx, self._pady = radius, padx, pady
        self._outline = outline
        self._fixed_width = width
        self._dot = dot
        self._icon = icon
        self._text = text
        self._disabled_fg = disabled_fg
        self._stretch = stretch
        self._last_w = 0
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', lambda _e: self._layout())
        self.bind('<Button-1>', self._on_press)
        self.bind('<ButtonRelease-1>', self._on_release)
        if stretch:
            self.bind('<Configure>', self._on_configure)
        self._layout()

    # -- public API --------------------------------------------------------
    def set_text(self, text):
        self._text = text
        self._layout()

    def set_selected(self, selected):
        self._selected = bool(selected)
        self._layout()

    def set_state(self, state):
        self._state = state
        self._layout()

    def is_selected(self):
        return self._selected

    def add_flash_class(self, tags):
        """Also run this button's hover/press feedback under extra bindtags."""
        for tag in tags:
            self.bindtags(tuple(self.bindtags()) + (tag,))

    def set_accent(self, fill, fg=BG):
        """Change the selected-state colour used by the steppers."""
        self._active = (fill, fg)
        self._idle = (SURFACE_2, FG)
        self._hover = (fill, BG)
        self._layout()

    # -- events ------------------------------------------------------------
    def _on_enter(self, _event=None):
        if self._state == 'normal':
            self._render(*self._hover)

    def _on_press(self, _event=None):
        if self._state == 'normal':
            self._render(*self._active)

    def _on_release(self, event=None):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._layout()
        if inside and self._state == 'normal' and self._command is not None:
            self._command()

    # -- drawing -----------------------------------------------------------
    def _layout(self):
        if self._state == 'disabled':
            self._render(SURFACE_2, self._disabled_fg)
        elif self._selected:
            self._render(*self._active)
        else:
            self._render(*self._idle)

    def _render(self, fill, fg):
        self.delete('all')
        font = tkfont.Font(font=self._font)
        icon_font = tkfont.Font(font=self._icon_font)
        text_w = font.measure(self._text)
        icon_w = icon_font.measure(self._icon) if self._icon else 0
        content = text_w + icon_w + (6 if (icon_w and text_w) else 0)
        if self._dot:
            content += 11
        if self._stretch and self._last_w > 1:
            width = self._last_w
        else:
            width = self._fixed_width or content + self._padx * 2
        height = self._pady * 2 + font.metrics('linespace')

        kwargs = {'fill': fill, 'tags': 'bg'}
        if self._outline:
            kwargs.update(outline=self._outline, width=1)
        round_rect(self, 1, 1, width - 1, height - 1, self._radius, **kwargs)

        centred = bool(self._fixed_width) or (self._stretch and self._last_w > 1)
        x = (width - content) / 2 if centred else self._padx
        if self._dot:
            self.create_oval(x, height / 2 - 2, x + 6, height / 2 + 4,
                             fill=self._dot, outline='', tags='bg')
            x += 11
        if icon_w:
            self.create_text(x, height / 2, text=self._icon, anchor=W, fill=fg,
                             font=self._icon_font, tags='bg')
            x += icon_w + 6
        self.create_text(x, height / 2, text=self._text, anchor=W, fill=fg,
                         font=self._font, tags='bg')
        if self._stretch:
            self.configure(height=height)
        else:
            self.configure(width=width, height=height)

    def _on_configure(self, event):
        """Re-draw at the width the geometry manager handed us (stretch mode)."""
        if event.width > 1 and event.width != self._last_w:
            self._last_w = event.width
            self._layout()


class RoundedCard(tk.Canvas):
    """A rounded panel that hosts child widgets in `body` and auto-sizes."""

    def __init__(self, parent, fill=SURFACE, radius=16, padding=(14, 12),
                 outline=BORDER, background=None, **kwargs):
        super().__init__(parent, background=background or outer_bg(parent),
                         highlightthickness=0, borderwidth=0, **kwargs)
        self._fill = fill
        self._radius = radius
        self._padx, self._pady = padding
        self._outline = outline
        self.body = tk.Frame(self, background=fill)
        self._win = self.create_window(self._padx, self._pady,
                                       window=self.body, anchor='nw')
        self._inner_width = -1
        self.bind('<Configure>', self._on_canvas)
        self.body.bind('<Configure>', self._on_body)

    def _on_canvas(self, event):
        inner = max(event.width - 2 * self._padx, 1)
        if inner != self._inner_width:
            self._inner_width = inner
            self.itemconfigure(self._win, width=inner)
        self._paint()

    def _on_body(self, _event=None):
        target = self.body.winfo_reqheight() + 2 * self._pady
        if target > 1 and target != self.winfo_height():
            self.configure(height=target)
        self._paint()

    def _paint(self):
        self.delete('panel')
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        kwargs = {'fill': self._fill, 'tags': 'panel'}
        if self._outline:
            kwargs['outline'] = self._outline
            kwargs['width'] = 1
        round_rect(self, 1, 1, width - 2, height - 2, self._radius, **kwargs)
        self.tag_lower('panel')


# ------------------------------------------------------------- theming -----
def register_scancalc_theme(style):
    """Register the ScanCalc palette as a dark ttkbootstrap theme."""
    if THEME_NAME not in style.theme_names():
        colors = Colors(
            primary=ACCENT, secondary=MUTED, success=ACCENT, info=INFO,
            warning=WARN, danger=DANGER, light=FG, dark=BG, bg=BG, fg=FG,
            selectbg=ACCENT, selectfg=BG, border=BORDER, inputfg=FG,
            inputbg=SURFACE_2, active=ACCENT_DEEP,
        )
        style.register_theme(ThemeDefinition(THEME_NAME, colors, mode='dark'))
    style.theme_use(THEME_NAME)


def first_available(candidates, fallback):
    """Return the first installed font family from `candidates`."""
    try:
        available = set(tkfont.families())
    except tk.TclError:      # no root yet - the fallback still renders
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


class Fonts:
    """Font families resolved once the Tk root exists."""

    def __init__(self):
        # Bahnschrift is a DIN-style technical face - a close match for the
        # mockup's lettering - and ships with Windows 10+.
        self.display = first_available(
            ['Bahnschrift', 'DIN Alternate', 'Segoe UI Semibold'], 'Segoe UI')
        self.body = first_available(['Segoe UI'], 'TkDefaultFont')
        self.mono = first_available(['Consolas', 'Courier New'], 'Courier')
        self.icon = first_available(
            ['Segoe Fluent Icons', 'Segoe MDL2 Assets', 'Segoe UI Symbol'],
            'Segoe UI')

    def d(self, size, weight='normal'):
        return (self.display, size, weight)

    def b(self, size, weight='normal'):
        return (self.body, size, weight)

    def m(self, size, weight='normal'):
        return (self.mono, size, weight)

    def i(self, size, weight='normal'):
        return (self.icon, size, weight)


# ------------------------------------------------------------- launcher ----
def main():
    """Create the root window, register the ScanCalc theme and run the app."""
    root = tb.Window(title='ScanCalc - barcode / QR scanner')
    register_scancalc_theme(root.style)
    root.configure(background=BG)
    root.minsize(760, 560)

    # Keep the window inside the screen: the page scrolls, so a short
    # viewport (e.g. 1366x768 laptops) still shows every card.
    width = min(940, root.winfo_screenwidth() - 80)
    height = min(920, root.winfo_screenheight() - 120)
    x = max((root.winfo_screenwidth() - width) // 2, 0)
    y = max((root.winfo_screenheight() - height) // 3, 0)
    root.geometry('{}x{}+{}+{}'.format(width, height, x, y))

    app = ScanCalcApp(root)
    try:
        root.mainloop()
    finally:
        if not app._closing:
            app.engine.stop()


if __name__ == '__main__':
    main()
