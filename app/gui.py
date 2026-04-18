import threading
import tkinter as tk
from tkinter import filedialog
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from app import image_cache
from app.downloader import Downloader, DownloadItem, DownloadStatus
from app.settings import Settings, save_settings
from app.tidal_client import TidalClient

STATUS_COLOR = {
    DownloadStatus.PENDING: "gray",
    DownloadStatus.FETCHING: "gray",
    DownloadStatus.IN_PROGRESS: "#1db954",
    DownloadStatus.TAGGING: "#1db954",
    DownloadStatus.COMPLETE: "#1db954",
    DownloadStatus.FAILED: "#e05252",
}

TYPE_COLOR = {
    "track": "#1db954",
    "album": "#3b82f6",
    "playlist": "#a855f7",
    "artist": "#f59e0b",
}


# ─────────────────────────────────────────────
# Shared primitives
# ─────────────────────────────────────────────

class SkeletonRow(ctk.CTkFrame):
    """A greyed-out placeholder row shown while real data loads."""

    def __init__(self, parent):
        super().__init__(parent, corner_radius=8, fg_color=("gray86", "gray18"), height=64)
        self.pack_propagate(False)

        thumb = ctk.CTkFrame(self, width=48, height=48,
                             fg_color=("gray78", "gray26"), corner_radius=4)
        thumb.pack(side="left", padx=(8, 10), pady=8)
        thumb.pack_propagate(False)

        text = ctk.CTkFrame(self, fg_color="transparent")
        text.pack(side="left", fill="both", expand=True, pady=14)

        ctk.CTkFrame(text, height=12, width=220,
                     fg_color=("gray78", "gray26"), corner_radius=2).pack(anchor="w", pady=(0, 6))
        ctk.CTkFrame(text, height=10, width=140,
                     fg_color=("gray80", "gray24"), corner_radius=2).pack(anchor="w")


class Thumb(ctk.CTkLabel):
    """A label that loads its image asynchronously."""

    def __init__(self, parent, size=(48, 48)):
        self._size = size
        super().__init__(parent, text="", image=image_cache.placeholder(size),
                         width=size[0], height=size[1])

    def load(self, url: str):
        if url:
            def _cb(img):
                try:
                    self.after(0, lambda: self.winfo_exists() and self.configure(image=img))
                except Exception:
                    pass
            image_cache.load_async(url, self._size, _cb)


def _image_url(obj, size: int) -> Optional[str]:
    try:
        return obj.image(size)
    except Exception:
        pass
    try:
        return obj.album.image(size)
    except Exception:
        pass
    try:
        pic = obj.picture
        if pic:
            return f"https://resources.tidal.com/images/{pic.replace('-', '/')}/{size}x{size}.jpg"
    except Exception:
        pass
    return None


def _title(obj) -> str:
    return getattr(obj, "name", "Unknown")


def _tidal_url(obj, content_type: str) -> Optional[str]:
    ident = getattr(obj, "id", None) or getattr(obj, "uuid", None)
    if ident is None:
        return None
    kind = {"track": "track", "album": "album",
            "artist": "artist", "playlist": "playlist"}.get(content_type)
    if not kind:
        return None
    return f"https://tidal.com/browse/{kind}/{ident}"


def _parent_album(obj):
    try:
        album = obj.album
        if album and getattr(album, "id", None):
            return album
    except Exception:
        pass
    return None


def _parent_artist(obj):
    try:
        artists = list(obj.artists)
        if artists:
            return artists[0]
    except Exception:
        pass
    try:
        artist = obj.artist
        if artist and getattr(artist, "id", None):
            return artist
    except Exception:
        pass
    return None


def _subtitle(obj, content_type: str) -> str:
    try:
        if content_type == "track":
            parts = []
            try:
                parts.append(", ".join(a.name for a in obj.artists))
            except Exception:
                pass
            try:
                parts.append(obj.album.name)
            except Exception:
                pass
            try:
                m, s = divmod(obj.duration, 60)
                parts.append(f"{m}:{s:02d}")
            except Exception:
                pass
            return " · ".join(parts)
        if content_type == "album":
            parts = []
            try:
                parts.append(obj.artist.name)
            except Exception:
                pass
            try:
                parts.append(str(obj.year))
            except Exception:
                pass
            try:
                parts.append(f"{obj.num_tracks} tracks")
            except Exception:
                pass
            return " · ".join(parts)
        if content_type == "playlist":
            try:
                return f"{obj.num_tracks} tracks"
            except Exception:
                return ""
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# Result row (search + library lists)
# ─────────────────────────────────────────────

