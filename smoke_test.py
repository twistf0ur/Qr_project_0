"""End-to-end smoke test for the ScanCalc UI (no camera required).

Builds the real window, injects a synthetic camera frame that contains a real
barcode, and drives the app's own tick loop so the whole capture -> decode ->
overlay -> running-calculation path executes for real.

Run it with:   python smoke_test.py
Exit code 0 means every check passed. A transcript is also written to
smoke_test_report.txt, because Tk can swallow stdout on Windows.
"""
import os
import traceback

import numpy as np
from pyzbar.pyzbar import decode

import qr_scanner as q

REPORT_FILE = 'smoke_test_report.txt'
LINES = []


def log(line):
    LINES.append(line)
    with open(REPORT_FILE, 'a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def check(name, condition, detail=''):
    log('{:<44} {} {}'.format(name, 'PASS' if condition else 'FAIL', detail))
    return bool(condition)


# ------------------------------------------------------- test fixtures -----
# The app only *reads* barcodes, so the smoke test has to produce one itself to
# prove the capture -> decode -> calculation path works. These two helpers are
# the ISO/IEC 15417 Code 128-B encoder, kept here as a test fixture only.
_CODE128 = (
    '212222', '222122', '222221', '121223', '121322', '131222', '122213',
    '122312', '132212', '221213', '221312', '231212', '112232', '122132',
    '122231', '113222', '123122', '123221', '223211', '221132', '221231',
    '213212', '223112', '312131', '311222', '321122', '321221', '312212',
    '322112', '322211', '212123', '212321', '232121', '111323', '131123',
    '131321', '112313', '132113', '132311', '211313', '231113', '231311',
    '112133', '112331', '132131', '113123', '113321', '133121', '313121',
    '211331', '231131', '213113', '213311', '213131', '311123', '311321',
    '331121', '312113', '312311', '332111', '314111', '221411', '431111',
    '111224', '111422', '121124', '121421', '141122', '141221', '112214',
    '112412', '122114', '122411', '142112', '142211', '241211', '221114',
    '413111', '241112', '134111', '111242', '121142', '121241', '114212',
    '124112', '124211', '411212', '421112', '421211', '212141', '214121',
    '412121', '111143', '111341', '131141', '114113', '114311', '411113',
    '411311', '113141', '114131', '311141', '411131', '211412', '211214',
    '211232', '2331112',
)


def code128b_modules(data):
    """Bar/space module string ('1' = bar) for a Code 128-B symbol."""
    values = [104]                                   # START B
    values.extend(ord(ch) - 32 for ch in data)
    values.append((values[0] + sum(i * v for i, v in enumerate(values[1:], 1))) % 103)
    values.append(106)                               # STOP
    bits = []
    for value in values:
        for i, width in enumerate(_CODE128[value]):
            bits.append(('1' if i % 2 == 0 else '0') * int(width))
    return ''.join(bits)


def barcode_image(code, width, height, quiet=30):
    """Render `code` as a Code 128-B symbol (the fixture the app must decode)."""
    from PIL import Image, ImageDraw
    bars = code128b_modules(code)
    total = len(bars) + quiet * 2
    scale = width / float(total)
    image = Image.new('RGB', (width, height), '#ffffff')
    draw = ImageDraw.Draw(image)
    for i, bit in enumerate(bars):
        if bit != '1':
            continue
        x0 = int(round((quiet + i) * scale))
        x1 = int(round((quiet + i + 1) * scale))
        draw.rectangle([x0, 0, max(x0, x1 - 1), height - 1], fill='black')
    return image


def synthetic_frame(code, width=1280, height=720):
    """A camera-like BGR frame with `code` printed as a scannable barcode.

    The module width is kept at >= 4 px so pyzbar sees crisp bars; rounding a
    narrow module onto whole pixels is what stops a synthetic barcode from
    decoding at small sizes.
    """
    from PIL import Image, ImageDraw
    bars = code128b_modules(code)
    quiet = 30
    total = len(bars) + quiet * 2
    scale = max(4.0, (width * 0.78) / total)
    bar_w = int(round(total * scale))
    canvas_w = max(width, bar_w + 80)
    top, bar_h = int(height * 0.30), int(height * 0.34)
    frame = Image.new('RGB', (canvas_w, height), '#f2f2f2')
    draw = ImageDraw.Draw(frame)
    left = (canvas_w - bar_w) // 2
    for i, bit in enumerate(bars):
        if bit == '1':
            x0 = left + int(round((quiet + i) * scale))
            x1 = left + int(round((quiet + i + 1) * scale))
            draw.rectangle([x0, top, max(x0, x1 - 1), top + bar_h], fill='black')
    return np.array(frame)[:, :, ::-1].copy()          # RGB -> BGR


def check_helpers():
    check('code128 modules non-empty', len(code128b_modules('ABC')) > 40)
    check('barcode_image renders', barcode_image('0735005653195', 300, 60) is not None)

    # the fixture itself must be a real symbol: pyzbar has to read it back
    round_trip = []
    for code in ('0735005653195', '012000161155', 'ABC-123', '5060466517814'):
        drawn = barcode_image(code, 640, 120)
        found = [item.data.decode('utf-8') for item in decode(np.array(drawn))]
        if code not in found:
            round_trip.append((code, found))
    check('barcode fixture decodes back via pyzbar', not round_trip,
          '{}'.format(round_trip))

    # and the app must expose no barcode *generation* at all - it only reads
    check('app does not generate barcodes',
          not hasattr(q, 'barcode_image') and not hasattr(q, 'code128b_modules'))

    check('money() formats', q.money(1234.5) == '$1,234.50', q.money(1234.5))
    check('symbol_type maps EAN13', q.symbol_type('EAN13') == 'EAN-13')
    name, price, size, aisle, est = q.guess_product('0735005653195')
    check('catalog hit', name == 'Sparkling Water Lime' and price == 4.99
          and aisle == 'Aisle 4' and est is False)
    name, price, _size, aisle, est = q.guess_product('9999999999999')
    check('unknown code has no price until set', est is True and price == 0.0
          and aisle == 'Unmapped', '{!r} {!r}'.format(price, aisle))
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.json')
    import os as _os
    _os.close(fd)
    q.save_price_override('9999999999999', 'Test Item', 7.77, path=tmp)
    q.PRICE_OVERRIDES.clear()
    q.load_price_overrides(path=tmp)
    name, price, _size, aisle, est = q.guess_product('9999999999999')
    check('price override round-trips', est is False and price == 7.77
          and name == 'Test Item', '{!r} {!r}'.format(name, price))
    _os.remove(tmp)
    check('BATCH covers every symbol', bool(q.ALL_SYMBOLS)
          and 'QRCODE' in q.ALL_SYMBOLS and 'EAN13' in q.ALL_SYMBOLS)

    # symbol names must reach zbar as enums - strings raise ArgumentError
    enums = q.symbol_enums(q.SCAN_MODES[0][1])
    check('symbol names map to ZBarSymbol enums',
          bool(enums) and all(not isinstance(item, str) for item in enums),
          '{} types'.format(len(enums)))
    check('unknown symbol names are dropped',
          q.symbol_enums(('EAN13', 'NOT_A_SYMBOL')) == q.symbol_enums(('EAN13',)))


def check_window():
    root = q.tb.Window(title='smoke')
    q.register_scancalc_theme(root.style)
    root.geometry('940x920+0+0')
    app = q.ScanCalcApp(root)
    root.update()

    check('theme registered', root.style.theme.name == q.THEME_NAME)
    check('window bg themed', root.cget('background').lower() == q.BG)
    check('footer nav has 4 tabs', len(app.nav_buttons) == 4)
    check('segmented modes = 3', len(app.mode_buttons) == 3)
    check('viewport mapped', app.viewport.winfo_width() > 100)
    check('page is scrollable', app.sf.winfo_reqheight() > 0)

    # -- synthetic scan through the real decode pipeline -------------------
    app.engine.decoder_started = True          # pretend the camera opened
    app.engine.error = None
    frame = synthetic_frame('0735005653195')
    app.engine.frame_queue.put(frame)
    app.engine.submit(frame)
    app._tick()
    root.update()

    check('scan pipeline decoded the code',
          app.pending_code == '0735005653195', repr(app.pending_code))
    check('product card populated',
          app.product_name.cget('text') == 'Sparkling Water Lime',
          app.product_name.cget('text'))
    check('price shown', app.product_price.cget('text') == '$4.99')
    check('aisle chip set', 'Aisle 4' in str(app.aisle_chip._text))
    check('viewport drew the frame', app._photo_id is not None)
    check('detection overlay active', app._overlay_visible())

    # the overlay must *show* the decoded value and generate no barcode graphic
    app._draw_viewport()
    root.update()
    drawn_text = [app.viewport.itemcget(item, 'text')
                  for item in app.viewport.find_all()
                  if app.viewport.type(item) == 'text']
    drawn_images = [item for item in app.viewport.find_all()
                    if app.viewport.type(item) == 'image']
    check('overlay shows the decoded payload', '0735005653195' in drawn_text,
          '{}'.format(drawn_text))
    check('overlay draws only the camera frame, no barcode graphic',
          len(drawn_images) == 1, '{} image items'.format(len(drawn_images)))

    # -- ADD -> running calculation ---------------------------------------
    app._add_current()
    root.update()
    check('history row created', len(app.rows) == 1)
    check('total = price + tax', app.total_label.cget('text') == '$5.34',
          app.total_label.cget('text'))
    check('items chip counts', '1 item tally' in str(app.items_chip._text),
          str(app.items_chip._text))
    check('budget left updated', '$44.66 left' in app.budget_left.cget('text'),
          app.budget_left.cget('text'))

    # -- quantity stepper --------------------------------------------------
    app._change_qty('0735005653195', 1)
    root.update()
    check('qty stepper adds', app.history[0]['qty'] == 2)
    check('total follows qty', app.total_label.cget('text') == '$10.68',
          app.total_label.cget('text'))
    app._change_qty('0735005653195', -2)
    root.update()
    check('qty 0 removes the line', len(app.history) == 0)

    # -- MULTI-SCAN --------------------------------------------------------
    app.duplicate_window = 0.0
    app._toggle_multi()
    root.update()
    check('multi-scan adopts the pending item', len(app.history) == 1
          and app.history[0]['qty'] == 1 and app.multi_scan,
          '{}'.format([(e['code'], e['qty']) for e in app.history]))

    before_qty = app.history[0]['qty']
    app.engine.submit(frame)
    app._tick()
    root.update()
    check('repeat scan ups qty by one', app.history[0]['qty'] == before_qty + 1,
          'qty={} (was {})'.format(app.history[0]['qty'], before_qty))

    # flipping the switch off and on again must not add another unit
    app._toggle_multi()
    app._toggle_multi()
    root.update()
    check('re-toggling multi-scan does not inflate qty',
          app.history[0]['qty'] == before_qty + 1,
          'qty={}'.format(app.history[0]['qty']))
    app._toggle_multi()

    # -- manual entry ------------------------------------------------------
    app.manual_entry.insert(0, '012000161155')
    app._submit_manual()
    root.update()
    check('manual code accepted', app.pending_code == '012000161155')
    check('manual entry cleared', app.manual_entry.get() == '')
    app._add_current()
    root.update()
    check('two distinct lines', len(app.rows) == 2)
    return root, app, frame


def check_window_rest(root, app, frame):
    # -- QR mode rejects barcodes ------------------------------------------
    app._set_mode(1)
    app.engine.submit(frame)
    app._tick()
    root.update()
    check('QR mode blocks barcode', len(app.history) == 2)
    check('mode chip follows', app.mode_chip._text == 'QR', app.mode_chip._text)
    app._set_mode(0)
    check('mode chip back', app.mode_chip._text == 'BARCODE')

    # -- toggles, zoom, budget ---------------------------------------------
    before = app.zoom
    app._cycle_zoom(1)
    check('zoom cycles', app.zoom != before
          and app.zoom_button._text == '{:g}x'.format(app.zoom))
    for index in (0, 3, 2):
        app.zoom_index = index
        app._dirty = True
        app._draw_viewport()
    check('redraw at 1.0x/2.0x/1.5x ok', True)

    app._toggle_scan()
    app._refresh_chips()
    check('pause pill', 'SCAN PAUSED' in str(app.fps_chip._text))
    app._toggle_scan()
    app._toggle_sound()
    check('sound toggles', app.beep_enabled is False)
    app._toggle_sound()
    app._toggle_exposure()
    app._refresh_chips()
    check('exposure pill flips', 'OFF' in app.exposure_button._text,
          app.exposure_button._text)
    app._toggle_exposure()
    app._toggle_lamp()
    check('lamp toggles', app.lamp_on is True)
    app._toggle_lamp()

    app.budget = 5.0                       # over-budget path
    app._refresh_totals()
    root.update()
    check('over-budget danger state', 'over' in app.budget_left.cget('text'),
          app.budget_left.cget('text'))
    app.budget = q.DEFAULT_BUDGET
    app._refresh_totals()

    # -- navigation + manual row -------------------------------------------
    for tab in ('CART', 'HISTORY', 'BUDGET', 'SCAN'):
        app._nav(tab)
        root.update()
    check('nav tab selected', app.nav_buttons['SCAN'].is_selected())
    app._toggle_manual()
    root.update()
    check('manual row shown', bool(app.manual_row.winfo_manager()))
    app._toggle_manual()
    root.update()
    check('manual row hidden', not app.manual_row.winfo_manager())

    # -- history upkeep + reset --------------------------------------------
    # the newest line is the single-unit manual entry: undo drops it outright
    newest_code = app.history[0]['code']
    expected_len = len(app.history) - 1
    app._undo_last()
    root.update()
    check('undo removes a 1-unit line',
          len(app.history) == expected_len
          and newest_code not in [e['code'] for e in app.history],
          '{}'.format([(e['code'], e['qty']) for e in app.history]))

    # a multi-unit line steps down by exactly one
    survivor = app.history[0]['code']
    app._change_qty(survivor, 1)
    root.update()
    before_qty = app.history[0]['qty']
    app._undo_last()
    root.update()
    check('undo decrements a multi-unit line',
          app.history[0]['qty'] == before_qty - 1,
          'qty={} (was {})'.format(app.history[0]['qty'], before_qty))

    app._remove_item(survivor)
    root.update()
    check('remove drops the line',
          survivor not in [e['code'] for e in app.history])
    app._clear_history()
    root.update()
    check('clear empties everything', len(app.history) == 0 and len(app.rows) == 0
          and app.total_label.cget('text') == '$0.00')
    check('placeholder restored', app.product_name.cget('text') == 'AWAITING SCAN')
    check('empty-state visible', bool(app.history_empty.winfo_manager()))

    # -- no-camera path ----------------------------------------------------
    app.engine.error = 'no camera on index 0'
    app._refresh_chips()
    app._dirty = True
    app._draw_viewport()
    root.update()
    check('camera-offline view draws', 'CAMERA OFFLINE' in str(app.fps_chip._text))

    app.on_close()
    check('clean shutdown', app._closing is True and app.engine._thread is None)


def main():
    if os.path.exists(REPORT_FILE):
        os.remove(REPORT_FILE)
    log('=== ScanCalc smoke test ===')
    check_helpers()
    root, app, frame = check_window()
    check_window_rest(root, app, frame)
    failures = [line for line in LINES if ' FAIL' in line]
    log('---')
    total = len([l for l in LINES if ' PASS' in l or ' FAIL' in l])
    log('TOTAL {} checks, {} failed'.format(total, len(failures)))
    log('RESULT: ' + ('ALL PASS' if not failures else 'FAILURES PRESENT'))
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log('CRASH:\n' + traceback.format_exc())
        raise SystemExit(3)