"""Barcode / QR scanner with a modern ttkbootstrap UI."""

import os
import queue
import threading
import time
import tkinter as tk
import winsound  # built-in on Windows - plays the .wav beep with no extra packages
from collections import deque
from datetime import datetime
from tkinter import messagebox

import cv2
import ttkbootstrap as tb
from PIL import Image, ImageTk
from pyzbar.pyzbar import decode
from ttkbootstrap.constants import (
    BOTH, EW, LEFT, NO, RIGHT, W, X, Y, YES,
)

sound_file_path = os.path.join(os.path.dirname(__file__), 'beep-01a.wav')

DEFAULT_THEME = 'superhero'


def play_sound():
    """Play the beep WAV asynchronously using the built-in winsound module."""
    try:
        winsound.PlaySound(sound_file_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as exc:  # best-effort
        print('Could not play sound: {}'.format(exc))


class BarcodeScannerApp:
    """Main application window: live camera preview + controls side panel."""

    def __init__(self, root):
        self.root = root
        self.root.title('Barcode & QR Scanner')
        self.root.geometry('1040x640')
        self.root.minsize(900, 580)

        self.frame_queue = queue.Queue()
        self.camera_index = 0
        self.previous_barcodes = set()
        self.scanning = False          # whether capture is requested
        self.camera_running = False    # whether the capture thread is alive
        self.scan_thread = None
        self.stop_event = threading.Event()
        self.recent_scans = deque(maxlen=20)

        self._build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self.quit_app)

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self):
        self.style = tb.Style(DEFAULT_THEME)

        self.main = tb.Frame(self.root, padding=12)
        self.main.pack(fill=BOTH, expand=YES)

        # Top header with title + live status badge
        self.header = tb.Frame(self.main, padding=(0, 6))
        self.header.pack(fill=X)
        tb.Label(
            self.header, text='Barcode & QR Scanner',
            font=('Segoe UI', 20, 'bold'), bootstyle='inverse-primary',
        ).pack(side=LEFT, padx=(0, 10))
        self.status_badge = tb.Label(
            self.header, text='● OFFLINE', bootstyle='inverse-secondary',
        )
        self.status_badge.pack(side=RIGHT)
        tb.Separator(self.main).pack(fill=X, pady=(8, 10))

        # Left: camera preview
        left = tb.Frame(self.main)
        left.pack(side=LEFT, fill=BOTH, expand=YES, padx=(0, 10))
        self.panel = tb.Label(left, text='Start the camera to see the feed')
        self.panel.pack(fill=BOTH, expand=YES)

        # Right: controls + recent scans
        right = tb.Frame(self.main, width=300)
        right.pack(side=RIGHT, fill=Y, expand=NO)
        right.pack_propagate(False)

        self.controls = tb.Labelframe(right, text='Controls', padding=12, bootstyle='primary')
        self.controls.pack(fill=X, pady=(0, 10))

        tb.Label(self.controls, text='Camera').grid(row=0, column=0, sticky=W, pady=6)
        self.camera_var = tk.StringVar(value='0')
        self.camera_spin = tb.Spinbox(
            self.controls, from_=0, to=3, width=6,
            textvariable=self.camera_var, command=self.on_camera_change,
        )
        self.camera_spin.grid(row=0, column=1, sticky=W, pady=6)

        self.btn_start = tb.Button(
            self.controls, text='Start scanning', bootstyle='success-outline',
            command=self.start_scanning,
        )
        self.btn_start.grid(row=1, column=0, columnspan=2, sticky=EW, pady=(6, 2))

        self.btn_stop = tb.Button(
            self.controls, text='Stop scanning', bootstyle='danger-outline',
            command=self.stop_scanning, state=tk.DISABLED,
        )
        self.btn_stop.grid(row=2, column=0, columnspan=2, sticky=EW, pady=(0, 2))

        tb.Button(
            self.controls, text='Quit', bootstyle='secondary-outline',
            command=self.quit_app,
        ).grid(row=3, column=0, columnspan=2, sticky=EW, pady=(6, 2))

        # Recent scans list
        self.scan_box = tb.Labelframe(
            right, text='Recent scans', padding=10, bootstyle='primary',
        )
        self.scan_box.pack(fill=BOTH, expand=YES, pady=(10, 4))

        self.scan_list = tb.Treeview(
            self.scan_box, columns=('time', 'code'), show='headings',
            height=10, bootstyle='primary', takefocus=False,
        )
        self.scan_list.heading('time', text='Time')
        self.scan_list.heading('code', text='Code')
        self.scan_list.column('time', width=110, anchor=W, stretch=NO)
        self.scan_list.column('code', width=170, anchor=W)
        self.scan_list.pack(fill=BOTH, expand=YES)

        self.counter_lbl = tb.Label(
            right, text='Scanned: 0', bootstyle='info', anchor=W,
        )
        self.counter_lbl.pack(fill=X, pady=(6, 0))

        self._set_status('offline')

    def _set_status(self, state):
        """state: 'offline' | 'live' | 'stopped' | 'error'"""
        mapping = {
            'offline': ('● OFFLINE', 'inverse-secondary'),
            'live': ('● LIVE', 'inverse-success'),
            'stopped': ('■ PAUSED', 'inverse-warning'),
            'error': ('!! ERROR', 'inverse-danger'),
        }
        text, boot = mapping[state]
        self.status_badge.config(text=text, bootstyle=boot)

    # ------------------------------------------------------------- camera --
    def start_scanning(self):
        # Force a fresh capture loop (handles camera swaps too)
        self.stop_event.set()
        self.scanning = False
        self._open_scanner()

    def stop_scanning(self):
        self.scanning = False
        self.stop_event.set()
        self.previous_barcodes.clear()
        self._set_status('stopped')
        self._reset_buttons()

    def on_camera_change(self):
        self.camera_index = int(self.camera_var.get())
        # Restart capture with the newly selected camera
        self.start_scanning()

    def _open_scanner(self):
        if self.camera_running:
            return
        self.stop_event.clear()
        self.previous_barcodes.clear()
        self.scan_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.scan_thread.start()

    def _capture_loop(self):
        self.camera_running = True
        try:
            self._scan_loop()
        finally:
            self.camera_running = False

    def _scan_loop(self):
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            cap.release()
            self.root.after(0, lambda: messagebox.showwarning(
                'Camera Error', 'Could not open the camera.'))
            self.root.after(0, lambda: self._set_status('error'))
            self.root.after(0, self._reset_buttons)
            return

        self.root.after(0, lambda: self._set_status('live'))
        self.root.after(0, self._set_buttons_running)

        frame_count = 0
        try:
            while self.scanning_active():
                ok, frame = cap.read()
                if not ok:
                    break

                frame = frame[:, ::-1, :]
                frame = cv2.resize(frame, (640, 480))

                frame_count += 1
                if frame_count % 5 == 0:
                    current = set()
                    for barcode in decode(frame):
                        data = barcode.data
                        if not data:
                            continue
                        current.add(data)
                        # Beep/save only when the code was NOT visible on the
                        # previous scan -> allows re-scanning a code that left
                        # the view, but avoids spamming while held still.
                        if data not in self.previous_barcodes:
                            self._on_code(data, frame)
                    self.previous_barcodes = current

                img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(img)
                img = ImageTk.PhotoImage(img)
                self.frame_queue.put(img)
                time.sleep(0.1)
        finally:
            cap.release()
            cv2.destroyAllWindows()
            self.root.after(0, lambda: self._set_status('stopped'))
            self.root.after(0, self._reset_buttons)

    def scanning_active(self):
        return not self.stop_event.is_set()

    def _on_code(self, data, frame):
        self.save_data(data)
        play_sound()
        self._log_scan(data)
        label = str(data)
        cv2.putText(frame, label, (50, 60),
                    cv2.FONT_HERSHEY_COMPLEX, 2, (0, 255, 255), 2)

    def update_panel(self):
        try:
            while True:
                img = self.frame_queue.get_nowait()
                self.panel.img = img
                self.panel.config(image=img)
        except queue.Empty:
            pass
        self.root.after(40, self.update_panel)

    def _log_scan(self, data):
        now = datetime.now().strftime('%H:%M:%S')
        self.recent_scans.appendleft((now, str(data)))
        self.scan_list.insert('', 0, values=(now, str(data)))
        if len(self.scan_list.get_children()) > 20:
            self.scan_list.delete(self.scan_list.get_children()[-1])
        self.counter_lbl.config(text='Scanned: {}'.format(len(self.recent_scans)))

    def _set_buttons_running(self):
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

    def _reset_buttons(self):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def quit_app(self):
        self.stop_scanning()
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=2.0)
        self.root.destroy()

    # -------------------------------------------------------------- data --
    def save_data(self, data):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open('scanned_data.txt', 'a') as file:
            file.write('{}: {}\n'.format(timestamp, data))


if __name__ == '__main__':
    AppWindow = tb.Window(themename=DEFAULT_THEME)
    app = BarcodeScannerApp(AppWindow)
    AppWindow.after(40, app.update_panel)
    AppWindow.mainloop()