class ResultRow(ctk.CTkFrame):
    def __init__(self, parent, obj, content_type: str,
                 on_download: Optional[Callable] = None,
                 on_browse: Optional[Callable] = None,
                 on_select: Optional[Callable] = None,
                 initial_selected: bool = False):
        super().__init__(parent, corner_radius=8, fg_color=("gray88", "gray17"))
        self._obj = obj
        self._content_type = content_type
        self._on_download = on_download
        self._on_browse = on_browse
        self._sel_var: Optional[ctk.BooleanVar] = None

        if on_select and content_type != "artist":
            self._sel_var = ctk.BooleanVar(value=initial_selected)
            ctk.CTkCheckBox(
                self, text="", width=24,
                checkbox_width=18, checkbox_height=18,
                variable=self._sel_var,
                command=lambda: on_select(obj, content_type, self._sel_var.get()),
            ).pack(side="left", padx=(10, 0))

        thumb = Thumb(self, size=(48, 48))
        thumb.pack(side="left", padx=(8, 0), pady=8)
        thumb.load(_image_url(obj, 80) or "")

        text = ctk.CTkFrame(self, fg_color="transparent")
        text.pack(side="left", fill="both", expand=True, padx=10, pady=8)

        ctk.CTkLabel(text, text=_title(obj),
                     font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x")
        sub = _subtitle(obj, content_type)
        if sub:
            ctk.CTkLabel(text, text=sub, font=ctk.CTkFont(size=11),
                         text_color="gray", anchor="w").pack(fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(side="right", padx=(0, 10))

        if on_browse and content_type in ("artist", "album"):
            ctk.CTkButton(
                btns, text="Browse →", width=84, height=28,
                fg_color="transparent", border_width=1,
                font=ctk.CTkFont(size=12),
                command=lambda: on_browse(obj, content_type),
            ).pack(side="left", padx=(0, 6))

        if on_download and content_type != "artist":
            ctk.CTkButton(
                btns, text="↓", width=34, height=28,
                font=ctk.CTkFont(size=13),
                command=lambda: on_download(obj, content_type),
            ).pack(side="left")

        self._bind_context_menu(self)
        for child in self._descendants(self):
            self._bind_context_menu(child)

    @staticmethod
    def _descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from ResultRow._descendants(child)

    def _bind_context_menu(self, widget):
        # Button-3 on Windows/Linux, Button-2 on macOS
        widget.bind("<Button-3>", self._show_context_menu, add="+")
        widget.bind("<Button-2>", self._show_context_menu, add="+")

    def _show_context_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        obj = self._obj
        ct = self._content_type

        if self._on_download and ct != "artist":
            label = {"track": "Download Track", "album": "Download Album",
                     "playlist": "Download Playlist"}.get(ct, "Download")
            menu.add_command(label=f"↓  {label}",
                             command=lambda: self._on_download(obj, ct))

        if self._on_browse:
            if ct == "track":
                album = _parent_album(obj)
                if album is not None:
                    menu.add_command(label="Browse Album",
                                     command=lambda a=album: self._on_browse(a, "album"))
                artist = _parent_artist(obj)
                if artist is not None:
                    menu.add_command(label="Browse Artist",
                                     command=lambda a=artist: self._on_browse(a, "artist"))
            elif ct == "album":
                artist = _parent_artist(obj)
                if artist is not None:
                    menu.add_command(label="Browse Artist",
                                     command=lambda a=artist: self._on_browse(a, "artist"))

        url = _tidal_url(obj, ct)
        if url:
            if menu.index("end") is not None:
                menu.add_separator()
            menu.add_command(label="Copy Tidal URL",
                             command=lambda u=url: self._copy_to_clipboard(u))

        if menu.index("end") is None:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_to_clipboard(self, text: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()
        except Exception:
            pass


# ─────────────────────────────────────────────
# Album detail page
# ─────────────────────────────────────────────

class AlbumDetail(ctk.CTkScrollableFrame):
    def __init__(self, parent, album, on_download: Callable):
        super().__init__(parent)

        # Header: cover + info
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=8, pady=(8, 16))

        cover = Thumb(hdr, size=(160, 160))
        cover.pack(side="left", padx=(0, 20))
        cover.load(_image_url(album, 320) or "")

        info = ctk.CTkFrame(hdr, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=8)

        ctk.CTkLabel(info, text=_title(album),
                     font=ctk.CTkFont(size=22, weight="bold"), anchor="w",
                     wraplength=400).pack(fill="x")
        ctk.CTkLabel(info, text=_subtitle(album, "album"),
                     font=ctk.CTkFont(size=13), text_color="gray", anchor="w").pack(fill="x", pady=(4, 14))
        ctk.CTkButton(info, text="↓  Download Album", width=170, height=36,
                      command=lambda: on_download(album, "album")).pack(anchor="w")

        # Divider
        ctk.CTkFrame(self, height=1, fg_color=("gray75", "gray30")).pack(fill="x", padx=8, pady=8)

        # Tracks
        loading = ctk.CTkLabel(self, text="Loading tracks…", text_color="gray")
        loading.pack(pady=20)

        def fetch():
            try:
                tracks = list(album.tracks())
                self.after(0, lambda: self._render_tracks(loading, tracks, on_download))
            except Exception as e:
                self.after(0, lambda: loading.configure(text=f"Error: {e}"))

        threading.Thread(target=fetch, daemon=True).start()

    def _render_tracks(self, loading_lbl, tracks, on_download):
        loading_lbl.destroy()
        for i, track in enumerate(tracks, 1):
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)

            ctk.CTkLabel(row, text=f"{i:02d}", width=32,
                         font=ctk.CTkFont(size=12), text_color="gray").pack(side="left")
            ctk.CTkLabel(row, text=track.name,
                         font=ctk.CTkFont(size=13), anchor="w").pack(side="left", fill="x", expand=True)
            try:
                m, s = divmod(track.duration, 60)
                ctk.CTkLabel(row, text=f"{m}:{s:02d}", width=44,
                             font=ctk.CTkFont(size=12), text_color="gray").pack(side="left")
            except Exception:
                pass
            ctk.CTkButton(row, text="↓", width=32, height=24,
                          command=lambda t=track: on_download(t, "track")).pack(side="left", padx=(6, 0))


# ─────────────────────────────────────────────
# Artist detail page
# ─────────────────────────────────────────────

class ArtistDetail(ctk.CTkScrollableFrame):
    def __init__(self, parent, artist, tidal: TidalClient,
                 on_download: Callable, on_browse: Callable):
        super().__init__(parent)

        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=8, pady=(8, 20))

        art = Thumb(hdr, size=(96, 96))
        art.pack(side="left", padx=(0, 16))
        art.load(_image_url(artist, 160) or "")

        ctk.CTkLabel(hdr, text=_title(artist),
                     font=ctk.CTkFont(size=24, weight="bold")).pack(side="left", anchor="w")

        # Top tracks
        ctk.CTkLabel(self, text="Top Tracks",
                     font=ctk.CTkFont(size=15, weight="bold"), anchor="w").pack(fill="x", padx=8, pady=(0, 6))

        top_lbl = ctk.CTkLabel(self, text="Loading…", text_color="gray")
        top_lbl.pack(anchor="w", padx=8)

        # Albums
        ctk.CTkLabel(self, text="Albums",
                     font=ctk.CTkFont(size=15, weight="bold"), anchor="w").pack(fill="x", padx=8, pady=(16, 6))

        album_lbl = ctk.CTkLabel(self, text="Loading…", text_color="gray")
        album_lbl.pack(anchor="w", padx=8)

        def fetch():
            top = tidal.get_artist_top_tracks(artist)
            albums = tidal.get_artist_albums(artist)
            self.after(0, lambda: self._render_top(top_lbl, top, on_download))
            self.after(0, lambda: self._render_albums(album_lbl, albums, on_download, on_browse))

        threading.Thread(target=fetch, daemon=True).start()

    def _render_top(self, lbl, tracks, on_download):
        lbl.destroy()
        for i, track in enumerate(tracks, 1):
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)
            ctk.CTkLabel(row, text=f"{i:02d}", width=32,
                         font=ctk.CTkFont(size=12), text_color="gray").pack(side="left")
            ctk.CTkLabel(row, text=track.name,
                         font=ctk.CTkFont(size=13), anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="↓", width=32, height=24,
                          command=lambda t=track: on_download(t, "track")).pack(side="left")

    def _render_albums(self, lbl, albums, on_download, on_browse):
        lbl.destroy()
        for album in albums:
            ResultRow(self, album, "album",
                      on_download=on_download, on_browse=on_browse).pack(fill="x", padx=8, pady=(0, 4))


