# Snaplish local capture watcher for this Windows laptop.
# New screenshots only. Images already on disk are left alone.

import ctypes
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import winsound
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import tkinter as tk
from PIL import Image, ImageGrab
import winocr

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
DATA = ROOT / "snaplish_data.json"
HOST = "127.0.0.1"
PORT = 8765

IDIOMS = [
    ("break a leg", "행운을 빌어!", "공연 전에 성공을 비는 말"),
    ("on the fence", "아직 결정을 못 하겠어", "망설일 때 쓰는 말"),
    ("touch base", "다시 짧게 이야기하자", "진행 상황을 맞출 때"),
    ("piece of cake", "정말 쉬운 일이야", "아주 쉽다는 뜻"),
    ("call off", "취소하다", "call off = 취소하다"),
    ("get the ball rolling", "시작을 끊다", "일을 시작하게 만들 때"),
    ("behind schedule", "일정보다 늦어지고 있어", "behind schedule = 일정 지연"),
    ("under the weather", "몸이 좀 안 좋아", "컨디션이 나쁠 때"),
    ("hit the books", "공부 시작할게", "본격적으로 공부할 때"),
    ("cost an arm and a leg", "엄청 비싸다", "가격이 매우 비쌀 때"),
    ("once in a blue moon", "아주 드물게", "거의 일어나지 않을 때"),
    ("spill the beans", "비밀을 말해버리다", "숨기던 이야기를 털어놓을 때"),
    ("ball is in your court", "이제 네가 정할 차례야", "상대의 결정을 기다릴 때"),
    ("by the way", "그런데", "화제를 바꿀 때"),
    ("look forward to", "기대하고 있어", "다가올 일을 기다릴 때"),
]

LOCK = threading.Lock()
LATEST = None
SAVED = []
UI = None
RECENT = []
LAST_AT = 0.0
LAST_CLIP = None
PAUSED_UNTIL = 0.0


