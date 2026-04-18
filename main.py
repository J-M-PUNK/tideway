import threading
import webbrowser

import customtkinter as ctk

from app.settings import load_settings
from app.tidal_client import TidalClient

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

WINDOW_W, WINDOW_H = 520, 440


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Tidal Downloader")
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.resizable(False, False)

        self.tidal = TidalClient()
        self.settings = load_settings()
        self._oauth_url = ""

        self._build_login_frame()
        self._try_auto_login()

    # ------------------------------------------------------------------
    # Login frame
    # ------------------------------------------------------------------

    def _build_login_frame(self):
        self.login_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.login_frame.pack(expand=True, fill="both", padx=50, pady=40)

        ctk.CTkLabel(
            self.login_frame,
            text="Tidal Downloader",
            font=ctk.CTkFont(size=30, weight="bold"),
        ).pack(pady=(0, 6))

        ctk.CTkLabel(
            self.login_frame,
            text="High-quality music, downloaded.",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        ).pack(pady=(0, 36))

        self.status_label = ctk.CTkLabel(
            self.login_frame,
            text="Checking saved session…",
            font=ctk.CTkFont(size=13),
        )
        self.status_label.pack(pady=(0, 20))

        self.login_btn = ctk.CTkButton(
            self.login_frame,
            text="Login with Tidal",
            width=200,
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_login,
            state="disabled",
        )
        self.login_btn.pack(pady=(0, 20))

        # OAuth URL row — hidden until login is initiated
        self.url_frame = ctk.CTkFrame(self.login_frame, fg_color="transparent")

        self.url_entry = ctk.CTkEntry(self.url_frame, width=310, state="disabled")
        self.url_entry.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            self.url_frame,
            text="Copy",
            width=64,
            command=self._copy_url,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            self.url_frame,
            text="Open",
            width=64,
            command=self._open_url,
        ).pack(side="left")

        # Device code display — shown after OAuth starts
        self.code_frame = ctk.CTkFrame(self.login_frame, fg_color="transparent")

        ctk.CTkLabel(
            self.code_frame,
            text="Enter this code on the Tidal page:",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        ).pack(pady=(0, 6))

        self.code_label = ctk.CTkLabel(
            self.code_frame,
            text="",
            font=ctk.CTkFont(size=32, weight="bold"),
        )
        self.code_label.pack()

        self.auth_hint = ctk.CTkLabel(
            self.login_frame,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray",
            wraplength=380,
        )

    def _try_auto_login(self):
        def check():
            if self.tidal.load_session():
                self.after(0, self._on_login_success)
            else:
                self.after(0, lambda: self.status_label.configure(text="Not logged in."))
                self.after(0, lambda: self.login_btn.configure(state="normal"))

        threading.Thread(target=check, daemon=True).start()

    def _start_login(self):
        self.login_btn.configure(state="disabled")
        self.status_label.configure(text="Contacting Tidal…")

        def do_login():
            try:
                url, user_code, future = self.tidal.start_oauth_login()
                self.after(0, lambda: self._show_oauth_url(url, user_code))
                success = self.tidal.complete_login(future)
                self.after(0, lambda: self._on_login_complete(success))
            except Exception as exc:
                self.after(0, lambda: self._on_login_error(str(exc)))

        threading.Thread(target=do_login, daemon=True).start()

    def _show_oauth_url(self, url: str, user_code: str):
        self._oauth_url = url

        self.url_entry.configure(state="normal")
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        self.url_entry.configure(state="readonly")
        self.url_frame.pack(pady=(0, 10))

        self.code_label.configure(text=user_code)
        self.code_frame.pack(pady=(0, 10))

        self.auth_hint.configure(
            text="A browser window has opened. Type the code above into the Tidal page, then return here."
        )
        self.auth_hint.pack(pady=(0, 0))
        self.status_label.configure(text="Waiting for authentication…")

        webbrowser.open(url)

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self._oauth_url)

    def _open_url(self):
        if self._oauth_url:
            webbrowser.open(self._oauth_url)

    def _on_login_complete(self, success: bool):
        if success:
            self._on_login_success()
        else:
            self.url_frame.pack_forget()
            self.auth_hint.pack_forget()
            self.status_label.configure(text="Login failed or timed out. Please try again.")
            self.login_btn.configure(state="normal")

    def _on_login_error(self, error: str):
        self.status_label.configure(text=f"Error: {error}")
        self.login_btn.configure(state="normal")

    def _on_login_success(self):
        username = self.tidal.get_user_info() or "Tidal User"
        self.login_frame.pack_forget()
        self._build_main_frame(username)

    # ------------------------------------------------------------------
    # Main frame (placeholder — full UI built in gui.py next phase)
    # ------------------------------------------------------------------

    def _build_main_frame(self, username: str):
        from app.gui import MainWindow
        self.geometry("800x580")
        self.resizable(True, True)
        self.main_frame = MainWindow(
            parent=self,
            tidal=self.tidal,
            settings=self.settings,
            username=username,
            on_logout=self._logout,
        )
        self.main_frame.pack(expand=True, fill="both")

    def _logout(self):
        self.tidal.logout()
        self.main_frame.pack_forget()
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.resizable(False, False)
        self._build_login_frame()
        self.status_label.configure(text="Not logged in.")
        self.login_btn.configure(state="normal")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