# ─────────────────────────────────────────────
# Navigable content pane (used by Search + Library)
# ─────────────────────────────────────────────

class BrowsePane(ctk.CTkFrame):
    """Navigation pane with a persistent scroll frame — never destroyed, just cleared."""

    PAGE_SIZE = 30

    def __init__(self, parent, tidal: TidalClient, on_download: Callable):
        super().__init__(parent, fg_color="transparent")
        self.tidal = tidal
        self.on_download = on_download
        self._stack: list = []
        self._detail: Optional[ctk.CTkBaseClass] = None
        self._pending_rows: list = []   # (ct, obj) not yet rendered
        self._scroll_watch_id = None
        self._load_sentinel: Optional[ctk.CTkBaseClass] = None
        self._rendering_batch: bool = False
        self._scroll_bind_ids: list = []
        self._render_gen: int = 0  # bumped on every _clear_scroll; stale chunks drop
        self._selection: list = []  # list of (obj, content_type)
        self._bar_visible: bool = False

        # Grid layout: row 0 = nav, row 1 = scroll/detail (expands),
        # row 2 = selection bar (hidden via grid_remove() when not needed).
        # Using grid means we can hide/show the selection bar without
        # touching the scroll frame's layout, which was corrupting the
        # canvas' scrollregion in earlier pack-based attempts.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._nav = ctk.CTkFrame(self, fg_color="transparent")
        self._nav.grid(row=0, column=0, sticky="ew", padx=4)
        self._back_btn = ctk.CTkButton(
            self._nav, text="← Back", width=80, height=28,
            fg_color="transparent", border_width=1,
            font=ctk.CTkFont(size=12), command=self._go_back,
        )
        self._crumb = ctk.CTkLabel(
            self._nav, text="", font=ctk.CTkFont(size=12), text_color="gray",
        )

        self._selection_bar = ctk.CTkFrame(
            self, fg_color=("gray86", "gray20"), corner_radius=8, height=48,
        )
        self._selection_bar.pack_propagate(False)
        self._selection_label = ctk.CTkLabel(
            self._selection_bar, text="0 selected",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._selection_label.pack(side="left", padx=14)
        ctk.CTkButton(
            self._selection_bar, text="↓  Download Selected", width=170, height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._download_selected,
        ).pack(side="right", padx=(0, 10), pady=9)
        ctk.CTkButton(
            self._selection_bar, text="Clear", width=70, height=30,
            fg_color="transparent", border_width=1,
            font=ctk.CTkFont(size=12),
            command=self._clear_selection,
        ).pack(side="right", padx=(0, 8), pady=9)
        self._selection_bar.grid(row=2, column=0, sticky="ew", padx=4, pady=(4, 4))
        self._selection_bar.grid_remove()

        self._scroll = ctk.CTkScrollableFrame(self)
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(4, 0))

        # When the pane first becomes visible (e.g. after the user clicks
        # the Library tab for the first time), the canvas' scrollregion
        # may have been computed while the tab was hidden — reset to top.
        self.bind("<Map>", self._on_map, add="+")

    # ── public ──────────────────────────────────────
    def show_list(self, items_by_type: dict, breadcrumb: str = ""):
        self._push(("list", items_by_type, breadcrumb))

    def show_message(self, text: str):
        self._push(("msg", text, ""))

    def show_skeleton(self, count: int = 8):
        self._push(("skeleton", count, ""))

    # ── internal ────────────────────────────────────
    def _go_back(self):
        if len(self._stack) > 1:
            self._stack.pop()
            self._render()

    def _browse(self, obj, content_type: str):
        if content_type == "artist":
            self._push(("artist", obj, _title(obj)))
        elif content_type == "album":
            self._push(("album", obj, _title(obj)))

    def _push(self, state):
        self._stack.append(state)
        self._render()

    def _clear_scroll(self):
        self._render_gen += 1  # invalidates any in-flight chunk callbacks
        self._rendering_batch = False
        self._stop_scroll_watch()
        self._pending_rows = []
        self._load_sentinel = None
        # Selection refers to widgets about to be destroyed and to the
        # specific view being left; reset it so filter/sort/nav don't
        # carry stale selections forward.
        self._selection = []
        self._hide_selection_bar()
        for child in list(self._scroll.winfo_children()):
            child.destroy()
        # Reset the canvas to the top so newly rendered content isn't stuck
        # above a stale scroll offset from the previous list. We reset it
        # both immediately and after the layout settles, because the canvas'
        # scrollregion is recomputed asynchronously after children change.
        self._scroll_to_top()
        self.after(0, self._scroll_to_top)
        self.after(120, self._scroll_to_top)

    def _scroll_to_top(self):
        try:
            canvas = self._scroll._parent_canvas
            canvas.update_idletasks()
            # Recompute scrollregion from actual content bounds. When
            # content was packed while the pane was hidden, the canvas'
            # scrollregion can be stale/wrong, so yview_moveto(0) scrolls
            # to the "top" of a region that doesn't match reality.
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)
            canvas.yview_moveto(0)
        except Exception:
            pass

    def _on_map(self, _event=None):
        # Force the inner frame to re-size to the now-visible canvas width,
        # then reset scrollregion + view. The canvas' <Configure> binding
        # normally handles this, but it doesn't fire on show if the width
        # didn't change since the last (hidden) configure pass.
        try:
            canvas = self._scroll._parent_canvas
            canvas.update_idletasks()
            w = canvas.winfo_width()
            if w > 1:
                # CTkScrollableFrame tags its inner window; match any id.
                for iid in canvas.find_all():
                    canvas.itemconfigure(iid, width=w)
        except Exception:
            pass
        self._scroll_to_top()
        self.after(50, self._scroll_to_top)
        self.after(200, self._scroll_to_top)

    def _use_scroll(self):
        """Hide detail widget (if any) and bring scroll frame back."""
        if self._detail:
            self._detail.grid_forget()
            self._detail.destroy()
            self._detail = None
        if not self._scroll.winfo_ismapped():
            self._scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(4, 0))

    def _use_detail(self, widget):
        """Hide scroll frame, show a full-area detail widget."""
        self._stop_scroll_watch()
        self._scroll.grid_forget()
        if self._detail:
            self._detail.destroy()
        self._detail = widget
        self._detail.grid(row=1, column=0, sticky="nsew", padx=4)

    # ── infinite scroll ─────────────────────────────
    CHUNK_SIZE = 5  # rows to render per event-loop tick during a batch

    def _render_batch(self):
        if self._rendering_batch:
            return
        self._rendering_batch = True
        gen = self._render_gen

        if self._load_sentinel and self._load_sentinel.winfo_exists():
            self._load_sentinel.destroy()
        self._load_sentinel = None

        batch, self._pending_rows = (
            self._pending_rows[:self.PAGE_SIZE],
            self._pending_rows[self.PAGE_SIZE:],
        )
        # Render 5 rows per tick; yields to the event loop between chunks so
        # scrolling and input stay responsive while a batch is being built.
        self._render_chunks(batch, 0, gen)

    def _render_chunks(self, batch, start: int, gen: int):
        if gen != self._render_gen:
            # A new list/clear happened; abandon this stale batch.
            self._rendering_batch = False
            return
        end = min(start + self.CHUNK_SIZE, len(batch))
        for i in range(start, end):
            ct, obj = batch[i]
            try:
                ResultRow(self._scroll, obj, ct,
                          on_download=self.on_download,
                          on_browse=self._browse,
                          on_select=self._toggle_select,
                          initial_selected=self._is_selected(obj, ct)
                          ).pack(fill="x", pady=(0, 5))
            except Exception:
                pass
        if end < len(batch):
            self.after_idle(lambda: self._render_chunks(batch, end, gen))
        else:
            self._finalize_batch()

    # ── selection ───────────────────────────────
    def _toggle_select(self, obj, ct: str, checked: bool):
        if checked:
            if not self._is_selected(obj, ct):
                self._selection.append((obj, ct))
        else:
            self._selection = [
                (o, c) for (o, c) in self._selection
                if not (o is obj and c == ct)
            ]
        self._refresh_selection_bar()

    def _is_selected(self, obj, ct: str) -> bool:
        return any(o is obj and c == ct for (o, c) in self._selection)

    def _refresh_selection_bar(self):
        count = len(self._selection)
        if count == 0:
            self._hide_selection_bar()
            return
        self._selection_label.configure(text=f"{count} selected")
        self._show_selection_bar()

    def _show_selection_bar(self):
        if self._bar_visible:
            return
        self._bar_visible = True
        self._selection_bar.grid()

    def _hide_selection_bar(self):
        if not self._bar_visible:
            return
        self._bar_visible = False
        self._selection_bar.grid_remove()

    def _download_selected(self):
        items = list(self._selection)
        self._selection = []
        for child in self._scroll.winfo_children():
            if isinstance(child, ResultRow) and child._sel_var is not None:
                child._sel_var.set(False)
        self._hide_selection_bar()
        for obj, ct in items:
            try:
                self.on_download(obj, ct)
            except Exception:
                pass

    def _clear_selection(self):
        self._selection = []
        for child in self._scroll.winfo_children():
            if isinstance(child, ResultRow) and child._sel_var is not None:
                child._sel_var.set(False)
        self._hide_selection_bar()

    def _finalize_batch(self):
        if self._pending_rows:
            remaining = len(self._pending_rows)
            next_n = min(self.PAGE_SIZE, remaining)
            self._load_sentinel = ctk.CTkButton(
                self._scroll,
                text=f"↓  Load {next_n} more  ({remaining} remaining)",
                height=34, fg_color="transparent", border_width=1,
                font=ctk.CTkFont(size=12),
                command=self._render_batch,
            )
            self._load_sentinel.pack(pady=(10, 8))
        try:
            self._scroll.update_idletasks()
        except Exception:
            pass
        self.after(100, self._unblock_render)

    def _start_scroll_watch(self):
        self._stop_scroll_watch()
        try:
            canvas = self._scroll._parent_canvas
            # Fire an immediate check right after any scroll event.
            # Keep the bind IDs so we remove ONLY our handlers later.
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                bid = canvas.bind(seq, self._on_scroll_event, add="+")
                self._scroll_bind_ids.append((seq, bid))
        except Exception:
            pass
        # Safety-net poll in case no scroll events fire
        self._scroll_watch_id = self.after(300, self._poll_scroll)

    def _on_scroll_event(self, _event=None):
        # Small delay so the canvas has a chance to update its scroll position
        self.after(60, self._maybe_load_more)

    def _poll_scroll(self):
        if not self._pending_rows:
            self._stop_scroll_watch()
            return
        self._maybe_load_more()
        if self._pending_rows:
            self._scroll_watch_id = self.after(300, self._poll_scroll)

    def _maybe_load_more(self):
        if self._rendering_batch or not self._pending_rows:
            return
        if self._sentinel_is_visible():
            self._render_batch()

    def _sentinel_is_visible(self) -> bool:
        """Return True when the 'load more' sentinel has scrolled into view."""
        try:
            sentinel = self._load_sentinel
            if not sentinel or not sentinel.winfo_exists():
                return False
            canvas = self._scroll._parent_canvas
            canvas_top = canvas.winfo_rooty()
            canvas_bottom = canvas_top + canvas.winfo_height()
            sentinel_top = sentinel.winfo_rooty()
            # Visible when the sentinel's top is above the canvas bottom
            # *and* not so high that it's already scrolled past the viewport.
            return canvas_top - 20 < sentinel_top < canvas_bottom + 20
        except Exception:
            return False

    def _unblock_render(self):
        self._rendering_batch = False

    def _stop_scroll_watch(self):
        if self._scroll_watch_id is not None:
            try:
                self.after_cancel(self._scroll_watch_id)
            except Exception:
                pass
            self._scroll_watch_id = None
        # Remove only the specific bindings we added; never blanket-unbind
        # because that would nuke customtkinter's own scroll handlers.
        try:
            canvas = self._scroll._parent_canvas
            for seq, bid in self._scroll_bind_ids:
                try:
                    canvas.unbind(seq, bid)
                except Exception:
                    pass
        except Exception:
            pass
        self._scroll_bind_ids = []
        self._rendering_batch = False

    def _render(self):
        if not self._stack:
            return

        kind, data, crumb = self._stack[-1]

        # Nav bar
        if len(self._stack) > 1:
            self._back_btn.pack(side="left", padx=(2, 8))
            self._crumb.configure(text=crumb)
            self._crumb.pack(side="left")
        else:
            self._back_btn.pack_forget()
            self._crumb.pack_forget()

        dl = self.on_download
        br = self._browse

        if kind == "msg":
            self._use_scroll()
            self._clear_scroll()
            ctk.CTkLabel(self._scroll, text=data,
                         font=ctk.CTkFont(size=13), text_color="gray").pack(pady=50)

        elif kind == "skeleton":
            self._use_scroll()
            self._clear_scroll()
            for _ in range(int(data)):
                SkeletonRow(self._scroll).pack(fill="x", pady=(0, 5))

        elif kind == "list":
            self._use_scroll()
            self._clear_scroll()
            self._pending_rows = [
                (ct, obj) for ct, objs in data.items() for obj in objs
            ]
            if not self._pending_rows:
                ctk.CTkLabel(self._scroll, text="No results.",
                             text_color="gray").pack(pady=40)
            else:
                self._render_batch()
                if self._pending_rows:
                    self._start_scroll_watch()

        elif kind == "artist":
            self._use_detail(ArtistDetail(self, data, self.tidal, dl, br))

        elif kind == "album":
            self._use_detail(AlbumDetail(self, data, dl))