def find_screenshot_dir():
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id = GUID()
    ctypes.oledll.ole32.CLSIDFromString("{B7BEDE81-DF94-4682-A7D8-57A52620B86F}", ctypes.byref(folder_id))
    path_ptr = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
    if result == 0 and path_ptr.value:
        folder = Path(path_ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return folder
    return Path.home() / "Pictures" / "Screenshots"


SHOT_DIR = find_screenshot_dir()


def load_saved():
    if not DATA.exists():
        return
    try:
        SAVED.extend(json.loads(DATA.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass


def persist():
    DATA.write_text(json.dumps(SAVED, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(img):
    small = img.convert("RGB").resize((32, 32))
    return hashlib.sha1(small.tobytes()).hexdigest()


def already_seen(image_hash):
    now = time.time()
    RECENT[:] = [(stamp, value) for stamp, value in RECENT if now - stamp < 12]
    if any(value == image_hash for _, value in RECENT):
        return True
    RECENT.append((now, image_hash))
    return False


def prepare(img):
    img = img.convert("RGB")
    if img.width > 1600:
        height = max(1, int(img.height * 1600 / img.width))
        img = img.resize((1600, height), Image.Resampling.LANCZOS)
    return img


def read_lines(img):
    prepared = prepare(img)
    errors = []
    for lang in ("ko", "en"):
        try:
            result = winocr.recognize_pil_sync(prepared, lang)
        except Exception as error:
            errors.append(error)
            continue
        found = []
        for line in result.get("lines") or []:
            text = re.sub(r"\s+", " ", (line.get("text") or "")).strip()
            if text:
                found.append(text)
        return rank_lines(found)
    if errors:
        raise errors[-1]
    return []


def rank_lines(lines):
    scored = []
    for text in lines:
        latin = sum(ch.isascii() and ch.isalpha() for ch in text)
        hangul = sum("가" <= ch <= "힣" for ch in text)
        words = re.findall(r"[A-Za-z']+", text)
        if latin < 4 or len(words) < 2 or hangul > latin:
            continue
        if text.isupper() and len(words) <= 2:
            continue
        scored.append((latin + len(words) * 8, text))
    scored.sort(reverse=True)
    picked = []
    for _, text in scored:
        if text not in picked:
            picked.append(text)
    return picked[:5]


def machine_translate(text):
    query = urllib.parse.quote(text[:400])
    url = "https://api.mymemory.translated.net/get?q=" + query + "&langpair=en|ko"
    request = urllib.request.Request(url, headers={"User-Agent": "Snaplish/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    translated = (data.get("responseData") or {}).get("translatedText") or ""
    if not translated or translated.upper().startswith("MYMEMORY"):
        raise RuntimeError(translated or "empty translation")
    return translated


def translate_line(text):
    lowered = text.lower()
    idiom = next((item for item in IDIOMS if item[0] in lowered), None)
    try:
        korean = machine_translate(text)
    except Exception:
        korean = ""
    if idiom and len(text.split()) <= 8:
        return idiom[1], idiom[2]
    if idiom:
        return korean or idiom[1], idiom[2]
    if korean:
        return korean, "화면에서 읽은 문장"
    return "번역을 가져오지 못했어요", "영어 문장만 먼저 띄웠어요"


def is_saved(english):
    return any(item.get("en") == english for item in SAVED)


def build_capture(lines, source):
    english = lines[0]
    korean, note = translate_line(english)
    return {
        "id": "cap-" + uuid.uuid4().hex[:8],
        "en": english,
        "ko": korean,
        "note": note,
        "lines": lines,
        "source": source,
        "tag": "캡처",
        "saved": is_saved(english),
        "live": True,
    }


def publish(capture):
    global LATEST
    with LOCK:
        LATEST = capture
    if UI:
        UI.notify(capture)


def remember(english, korean, note):
    with LOCK:
        if any(item.get("en") == english for item in SAVED):
            return False
        SAVED.insert(0, {
            "en": english,
            "ko": korean,
            "note": note,
            "source": "내 노트북",
            "tag": "캡처",
        })
        persist()
        if LATEST and LATEST.get("en") == english:
            LATEST["saved"] = True
    return True


def choose_line(text):
    korean, note = translate_line(text)
    with LOCK:
        if not LATEST:
            return None
        LATEST["en"] = text
        LATEST["ko"] = korean
        LATEST["note"] = note
        LATEST["saved"] = is_saved(text)
        capture = dict(LATEST)
        capture["lines"] = list(LATEST.get("lines") or [])
    if UI:
        UI.notify(capture)
    return capture


def speak(text):
    escaped = text.replace("'", "''")
    command = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = -1; "
        f"$s.Speak('{escaped}')"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", command],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def alerts_paused():
    return time.time() < PAUSED_UNTIL


def pause_alerts(minutes):
    global PAUSED_UNTIL
    PAUSED_UNTIL = time.time() + minutes * 60
    print(f"번역 알림 {minutes}분 중지", flush=True)
    if UI:
        UI.show_paused()


def resume_alerts():
    global PAUSED_UNTIL
    PAUSED_UNTIL = 0.0
    print("번역 알림 다시 켜짐", flush=True)
    if UI:
        UI.hide_paused()


def consider(img, source):
    global LAST_AT
    if alerts_paused():
        return
    image_hash = fingerprint(img)
    if already_seen(image_hash):
        return
    if time.time() - LAST_AT < 2.5:
        return
    LAST_AT = time.time()
    print("새 캡처 감지:", source, flush=True)
    if UI:
        UI.loading()
    try:
        winsound.MessageBeep(winsound.MB_OK)
    except RuntimeError:
        pass
    try:
        lines = read_lines(img)
    except Exception as error:
        print("글자 인식 오류", error, flush=True)
        lines = []
    if not lines:
        publish({
            "id": "cap-" + uuid.uuid4().hex[:8],
            "en": "영어 문장을 찾지 못했어요",
            "ko": "글자가 더 크게 보이는 부분을 다시 찍어 주세요",
            "note": "",
            "lines": [],
            "source": source,
            "tag": "캡처",
            "saved": True,
            "live": True,
        })
        return
    publish(build_capture(lines, source))
    print("번역:", lines[0], flush=True)


def check_clipboard():
    global LAST_CLIP
    data = ImageGrab.grabclipboard()
    if isinstance(data, Image.Image):
        img = data.copy()
        image_hash = fingerprint(img)
        if image_hash == LAST_CLIP:
            return
        LAST_CLIP = image_hash
        consider(img, "클립보드")
        return
    if isinstance(data, list):
        for path in data:
            if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
                try:
                    consider(Image.open(path), "클립보드 파일")
                except OSError:
                    pass


def check_folder(known):
    if not SHOT_DIR.exists():
        return
    for path in SHOT_DIR.iterdir():
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
            continue
        if path.name in known:
            continue
        known.add(path.name)
        opened = None
        for _ in range(4):
            try:
                with Image.open(path) as img:
                    opened = img.convert("RGB")
                break
            except OSError:
                time.sleep(0.4)
        if opened is not None:
            consider(opened, "스크린샷 폴더")


def watch():
    known = set()
    if SHOT_DIR.exists():
        known = {path.name for path in SHOT_DIR.iterdir()}
    try:
        current = ImageGrab.grabclipboard()
        if isinstance(current, Image.Image):
            global LAST_CLIP
            LAST_CLIP = fingerprint(current)
            already_seen(LAST_CLIP)
    except Exception:
        pass
    print("캡처 감시 시작", SHOT_DIR, flush=True)
    while True:
        try:
            check_clipboard()
            check_folder(known)
        except Exception as error:
            print("감시 오류", error, flush=True)
        time.sleep(0.45)


class AppUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = None
        self.card_pos = None
        self.chip_pos = None
        self.drag = None
        self.pause_chip = None
        self.pause_label = None

    def loading(self):
        self.root.after(0, lambda: self.render({
            "en": "캡처를 읽고 있어요",
            "ko": "글자를 인식한 뒤 바로 번역할게요",
            "note": "",
            "lines": [],
            "saved": True,
            "loading": True,
        }))

    def notify(self, capture):
        self.root.after(0, lambda: self.render(capture))

    def paint_round(self, canvas, width, height, radius, color):
        curve = min(radius, width // 2, height // 2)
        canvas.create_oval(0, 0, curve * 2, curve * 2, fill=color, outline=color)
        canvas.create_oval(width - curve * 2, 0, width, curve * 2, fill=color, outline=color)
        canvas.create_oval(0, height - curve * 2, curve * 2, height, fill=color, outline=color)
        canvas.create_oval(width - curve * 2, height - curve * 2, width, height, fill=color, outline=color)
        canvas.create_rectangle(curve, 0, width - curve, height, fill=color, outline=color)
        canvas.create_rectangle(0, curve, width, height - curve, fill=color, outline=color)

    def open_rounded(self, width, height, x, y, radius=26, inset=16):
        chroma = "#ff00ff"
        card = "#0b140d"
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=chroma)
        window.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
        canvas = tk.Canvas(window, width=width, height=height, bg=chroma, highlightthickness=0, bd=0)
        canvas.pack()
        self.paint_round(canvas, width, height, radius, card)
        shell = tk.Frame(canvas, bg=card)
        canvas.create_window(inset, inset, window=shell, anchor="nw", width=width - inset * 2, height=height - inset * 2)
        self.bind_drag(canvas)
        window.attributes("-transparentcolor", chroma)
        return window, shell

    def render(self, capture):
        if self.popup is not None:
            self.popup.destroy()
        width = 392
        lines = [line for line in capture.get("lines") or [] if line != capture.get("en")][:3]
        height = (262 if not capture.get("loading") else 176) + len(lines) * 36
        screen_w = self.root.winfo_screenwidth()
        if self.card_pos:
            origin_x, origin_y = self.card_pos
        else:
            origin_x, origin_y = screen_w - width - 28, 36
        popup, shell = self.open_rounded(width, height, origin_x, origin_y)
        self.popup = popup
        top = tk.Frame(shell, bg="#0b140d")
        top.pack(fill="x")
        title = tk.Label(top, text="Snaplish · 방금 캡처", bg="#0b140d", fg="#c9d4cb", font=("Malgun Gothic", 9))
        title.pack(side="left")
        tk.Button(
            top, text="×", command=popup.destroy, bg="#0b140d", fg="#ffffff",
            activebackground="#0b140d", activeforeground="#ffffff",
            relief="flat", bd=0, font=("Malgun Gothic", 12), cursor="hand2",
        ).pack(side="right")

        english = tk.Label(
            shell, text=f"“{capture.get('en', '')}”", bg="#0b140d", fg="#ffffff",
            font=("Malgun Gothic", 13, "bold"), wraplength=328, justify="left", anchor="w",
        )
        english.pack(fill="x", pady=(10, 2))
        note = capture.get("note") or ""
        korean = capture.get("ko") or ""
        detail = f"{korean}  ({note})" if note else korean
        meaning = tk.Label(
            shell, text=detail, bg="#0b140d", fg="#6ce182",
            font=("Malgun Gothic", 10), wraplength=328, justify="left", anchor="w",
        )
        meaning.pack(fill="x", pady=(0, 10))
        for widget in (shell, top, title, english, meaning):
            self.bind_drag(widget)

        if not capture.get("loading"):
            row = tk.Frame(shell, bg="#0b140d")
            row.pack(fill="x")
            saved = capture.get("saved")
            tk.Button(
                row,
                text="저장됨" if saved else "단어장 저장",
                command=lambda: self.save(capture),
                bg="#1c3324" if saved else "#6ce182",
                fg="#d9ffe3" if saved else "#0b140d",
                relief="flat", bd=0, padx=12, pady=6,
                font=("Malgun Gothic", 10, "bold"), cursor="hand2",
            ).pack(side="left")
            tk.Button(
                row, text="발음", command=lambda: speak(capture.get("en", "")),
                bg="#0b140d", fg="#ffffff", relief="flat", bd=1, padx=12, pady=6,
                highlightbackground="#355744", font=("Malgun Gothic", 10, "bold"), cursor="hand2",
            ).pack(side="left", padx=(8, 0))
            pause_row = tk.Frame(shell, bg="#0b140d")
            pause_row.pack(fill="x", pady=(10, 0))
            for minutes, label in ((10, "10분 끄기"), (30, "30분 끄기"), (60, "1시간 끄기")):
                tk.Button(
                    pause_row, text=label, command=lambda chosen=minutes: pause_alerts(chosen),
                    bg="#0b140d", fg="#c9d4cb", relief="flat", bd=1, padx=8, pady=4,
                    highlightbackground="#355744", font=("Malgun Gothic", 9), cursor="hand2",
                ).pack(side="left", padx=(0, 6))

            for line in lines:
                tk.Button(
                    shell, text=line, command=lambda chosen=line: choose_line(chosen),
                    bg="#0b140d", fg="#ffffff", relief="flat", bd=1, anchor="w",
                    padx=8, pady=4, font=("Malgun Gothic", 9), cursor="hand2",
                ).pack(fill="x", pady=(6, 0))
        popup.lift()

    def bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.drag_card)
        widget.configure(cursor="fleur")

    def start_drag(self, event):
        window = event.widget.winfo_toplevel()
        self.drag = (event.x_root, event.y_root, window.winfo_x(), window.winfo_y(), window)

    def drag_card(self, event):
        if not self.drag:
            return
        _, _, origin_x, origin_y, window = self.drag
        x = origin_x + event.x_root - self.drag[0]
        y = origin_y + event.y_root - self.drag[1]
        width = max(window.winfo_width(), 80)
        height = max(window.winfo_height(), 32)
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
        x = min(max(x, -width + 80), screen_w - 80)
        y = min(max(y, 0), screen_h - 48)
        window.geometry(f"+{x}+{y}")
        if window is self.popup:
            self.card_pos = (x, y)
        else:
            self.chip_pos = (x, y)

    def show_paused(self):
        if self.popup is not None:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None
        if self.pause_chip is not None:
            self.refresh_pause_label()
            return
        if self.chip_pos:
            origin_x, origin_y = self.chip_pos
        elif self.card_pos:
            origin_x, origin_y = self.card_pos
        else:
            origin_x, origin_y = self.root.winfo_screenwidth() - 340, 36
        chip, shell = self.open_rounded(320, 64, origin_x, origin_y, radius=22, inset=12)
        self.pause_chip = chip
        self.pause_label = tk.Label(shell, text="", bg="#0b140d", fg="#c9d4cb", font=("Malgun Gothic", 9))
        self.pause_label.pack(side="left")
        tk.Button(
            shell, text="다시 켜기", command=resume_alerts,
            bg="#6ce182", fg="#0b140d", relief="flat", bd=0, padx=8, pady=2,
            font=("Malgun Gothic", 9, "bold"), cursor="hand2",
        ).pack(side="right")
        self.bind_drag(shell)
        self.bind_drag(self.pause_label)
        self.refresh_pause_label()
        chip.lift()

    def refresh_pause_label(self):
        if self.pause_label is None:
            return
        left = max(0, int(PAUSED_UNTIL - time.time()))
        minutes = max(1, (left + 59) // 60)
        self.pause_label.configure(text=f"알림 꺼짐 · {minutes}분")

    def hide_paused(self):
        if self.pause_chip is not None:
            try:
                self.pause_chip.destroy()
            except tk.TclError:
                pass
            self.pause_chip = None
            self.pause_label = None

    def save(self, capture):
        if capture.get("loading"):
            return
        remember(capture.get("en", ""), capture.get("ko", ""), capture.get("note", ""))
        capture["saved"] = True
        self.render(capture)

    def pump(self):
        if self.popup is not None:
            try:
                self.popup.attributes("-topmost", True)
            except tk.TclError:
                self.popup = None
        if self.pause_chip is not None:
            if not alerts_paused():
                self.hide_paused()
            else:
                try:
                    self.pause_chip.attributes("-topmost", True)
                    self.refresh_pause_label()
                except tk.TclError:
                    self.pause_chip = None
                    self.pause_label = None
        self.root.after(700, self.pump)

    def run(self):
        self.pump()
        self.root.mainloop()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/latest":
            with LOCK:
                capture = dict(LATEST) if LATEST else None
                if capture and LATEST.get("lines"):
                    capture["lines"] = list(LATEST["lines"])
                saved = list(SAVED)
            self.json({
                "ok": True,
                "capture": capture,
                "saved": saved,
                "paused": alerts_paused(),
                "pausedFor": max(0, int(PAUSED_UNTIL - time.time())),
            })
            return
        if path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.json({"ok": False}, 400)
            return
        if path == "/api/save":
            remember(payload.get("en", ""), payload.get("ko", ""), payload.get("note", ""))
            self.json({"ok": True})
            return
        if path == "/api/pick":
            capture = choose_line(payload.get("text", ""))
            self.json({"ok": bool(capture), "capture": capture})
            return
        self.send_error(404)


def serve():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Snaplish http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


def main():
    global UI
    load_saved()
    UI = AppUI()
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    UI.run()


if __name__ == "__main__":
    main()
