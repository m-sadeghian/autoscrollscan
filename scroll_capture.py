import ctypes, time, threading
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import cv2
import mss
from PIL import Image

# ---------- DPI awareness (باید قبل از ساخت Tk باشد) ----------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
VK_SHIFT, VK_ESCAPE = 0x10, 0x1B
KEYEVENTF_KEYUP = 0x2


class Cancelled(Exception):
    pass


# ---------- انتخاب ناحیه ----------
def select_region(root):
    result = {}
    top = tk.Toplevel(root)
    top.attributes("-fullscreen", True)
    top.attributes("-alpha", 0.3)
    top.attributes("-topmost", True)
    top.configure(bg="black", cursor="cross")
    canvas = tk.Canvas(top, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    st = {"x": 0, "y": 0, "rect": None}

    def press(e):
        st["x"], st["y"] = e.x_root, e.y_root
        st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                             outline="red", width=2, fill="white")

    def drag(e):
        canvas.coords(st["rect"], st["x"], st["y"], e.x, e.y)

    def release(e):
        x1, y1 = min(st["x"], e.x_root), min(st["y"], e.y_root)
        x2, y2 = max(st["x"], e.x_root), max(st["y"], e.y_root)
        if x2 - x1 > 20 and y2 - y1 > 20:
            result["r"] = (x1, y1, x2 - x1, y2 - y1)
        top.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    top.bind("<Escape>", lambda e: top.destroy())
    top.focus_force()
    root.wait_window(top)
    return result.get("r")