# ─────────────────────────────────────────────
# Search tab
# ─────────────────────────────────────────────

class SearchTab(ctk.CTkFrame):
    DEBOUNCE_MS = 350  # ms to wait after the last keystroke before searching

    def __init__(self, parent, tidal: TidalClient, on_download: Callable):
        super().__init__(parent, fg_color="transparent")
        self.tidal = tidal

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(10, 6))

        self.entry = ctk.CTkEntry(bar, height=40, font=ctk.CTkFont(size=13),
                                  placeholder_text="Search Tidal for artists, albums, tracks…")
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", lambda _: self._search_now())
        self.entry.bind("<KeyRelease>", self._on_key_release)

        ctk.CTkButton(bar, text="Search", width=90, height=40,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self._search_now).pack(side="left")

        self.filter_var = ctk.StringVar(value="All")
        self._last_results: dict = {}
        self._last_query: str = ""
        self._debounce_id = None
        self._search_gen: int = 0  # increments on every search; stale results drop
        self._filter_btn = ctk.CTkSegmentedButton(
            self, values=["All", "Artists", "Albums", "Tracks", "Playlists"],
            variable=self.filter_var, command=self._apply_filter,
            font=ctk.CTkFont(size=12),
        )
        self._filter_btn.pack(anchor="w", padx=4, pady=(0, 6))

        self.pane = BrowsePane(self, tidal=tidal, on_download=on_download)
        self.pane.pack(fill="both", expand=True)
        self.pane.show_message("Start typing to search Tidal…")

    def _on_key_release(self, event=None):
        # Ignore pure navigation keys so debounce isn't reset on arrow/shift/etc.
        if event and event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Return",
        ):
            return
        self._cancel_debounce()
        query = self.entry.get().strip()
        if not query:
            self._search_gen += 1  # invalidates any in-flight search
            self._last_query = ""
            self._last_results = {}
            self.pane._stack.clear()
            self.pane.show_message("Start typing to search Tidal…")
            return
        if query == self._last_query:
            return
        self._debounce_id = self.after(self.DEBOUNCE_MS, self._search_now)

    def _cancel_debounce(self):
        if self._debounce_id is not None:
            try:
                self.after_cancel(self._debounce_id)
            except Exception:
                pass
            self._debounce_id = None

    def _search_now(self):
        self._cancel_debounce()
        query = self.entry.get().strip()
        if not query or query == self._last_query:
            return
        self._last_query = query
        self._search_gen += 1
        gen = self._search_gen
        self.pane.show_skeleton(8)
        threading.Thread(target=self._do_search, args=(query, gen), daemon=True).start()

    def _do_search(self, query: str, gen: int):
        try:
            results = self.tidal.search(query)
        except Exception as e:
            if gen == self._search_gen:
                self.after(0, lambda: self.pane.show_message(f"Error: {e}"))
            return
        if gen != self._search_gen:
            return  # a newer query has been issued; drop these results
        self._last_results = results
        self.after(0, lambda: self._apply_filter(self.filter_var.get()))

    def _apply_filter(self, section: str):
        mapping = {
            "All":       ["artists", "albums", "tracks", "playlists"],
            "Artists":   ["artists"],
            "Albums":    ["albums"],
            "Tracks":    ["tracks"],
            "Playlists": ["playlists"],
        }
        keys = mapping.get(section, [])
        filtered = {k.rstrip("s"): self._last_results.get(k, []) for k in keys}
        self.pane._stack.clear()
        self.pane.show_list(filtered)


# ─────────────────────────────────────────────
# Library tab
# ─────────────────────────────────────────────

class LibraryTab(ctk.CTkFrame):
    KEY_MAP = {"Artists": "artist", "Albums": "album", "Songs": "track", "Playlists": "playlist"}
    SORT_OPTIONS = ["Date added", "A–Z", "Artist"]
    FILTER_DEBOUNCE_MS = 200

    def __init__(self, parent, tidal: TidalClient, on_download: Callable):
        super().__init__(parent, fg_color="transparent")
        self.tidal = tidal
        self._data: Dict[str, list] = {"artist": [], "album": [], "track": [], "playlist": []}
        self._section_ready: Dict[str, bool] = {"artist": False, "album": False, "track": False, "playlist": False}
        self._loading_started: bool = False
        self._filter_debounce_id = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(10, 4))

        self.section_var = ctk.StringVar(value="Albums")
        ctk.CTkSegmentedButton(
            top, values=["Artists", "Albums", "Songs", "Playlists"],
            variable=self.section_var, command=self._show_section,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")

        ctk.CTkButton(top, text="↺  Refresh", width=96, height=32,
                      fg_color="transparent", border_width=1,
                      font=ctk.CTkFont(size=12), command=self._load).pack(side="right")

        self.sort_var = ctk.StringVar(value="Date added")
        ctk.CTkOptionMenu(
            top, values=self.SORT_OPTIONS, variable=self.sort_var,
            command=lambda _: self._render_current(), width=130, height=32,
            font=ctk.CTkFont(size=12),
        ).pack(side="right", padx=(0, 8))

        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.pack(fill="x", padx=4, pady=(0, 6))
        self.filter_entry = ctk.CTkEntry(
            filter_row, height=32, font=ctk.CTkFont(size=12),
            placeholder_text="Filter this section…",
        )
        self.filter_entry.pack(fill="x")
        self.filter_entry.bind("<KeyRelease>", self._on_filter_key)

        self.pane = BrowsePane(self, tidal=tidal, on_download=on_download)
        self.pane.pack(fill="both", expand=True)
        self.pane.show_message("Click Refresh to load your library.")

    def _load(self):
        self._loading_started = True
        self._section_ready = {k: False for k in self._section_ready}
        self._data = {k: [] for k in self._data}
        self.pane._stack.clear()
        self.pane.show_skeleton(10)

        for ct in ("artist", "album", "track", "playlist"):
            threading.Thread(target=self._fetch_section, args=(ct,), daemon=True).start()

    def _fetch_section(self, ct: str):
        try:
            if ct == "artist":
                data = self.tidal.get_favorite_artists()
            elif ct == "album":
                data = self.tidal.get_favorite_albums()
            elif ct == "track":
                data = self.tidal.get_favorite_tracks()
            elif ct == "playlist":
                pl = self.tidal.get_favorite_playlists() + self.tidal.get_user_playlists()
                seen: set = set()
                data = []
                for p in pl:
                    pid = getattr(p, "id", None) or getattr(p, "uuid", None)
                    if pid not in seen:
                        seen.add(pid)
                        data.append(p)
            else:
                data = []
        except Exception:
            data = []

        self._data[ct] = data
        self._section_ready[ct] = True

        current_ct = self.KEY_MAP.get(self.section_var.get(), "album")
        if ct == current_ct:
            self.after(0, lambda: self._show_section(self.section_var.get()))

    def _on_filter_key(self, event=None):
        if event and event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Return",
        ):
            return
        if self._filter_debounce_id is not None:
            try:
                self.after_cancel(self._filter_debounce_id)
            except Exception:
                pass
        self._filter_debounce_id = self.after(
            self.FILTER_DEBOUNCE_MS, self._render_current
        )

    def _render_current(self):
        self._filter_debounce_id = None
        self._show_section(self.section_var.get())

    def _show_section(self, section: str):
        ct = self.KEY_MAP.get(section, "album")
        self.pane._stack.clear()
        if not self._section_ready.get(ct):
            if self._loading_started:
                self.pane.show_skeleton(10)
            else:
                self.pane.show_message("Click Refresh to load your library.")
            return
        items = self._data.get(ct, [])
        items = self._apply_filter(items, ct)
        items = self._apply_sort(items, ct)
        if items:
            self.pane.show_list({ct: items})
        else:
            query = self.filter_entry.get().strip()
            if query:
                self.pane.show_message(f'No matches for "{query}".')
            else:
                self.pane.show_message(
                    f"No {section.lower()} saved in your Tidal library."
                )

    def _apply_filter(self, items: list, ct: str) -> list:
        query = self.filter_entry.get().strip().lower()
        if not query:
            return items

        def haystack(obj) -> str:
            parts = [getattr(obj, "name", "") or ""]
            if ct == "track":
                try:
                    parts.append(", ".join(a.name for a in obj.artists))
                except Exception:
                    pass
                try:
                    parts.append(obj.album.name or "")
                except Exception:
                    pass
            elif ct == "album":
                try:
                    parts.append(obj.artist.name or "")
                except Exception:
                    pass
            return " ".join(parts).lower()

        return [o for o in items if query in haystack(o)]

    def _apply_sort(self, items: list, ct: str) -> list:
        mode = self.sort_var.get()
        if mode == "Date added":
            return items  # already in date-added order from tidal_client
        if mode == "A–Z":
            return sorted(items, key=lambda o: (getattr(o, "name", "") or "").lower())
        if mode == "Artist":
            def key(o):
                try:
                    return o.artist.name.lower()
                except Exception:
                    pass
                try:
                    return o.artists[0].name.lower()
                except Exception:
                    return (getattr(o, "name", "") or "").lower()
            return sorted(items, key=key)
        return items