# ---------- موتور اسکرول و عکس‌برداری ----------
class Scanner:
    def __init__(self, region, delay, shift_mode, status):
        self.l, self.t, self.w, self.h = region
        self.mon = {"left": self.l, "top": self.t, "width": self.w, "height": self.h}
        self.delay = delay
        self.shift_mode = shift_mode
        self.status = status
        self.ppt = {"x": None, "y": None}        # پیکسل به ازای هر تیک ماوس
        self.ticks = {"x": 1, "y": 1}            # تیک‌های هر قدم
        self.last_ticks = {"x": 1, "y": 1}

    # --- ابزارها ---
    def check(self):
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            raise Cancelled()

    def grab(self):
        return np.ascontiguousarray(np.array(self.sct.grab(self.mon))[:, :, :3])

    def grab_stable(self):
        prev = self.grab()
        for _ in range(20):
            time.sleep(self.delay)
            cur = self.grab()
            if np.array_equal(cur, prev):
                return cur
            prev = cur
        return prev

    def move_mouse(self):
        user32.SetCursorPos(self.l + self.w // 2, self.t + self.h // 2)

    def _wheel(self, flag, notches):
        user32.mouse_event(flag, 0, 0, int(notches * 120) & 0xFFFFFFFF, 0)

    def scroll(self, axis, notches):
        """notches مثبت = حرکت به راست/پایین، منفی = چپ/بالا"""
        self.check()
        self.move_mouse()
        if notches > 0:
            self.last_ticks[axis] = notches
        if axis == "y":
            self._wheel(MOUSEEVENTF_WHEEL, -notches)
        elif self.shift_mode:
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
            self._wheel(MOUSEEVENTF_WHEEL, -notches)
            user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
        else:
            self._wheel(MOUSEEVENTF_HWHEEL, notches)

    def scroll_to_start(self, axis):
        last = self.grab_stable()
        while True:
            self.scroll(axis, -15)
            cur = self.grab_stable()
            if np.array_equal(cur, last):
                return
            last = cur

    # --- اندازه‌گیری جابجایی بین دو فریم ---
    def measure(self, prev, new, axis):
        if np.array_equal(prev, new):
            return 0
        g1 = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(new, cv2.COLOR_BGR2GRAY)
        h, w = g1.shape
        if axis == "x":
            templ, size = g2[:, : int(w * 0.35)], w
        else:
            templ, size = g2[: int(h * 0.35), :], h

        if templ.std() > 3:
            res = cv2.matchTemplate(g1, templ, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            s = loc[0] if axis == "x" else loc[1]
            if score > 0.97:
                if s >= 2:
                    self.ppt[axis] = s / self.last_ticks[axis]
                    self.ticks[axis] = max(1, int(0.5 * size / self.ppt[axis]))
                return s
        # ناحیه تقریباً خالی/سفید بود -> از مقدار قبلی استفاده کن
        if self.ppt[axis]:
            return int(round(self.ppt[axis] * self.last_ticks[axis]))
        raise RuntimeError("تشخیص میزان اسکرول ممکن نشد (ناحیه خالی است). "
                           "ناحیه را طوری انتخاب کنید که در ابتدای سند طرح داشته باشد.")

    # --- اجرای اصلی ---
    def run(self):
        with mss.mss() as sct:
            self.sct = sct
            self.status("رفتن به گوشه بالا-چپ ...")
            self.scroll_to_start("y")
            self.scroll_to_start("x")

            tiles, y, prev_first, row = [], 0, None, 0
            while True:
                self.check()
                first = self.grab_stable()
                if prev_first is not None:
                    dy = self.measure(prev_first, first, "y")
                    if dy < 2:
                        break
                    y += dy
                row += 1
                self.status(f"در حال اسکن ردیف {row} ...  (توقف: Esc)")
                tiles.append((0, y, first))
                frame, x = first, 0
                while True:
                    self.scroll("x", self.ticks["x"])
                    new = self.grab_stable()
                    dx = self.measure(frame, new, "x")
                    if dx < 2:
                        break
                    x += dx
                    tiles.append((x, y, new))
                    frame = new
                self.scroll_to_start("x")
                prev_first = first
                self.scroll("y", self.ticks["y"])

        self.status("در حال ترکیب تصاویر ...")
        W = max(x + f.shape[1] for x, _, f in tiles)
        H = max(y + f.shape[0] for _, y, f in tiles)
        canvas = np.full((H, W, 3), 255, np.uint8)
        for x, y, f in tiles:
            canvas[y:y + f.shape[0], x:x + f.shape[1]] = f
        return canvas


# ---------- رابط کاربری ----------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Scroll Capture")
        self.root.geometry("340x230")
        self.region = None

        ttk.Button(self.root, text="1) انتخاب ناحیه", command=self.pick).pack(pady=8, fill="x", padx=15)
        self.lbl = ttk.Label(self.root, text="ناحیه‌ای انتخاب نشده")
        self.lbl.pack()

        f = ttk.Frame(self.root); f.pack(pady=6)
        ttk.Label(f, text="تاخیر بعد از اسکرول (ثانیه):").grid(row=0, column=0)
        self.delay = tk.DoubleVar(value=0.15)
        ttk.Spinbox(f, from_=0.05, to=2, increment=0.05, textvariable=self.delay, width=6).grid(row=0, column=1)
        self.shift = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="اسکرول افقی با Shift+Wheel",
                        variable=self.shift).grid(row=1, column=0, columnspan=2, pady=4)

        ttk.Button(self.root, text="2) شروع", command=self.start).pack(pady=8, fill="x", padx=15)
        self.status = ttk.Label(self.root, text="")
        self.status.pack()

    def set_status(self, t):
        self.root.after(0, lambda: self.status.config(text=t))

    def pick(self):
        self.root.withdraw()
        time.sleep(0.3)
        r = select_region(self.root)
        self.root.deiconify()
        if r:
            self.region = r
            self.lbl.config(text=f"ناحیه: {r[2]}x{r[3]} در ({r[0]},{r[1]})")

    def start(self):
        if not self.region:
            messagebox.showwarning("خطا", "اول ناحیه را انتخاب کنید")
            return
        self.root.iconify()
        scanner = Scanner(self.region, self.delay.get(), self.shift.get(), self.set_status)

        def work():
            time.sleep(1.5)
            try:
                img = scanner.run()
                self.root.after(0, lambda: self.finish(img))
            except Cancelled:
                self.root.after(0, lambda: self.fail("لغو شد"))
            except Exception as e:
                self.root.after(0, lambda: self.fail(str(e)))

        threading.Thread(target=work, daemon=True).start()

    def finish(self, img):
        self.root.deiconify()
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")])
        if path:
            Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(path)
            self.status.config(text=f"ذخیره شد: {img.shape[1]}x{img.shape[0]}")

    def fail(self, msg):
        self.root.deiconify()
        messagebox.showerror("پیام", msg)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()