# ─────────────────────────────────────────────
# Download queue tab
# ─────────────────────────────────────────────

class QueueRow(ctk.CTkFrame):
    def __init__(self, parent, item: DownloadItem):
        super().__init__(parent, corner_radius=8, fg_color=("gray88", "gray17"))
        self.item_id = item.item_id
        self.item = item

        tc = ctk.CTkFrame(self, fg_color="transparent")
        tc.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=(8, 10))

        self.title_lbl = ctk.CTkLabel(tc, text=item.title,
                                      font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self.title_lbl.pack(fill="x")

        self.sub_lbl = ctk.CTkLabel(tc, text=self._sub(item),
                                    font=ctk.CTkFont(size=11), text_color="gray", anchor="w")
        self.sub_lbl.pack(fill="x")

        self.bar = ctk.CTkProgressBar(tc, height=5)
        self.bar.set(item.progress)
        self.bar.pack(fill="x", pady=(5, 0))

        self.status_lbl = ctk.CTkLabel(
            self, text=item.status.value, font=ctk.CTkFont(size=11),
            text_color=STATUS_COLOR.get(item.status, "gray"), width=96, anchor="e",
        )
        self.status_lbl.pack(side="right", padx=(0, 14))

    @staticmethod
    def _sub(item: DownloadItem) -> str:
        return " · ".join(p for p in [item.artist, item.album] if p)

    def update(self, item: DownloadItem):
        self.title_lbl.configure(text=item.title)
        self.sub_lbl.configure(text=self._sub(item), text_color="gray")
        self.bar.set(item.progress)
        self.status_lbl.configure(text=item.status.value,
                                  text_color=STATUS_COLOR.get(item.status, "gray"))
        if item.status in (DownloadStatus.COMPLETE, DownloadStatus.FAILED):
            self.bar.pack_forget()
        if item.status == DownloadStatus.FAILED and item.error:
            self.sub_lbl.configure(text=item.error, text_color="#e05252")


class DownloadTab(ctk.CTkFrame):
    def __init__(self, parent, downloader: Downloader):
        super().__init__(parent, fg_color="transparent")
        self.downloader = downloader
        self._rows: Dict[str, QueueRow] = {}

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(10, 8))

        self.url_entry = ctk.CTkEntry(bar, height=40, font=ctk.CTkFont(size=13),
                                      placeholder_text="Paste a Tidal track, album, or playlist URL…")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.url_entry.bind("<Return>", lambda _: self._submit())

        ctk.CTkButton(bar, text="Download", width=110, height=40,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self._submit).pack(side="left")

        # Queue-management actions row
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkButton(actions, text="↻  Retry failed", width=120, height=28,
                      fg_color="transparent", border_width=1,
                      font=ctk.CTkFont(size=12),
                      command=self._retry_failed).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="✓  Clear completed", width=140, height=28,
                      fg_color="transparent", border_width=1,
                      font=ctk.CTkFont(size=12),
                      command=self._clear_completed).pack(side="left")

        self.scroll = ctk.CTkScrollableFrame(self, label_text="Queue")
        self.scroll.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.empty_lbl = ctk.CTkLabel(self.scroll,
                                      text="Paste a Tidal URL above and click Download.",
                                      font=ctk.CTkFont(size=13), text_color="gray")
        self.empty_lbl.pack(pady=50)

    def _submit(self):
        url = self.url_entry.get().strip()
        if url:
            self.url_entry.delete(0, "end")
            self.downloader.submit(url)

    def on_item_add(self, item: DownloadItem):
        self.after(0, lambda i=item: self._add_row(i))

    def on_item_update(self, item: DownloadItem):
        self.after(0, lambda i=item: self._update_row(i))

    def _add_row(self, item: DownloadItem):
        self.empty_lbl.pack_forget()
        row = QueueRow(self.scroll, item)
        row.pack(fill="x", pady=(0, 6))
        self._rows[item.item_id] = row

    def _update_row(self, item: DownloadItem):
        row = self._rows.get(item.item_id)
        if row:
            row.update(item)

    def _retry_failed(self):
        for row in list(self._rows.values()):
            if row.item.status == DownloadStatus.FAILED:
                self.downloader.retry(row.item)

    def _clear_completed(self):
        for item_id in list(self._rows.keys()):
            row = self._rows[item_id]
            if row.item.status == DownloadStatus.COMPLETE:
                row.destroy()
                del self._rows[item_id]
        if not self._rows:
            self.empty_lbl.pack(pady=50)


# ─────────────────────────────────────────────
# Settings dialog
# ─────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent)
        self.settings = settings
        self.title("Settings")
        self.geometry("480x370")
        self.resizable(False, False)
        self.grab_set()

        def lbl(t):
            return ctk.CTkLabel(self, text=t, anchor="w")

        p = {"padx": 24, "pady": (10, 2)}

        lbl("Output Directory").pack(fill="x", **p)
        dr = ctk.CTkFrame(self, fg_color="transparent")
        dr.pack(fill="x", padx=24, pady=(0, 10))
        self.dir_e = ctk.CTkEntry(dr, width=360)
        self.dir_e.insert(0, settings.output_dir)
        self.dir_e.pack(side="left", padx=(0, 8))
        ctk.CTkButton(dr, text="Browse", width=72, command=self._browse).pack(side="left")

        lbl("Audio Quality").pack(fill="x", **p)
        self.q_var = ctk.StringVar(value=settings.quality)
        ctk.CTkOptionMenu(self, width=220,
                          values=["high_lossless", "hi_res", "hi_res_lossless"],
                          variable=self.q_var).pack(anchor="w", padx=24, pady=(0, 10))

        lbl("Filename Template").pack(fill="x", **p)
        self.tmpl_e = ctk.CTkEntry(self, width=432)
        self.tmpl_e.insert(0, settings.filename_template)
        self.tmpl_e.pack(anchor="w", padx=24, pady=(0, 4))
        ctk.CTkLabel(self, text="Available: {title}  {artist}  {album}  {track_num}",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=24, pady=(0, 10))

        self.skip_var = ctk.BooleanVar(value=settings.skip_existing)
        ctk.CTkCheckBox(
            self, text="Skip files already on disk", variable=self.skip_var,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=24, pady=(0, 18))

        ctk.CTkButton(self, text="Save", width=110, command=self._save).pack()

    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.settings.output_dir)
        if path:
            self.dir_e.delete(0, "end")
            self.dir_e.insert(0, path)

    def _save(self):
        self.settings.output_dir = self.dir_e.get().strip()
        self.settings.quality = self.q_var.get()
        self.settings.filename_template = self.tmpl_e.get().strip()
        self.settings.skip_existing = self.skip_var.get()
        save_settings(self.settings)
        self.destroy()


# ─────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────

class MainWindow(ctk.CTkFrame):
    def __init__(self, parent, tidal: TidalClient, settings: Settings,
                 username: str, on_logout: Callable):
        super().__init__(parent, fg_color="transparent")
        self.tidal = tidal
        self.settings = settings
        self.on_logout = on_logout

        self._build_header(username)

        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        for name in ("Search", "Library", "Download"):
            self.tabs.add(name)

        # Download tab (needs downloader first)
        self.dl_tab = DownloadTab(self.tabs.tab("Download"), downloader=None)

        downloader = Downloader(
            tidal_client=tidal, settings=settings,
            on_add=self.dl_tab.on_item_add,
            on_update=self.dl_tab.on_item_update,
        )
        self.dl_tab.downloader = downloader
        self.dl_tab.pack(fill="both", expand=True)

        def queue_download(obj, content_type):
            downloader.submit_object(obj, content_type)
            self.tabs.set("Download")  # switch to queue tab

        self.search_tab = SearchTab(self.tabs.tab("Search"), tidal=tidal,
                                    on_download=queue_download)
        self.search_tab.pack(fill="both", expand=True)

        self.library_tab = LibraryTab(self.tabs.tab("Library"), tidal=tidal,
                                      on_download=queue_download)
        self.library_tab.pack(fill="both", expand=True)

        self._bind_shortcuts()

    def _bind_shortcuts(self):
        root = self.winfo_toplevel()
        root.bind("<Control-KeyPress-1>", lambda _e: self._go_tab("Search"))
        root.bind("<Control-KeyPress-2>", lambda _e: self._go_tab("Library"))
        root.bind("<Control-KeyPress-3>", lambda _e: self._go_tab("Download"))
        root.bind("<Control-f>", self._focus_search)
        root.bind("<Control-F>", self._focus_search)
        root.bind("<Control-l>", self._focus_library_filter)
        root.bind("<Control-L>", self._focus_library_filter)
        root.bind("<F5>", lambda _e: self.library_tab._load())

    def _go_tab(self, name: str):
        try:
            self.tabs.set(name)
        except Exception:
            pass

    def _focus_search(self, _event=None):
        self._go_tab("Search")
        try:
            self.search_tab.entry.focus_set()
            self.search_tab.entry.select_range(0, "end")
        except Exception:
            pass
        return "break"

    def _focus_library_filter(self, _event=None):
        self._go_tab("Library")
        try:
            self.library_tab.filter_entry.focus_set()
            self.library_tab.filter_entry.select_range(0, "end")
        except Exception:
            pass
        return "break"

    def _build_header(self, username: str):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(14, 4))

        ctk.CTkLabel(hdr, text="Tidal Downloader",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")

        ctk.CTkButton(hdr, text="Logout", width=76, height=28,
                      fg_color="transparent", border_width=1,
                      font=ctk.CTkFont(size=12), command=self.on_logout).pack(side="right")
        ctk.CTkButton(hdr, text="⚙  Settings", width=96, height=28,
                      fg_color="transparent", border_width=1,
                      font=ctk.CTkFont(size=12),
                      command=lambda: SettingsDialog(self, self.settings)).pack(side="right", padx=(0, 8))
        ctk.CTkLabel(hdr, text=username, font=ctk.CTkFont(size=12),
                     text_color="gray").pack(side="right", padx=(0, 10))
