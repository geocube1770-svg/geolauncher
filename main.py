import pathlib
import sys
import os
import json
import subprocess
import zipfile
import requests
import minecraft_launcher_lib

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox,
    QProgressBar, QFrame, QSizePolicy, QStackedWidget, QSlider,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox,
    QScrollArea, QMessageBox, QInputDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QByteArray
from PyQt6.QtGui import QPixmap, QIcon


# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

LAUNCHER_DIR  = os.path.join(os.getcwd(), "minecraft")
PROFILES_FILE = os.path.join(os.getcwd(), "profiles.json")
UA            = "geocube1770/GeoLauncher/0.3"
MODRINTH_API  = "https://api.modrinth.com/v2"


def load_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"Default": {"version": "1.21.1", "loader": "vanilla",
                        "directory": LAUNCHER_DIR}}


def save_profiles(profiles):
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=4)


# ═══════════════════════════════════════════════════════
#  Threads
# ═══════════════════════════════════════════════════════

class InstallThread(QThread):
    progress     = pyqtSignal(int)
    max_progress = pyqtSignal(int)
    status       = pyqtSignal(str)
    finished     = pyqtSignal(list)
    error        = pyqtSignal(str)

    def __init__(self, version, loader, mc_dir, options):
        super().__init__()
        self.version = version
        self.loader  = loader
        self.mc_dir  = mc_dir
        self.options = options

    def run(self):
        try:
            cb = {
                "setStatus":   lambda t: self.status.emit(t),
                "setProgress": lambda v: self.progress.emit(v),
                "setMax":      lambda v: self.max_progress.emit(v),
            }
            self.status.emit(f"Installing vanilla {self.version}…")
            minecraft_launcher_lib.install.install_minecraft_version(
                self.version, self.mc_dir, cb)
            launch_version = self.version

            if self.loader == "fabric":
                self.status.emit(f"Installing Fabric for {self.version}…")
                fl = minecraft_launcher_lib.fabric.get_latest_loader_version()
                minecraft_launcher_lib.fabric.install_fabric(
                    self.version, self.mc_dir, loader_version=fl, callback=cb)
                launch_version = f"fabric-loader-{fl}-{self.version}"

            elif self.loader == "forge":
                self.status.emit(f"Installing Forge for {self.version}…")
                fv = minecraft_launcher_lib.forge.find_forge_version(self.version)
                if not fv:
                    self.error.emit(f"No Forge version found for {self.version}")
                    return
                minecraft_launcher_lib.forge.install_forge_version(
                    fv, self.mc_dir, callback=cb)
                launch_version = fv

            cmd = minecraft_launcher_lib.command.get_minecraft_command(
                launch_version, self.mc_dir, self.options)
            self.finished.emit(cmd)
        except Exception as e:
            self.error.emit(str(e))


class ModrinthSearchThread(QThread):
    results = pyqtSignal(list)
    error   = pyqtSignal(str)

    def __init__(self, query, project_type, version, loader, sort_index):
        super().__init__()
        self.query        = query
        self.project_type = project_type
        self.version      = version
        self.loader       = loader
        self.sort_index   = sort_index

    def run(self):
        try:
            sort_map = {0: "downloads", 1: "follows", 2: "newest", 3: "updated"}
            facets   = [[f"project_type:{self.project_type}"]]
            if self.version:
                facets.append([f"versions:{self.version}"])
            # FIX: skip loader facet for vanilla (not a valid Modrinth category)
            if self.loader and self.loader != "vanilla" and self.project_type == "mod":
                facets.append([f"categories:{self.loader}"])

            params = {
                "limit":  20,
                "index":  sort_map.get(self.sort_index, "downloads"),
                "facets": json.dumps(facets),
            }
            if self.query:
                params["query"] = self.query

            r = requests.get(f"{MODRINTH_API}/search", params=params,
                             headers={"User-Agent": UA}, timeout=12)
            r.raise_for_status()
            self.results.emit(r.json().get("hits", []))
        except Exception as e:
            self.error.emit(str(e))


class FetchIconThread(QThread):
    done = pyqtSignal(int, bytes)

    def __init__(self, row, url):
        super().__init__()
        self.row = row
        self.url = url

    def run(self):
        try:
            r = requests.get(self.url, timeout=6, headers={"User-Agent": UA})
            if r.status_code == 200:
                self.done.emit(self.row, r.content)
        except Exception:
            pass


class DownloadFileThread(QThread):
    status   = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, version_id, dest_folder):
        super().__init__()
        self.version_id  = version_id
        self.dest_folder = dest_folder

    def run(self):
        try:
            self.status.emit("Fetching download info…")
            r = requests.get(f"{MODRINTH_API}/version/{self.version_id}",
                             headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            files = r.json().get("files", [])
            if not files:
                self.error.emit("No files found.")
                return

            primary = next((f for f in files if f.get("primary")), files[0])
            url   = primary["url"]
            fname = primary["filename"]

            self.status.emit(f"Downloading {fname}…")
            pathlib.Path(self.dest_folder).mkdir(parents=True, exist_ok=True)
            dest = os.path.join(self.dest_folder, fname)

            with requests.get(url, stream=True, timeout=120,
                              headers={"User-Agent": UA}) as resp:
                resp.raise_for_status()
                total      = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            self.finished.emit(dest)
        except Exception as e:
            self.error.emit(str(e))


class InstallModpackThread(QThread):
    status   = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(self, mrpack_path, profile_dir):
        super().__init__()
        self.mrpack_path = mrpack_path
        self.profile_dir = profile_dir

    def run(self):
        try:
            import shutil
            self.status.emit("Unpacking modpack…")
            tmp = os.path.join(self.profile_dir, "_mrpack_tmp")
            pathlib.Path(tmp).mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(self.mrpack_path, "r") as z:
                z.extractall(tmp)

            index_path = os.path.join(tmp, "modrinth.index.json")
            if not os.path.exists(index_path):
                self.error.emit("modrinth.index.json not found.")
                return

            with open(index_path) as f:
                index = json.load(f)

            files = index.get("files", [])
            total = len(files)

            for i, entry in enumerate(files):
                url      = entry["downloads"][0]
                rel_path = entry["path"]
                dest     = os.path.join(self.profile_dir, rel_path)
                pathlib.Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
                self.status.emit(
                    f"({i+1}/{total}) {os.path.basename(rel_path)}")
                self.progress.emit(i + 1, total)
                with requests.get(url, stream=True, timeout=60,
                                  headers={"User-Agent": UA}) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=65536):
                            fh.write(chunk)

            overrides = os.path.join(tmp, "overrides")
            if os.path.isdir(overrides):
                self.status.emit("Copying overrides…")
                for item in os.listdir(overrides):
                    src = os.path.join(overrides, item)
                    dst = os.path.join(self.profile_dir, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)

            shutil.rmtree(tmp, ignore_errors=True)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


# ═══════════════════════════════════════════════════════
#  Card widget
# ═══════════════════════════════════════════════════════

class ContentCardWidget(QWidget):
    action_requested = pyqtSignal(dict)

    def __init__(self, project, action_label="⬇ Download", parent=None):
        super().__init__(parent)
        self.project = project
        self.setObjectName("contentCard")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(14)

        self.icon_label = QLabel("…")
        self.icon_label.setFixedSize(56, 56)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setObjectName("modIcon")
        layout.addWidget(self.icon_label)

        info = QVBoxLayout()
        info.setSpacing(3)

        top = QHBoxLayout()
        name = QLabel(f"<b>{project.get('title','Unknown')}</b>")
        name.setObjectName("modName")
        author = QLabel(f"by {project.get('author','?')}")
        author.setObjectName("modAuthor")
        dl = QLabel(f"⬇ {project.get('downloads',0):,}")
        dl.setObjectName("modDownloads")
        top.addWidget(name)
        top.addWidget(author)
        top.addStretch()
        top.addWidget(dl)

        badges = QHBoxLayout()
        badges.setSpacing(5)
        for cat in project.get("categories", [])[:4]:
            b = QLabel(cat)
            b.setObjectName("badge")
            badges.addWidget(b)
        badges.addStretch()

        desc = QLabel(project.get("description",""))
        desc.setObjectName("modDesc")
        desc.setWordWrap(True)

        info.addLayout(top)
        info.addLayout(badges)
        info.addWidget(desc)
        layout.addLayout(info, 1)

        self.dl_btn = QPushButton(action_label)
        self.dl_btn.setObjectName("dlBtn")
        self.dl_btn.setFixedWidth(112)
        self.dl_btn.clicked.connect(lambda: self.action_requested.emit(self.project))
        layout.addWidget(self.dl_btn)

    def set_icon(self, pixmap):
        self.icon_label.setText("")
        self.icon_label.setPixmap(
            pixmap.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation))

    def set_status(self, text):
        self.dl_btn.setText(text)
        self.dl_btn.setEnabled(False)

    def reset_button(self, label):
        self.dl_btn.setText(label)
        self.dl_btn.setEnabled(True)


# ═══════════════════════════════════════════════════════
#  Generic browse panel
# ═══════════════════════════════════════════════════════

class BrowsePanel(QFrame):
    def __init__(self, project_type, action_label, launcher, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.project_type  = project_type
        self.action_label  = action_label
        self.launcher      = launcher
        self.search_thread = None
        self.icon_threads  = []

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(28, 22, 28, 22)

        title_text = "Mods" if project_type == "mod" else "Modpack Downloader"
        self.title_label = QLabel(title_text)
        self.title_label.setObjectName("title")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.title_label)

        self.profile_label = QLabel("")
        self.profile_label.setObjectName("subtitle")
        self.profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.profile_label)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        placeholder = ("Search mods… (empty = popular)"
                       if project_type == "mod"
                       else "Search modpacks… (empty = popular)")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(placeholder)
        self.search_input.returnPressed.connect(self.do_search)
        controls.addWidget(self.search_input, 3)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "Most Downloaded", "Most Followed", "Newest", "Recently Updated"])
        self.sort_combo.setFixedWidth(188)
        self.sort_combo.currentIndexChanged.connect(self.do_search)
        controls.addWidget(self.sort_combo, 1)

        search_btn = QPushButton("Search")
        search_btn.setFixedWidth(85)
        search_btn.clicked.connect(self.do_search)
        controls.addWidget(search_btn)
        root.addLayout(controls)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("modsScroll")
        self.container = QWidget()
        self.container.setObjectName("modsContainer")
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setSpacing(6)
        self.vbox.setContentsMargins(4, 4, 4, 4)
        self.vbox.addStretch()
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll)

        self.status_label = QLabel("Loading popular content…")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status_label)

        self.dl_progress = QProgressBar()
        self.dl_progress.setValue(0)
        self.dl_progress.setVisible(False)
        root.addWidget(self.dl_progress)

        if project_type == "mod":
            btn = QPushButton("📂 Open Active Profile's Mods Folder")
            btn.clicked.connect(self._open_mods_folder)
            root.addWidget(btn)

    def refresh_profile_label(self):
        data    = self.launcher.profiles.get(self.launcher.current_profile_name, {})
        loader  = data.get("loader", "?").capitalize()
        version = data.get("version", "?")
        if self.project_type == "mod":
            self.profile_label.setText(
                f"Active: {self.launcher.current_profile_name}  [{loader} {version}]")
        else:
            self.profile_label.setText(
                f"Installing to: {self.launcher.current_profile_name}  [{loader} {version}]")

    def load_popular(self):
        self.do_search()

    def do_search(self):
        if self.search_thread and self.search_thread.isRunning():
            self.search_thread.terminate()

        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name, {})
        version = profile_data.get("version", "")
        loader  = profile_data.get("loader", "vanilla")
        if self.project_type == "modpack":
            loader = ""

        query = self.search_input.text().strip()
        self.status_label.setText(
            "Loading popular…" if not query else f"Searching '{query}'…")
        self._clear_cards()

        self.search_thread = ModrinthSearchThread(
            query, self.project_type, version, loader,
            self.sort_combo.currentIndex())
        self.search_thread.results.connect(self._on_results)
        self.search_thread.error.connect(
            lambda e: self.status_label.setText(f"Error: {e}"))
        self.search_thread.start()

    def _on_results(self, hits):
        self._clear_cards()
        self.icon_threads.clear()

        if not hits:
            self.status_label.setText("No results found.")
            return

        for idx, project in enumerate(hits):
            card = ContentCardWidget(project, self.action_label)
            card.action_requested.connect(self._on_action)
            self.vbox.insertWidget(self.vbox.count() - 1, card)
            icon_url = project.get("icon_url", "")
            if icon_url:
                t = FetchIconThread(idx, icon_url)
                t.done.connect(self._on_icon)
                t.start()
                self.icon_threads.append(t)

        q = self.search_input.text().strip()
        s = self.sort_combo.currentText()
        self.status_label.setText(
            f"{len(hits)} result(s) for '{q}' — {s}" if q
            else f"Top {len(hits)} popular — {s}")

    def _on_icon(self, row, data):
        item = self.vbox.itemAt(row)
        if item and item.widget():
            px = QPixmap()
            px.loadFromData(QByteArray(data))
            if not px.isNull():
                item.widget().set_icon(px)

    def _clear_cards(self):
        while self.vbox.count() > 1:
            it = self.vbox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _open_mods_folder(self):
        data = self.launcher.profiles.get(self.launcher.current_profile_name, {})
        mods = os.path.join(data.get("directory", LAUNCHER_DIR), "mods")
        pathlib.Path(mods).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", mods])

    def _on_action(self, project):
        pass  # override


# ═══════════════════════════════════════════════════════
#  Mods panel
# ═══════════════════════════════════════════════════════

class ModsBrowsePanel(BrowsePanel):
    def __init__(self, launcher, parent=None):
        super().__init__("mod", "⬇ Download", launcher, parent)
        self._dl_thread = None

    def _on_action(self, project):
        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name, {})
        loader   = profile_data.get("loader", "vanilla")
        version  = profile_data.get("version", "")
        mods_dir = os.path.join(profile_data.get("directory", LAUNCHER_DIR), "mods")

        if loader == "vanilla":
            self.status_label.setText(
                "Switch to a Fabric or Forge profile to download mods.")
            return

        slug  = project.get("slug", project.get("project_id", ""))
        title = project.get("title", slug)
        self.status_label.setText(
            f"Finding {loader} {version} version of {title}…")
        QApplication.processEvents()

        for i in range(self.vbox.count() - 1):
            w = self.vbox.itemAt(i).widget()
            if w and w.project.get("slug") == slug:
                w.set_status("Downloading…")
                break

        try:
            r = requests.get(
                f"{MODRINTH_API}/project/{slug}/version",
                params={"loaders": json.dumps([loader]),
                        "game_versions": json.dumps([version])},
                headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            versions = r.json()
            if not versions:
                self.status_label.setText(
                    f"No {loader} {version} file for {title}.")
                return
            version_id = versions[0]["id"]
        except Exception as e:
            self.status_label.setText(f"Version lookup error: {e}")
            return

        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)
        self._dl_thread = DownloadFileThread(version_id, mods_dir)
        self._dl_thread.status.connect(self.status_label.setText)
        self._dl_thread.progress.connect(
            lambda d, t: self.dl_progress.setValue(
                int(d / t * 100) if t else 0))
        self._dl_thread.finished.connect(self._done)
        self._dl_thread.error.connect(
            lambda e: self.status_label.setText(f"Error: {e}"))
        self._dl_thread.start()

    def _done(self, path):
        self.status_label.setText(f"✅ Downloaded: {os.path.basename(path)}")
        self.dl_progress.setValue(100)
        for i in range(self.vbox.count() - 1):
            w = self.vbox.itemAt(i).widget()
            if w:
                w.reset_button("⬇ Download")


# ═══════════════════════════════════════════════════════
#  Modpacks panel
# ═══════════════════════════════════════════════════════

class ModpackBrowsePanel(BrowsePanel):
    def __init__(self, launcher, parent=None):
        super().__init__("modpack", "⬇ Install", launcher, parent)
        self._dl_thread      = None
        self._install_thread = None

    def _on_action(self, project):
        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name, {})
        profile_dir = profile_data.get("directory", LAUNCHER_DIR)
        slug  = project.get("slug", project.get("project_id", ""))
        title = project.get("title", slug)

        self.status_label.setText(f"Finding latest version of {title}…")
        QApplication.processEvents()

        for i in range(self.vbox.count() - 1):
            w = self.vbox.itemAt(i).widget()
            if w and w.project.get("slug") == slug:
                w.set_status("Installing…")
                break

        try:
            r = requests.get(f"{MODRINTH_API}/project/{slug}/version",
                             headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            versions = r.json()
            if not versions:
                self.status_label.setText(f"No versions found for {title}.")
                return
            version_id = versions[0]["id"]
        except Exception as e:
            self.status_label.setText(f"Version lookup error: {e}")
            return

        tmp_dir = os.path.join(os.getcwd(), "_tmp_mrpack")
        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)

        self._dl_thread = DownloadFileThread(version_id, tmp_dir)
        self._dl_thread.status.connect(self.status_label.setText)
        self._dl_thread.progress.connect(
            lambda d, t: self.dl_progress.setValue(
                int(d / t * 100) if t else 0))
        self._dl_thread.finished.connect(
            lambda path: self._start_install(path, profile_dir))
        self._dl_thread.error.connect(
            lambda e: self.status_label.setText(f"Download error: {e}"))
        self._dl_thread.start()

    def _start_install(self, mrpack_path, profile_dir):
        self.status_label.setText("Installing modpack files…")
        self.dl_progress.setValue(0)
        self._install_thread = InstallModpackThread(mrpack_path, profile_dir)
        self._install_thread.status.connect(self.status_label.setText)
        self._install_thread.progress.connect(
            lambda d, t: self.dl_progress.setValue(
                int(d / t * 100) if t else 0))
        self._install_thread.finished.connect(self._done)
        self._install_thread.error.connect(
            lambda e: self.status_label.setText(f"Install error: {e}"))
        self._install_thread.start()

    def _done(self):
        self.status_label.setText(
            "✅ Modpack installed! All mods are in your profile folder.")
        self.dl_progress.setValue(100)
        import shutil
        shutil.rmtree(os.path.join(os.getcwd(), "_tmp_mrpack"),
                      ignore_errors=True)
        for i in range(self.vbox.count() - 1):
            w = self.vbox.itemAt(i).widget()
            if w:
                w.reset_button("⬇ Install")


# ═══════════════════════════════════════════════════════
#  Profile dialog
# ═══════════════════════════════════════════════════════

class ProfileDialog(QDialog):
    def __init__(self, existing_name="", existing_data=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Modpack Profile")
        self.setMinimumWidth(420)
        if existing_data is None:
            existing_data = {"version": "1.21.1", "loader": "fabric",
                             "directory": ""}

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(22, 22, 22, 22)

        layout.addWidget(QLabel("Profile Name:"))
        self.name_input = QLineEdit(existing_name)
        self.name_input.setPlaceholderText("e.g. Fabric 1.21 Tech Pack")
        layout.addWidget(self.name_input)

        layout.addWidget(QLabel("Minecraft Version:"))
        self.version_combo = QComboBox()
        try:
            for v in minecraft_launcher_lib.utils.get_version_list():
                if v["type"] == "release":
                    self.version_combo.addItem(v["id"])
        except Exception:
            self.version_combo.addItems(
                ["1.21.1","1.20.1","1.19.4","1.18.2","1.16.5"])
        idx = self.version_combo.findText(
            existing_data.get("version", "1.21.1"))
        if idx >= 0:
            self.version_combo.setCurrentIndex(idx)
        layout.addWidget(self.version_combo)

        layout.addWidget(QLabel("Mod Loader:"))
        row = QHBoxLayout()
        self.loader_btns = {}
        for ld in ["vanilla", "fabric", "forge"]:
            b = QPushButton(ld.capitalize())
            b.setCheckable(True)
            b.setObjectName("loaderBtn")
            b.clicked.connect(lambda _, l=ld: self._sel(l))
            self.loader_btns[ld] = b
            row.addWidget(b)
        layout.addLayout(row)
        self._sel(existing_data.get("loader", "fabric"))

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        self._style()

    def _sel(self, loader):
        self._loader = loader
        for n, b in self.loader_btns.items():
            b.setChecked(n == loader)

    def result_data(self):
        name = self.name_input.text().strip() or "Unnamed"
        ver  = self.version_combo.currentText()
        d    = os.path.join(os.getcwd(), "profiles",
                            name.replace(" ", "_").lower())
        return name, {"version": ver, "loader": self._loader, "directory": d}

    def _style(self):
        self.setStyleSheet("""
            QDialog { background:#1a0000; color:white; font-family:Arial; }
            QLabel  { color:#cccccc; font-size:13px; }
            QLineEdit, QComboBox {
                background:rgba(20,20,20,220); border:2px solid #550000;
                border-radius:8px; padding:8px; color:white; font-size:13px; }
            QPushButton { background:#cc0000; color:white; border:none;
                border-radius:8px; padding:8px 16px; font-weight:bold; }
            QPushButton:hover { background:#ff1a1a; }
            QPushButton#loaderBtn {
                background:rgba(60,0,0,200); border:1px solid #550000; }
            QPushButton#loaderBtn:checked {
                background:#cc0000; border:1px solid #ff3333; }
        """)


# ═══════════════════════════════════════════════════════
#  Main launcher window
# ═══════════════════════════════════════════════════════

class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GeoLauncher")
        self.setGeometry(100, 100, 1060, 680)
        self.setMinimumSize(860, 580)

        self.install_thread       = None
        self.selected_loader      = "vanilla"
        self.profiles             = load_profiles()
        self.current_profile_name = list(self.profiles.keys())[0]

        self.bg = QLabel(self)
        self.bg.setGeometry(0, 0, self.width(), self.height())
        self.bg.setScaledContents(True)
        self.bg.setPixmap(QPixmap("assets/background.jpg"))
        self.bg.lower()

        root = QHBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(16)

        # ── Sidebar
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(200)
        sl = QVBoxLayout(self.sidebar)
        sl.setContentsMargins(14, 18, 14, 18)
        sl.setSpacing(10)

        lbl = QLabel("GeoLauncher")
        lbl.setObjectName("sidebarTitle")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(lbl)

        self.btn_play     = QPushButton("▶  Play")
        self.btn_profiles = QPushButton("📦 Profiles")
        self.btn_mods     = QPushButton("🧩 Mods")
        self.btn_modpacks = QPushButton("🌐 Modpacks")
        self.btn_rpacks   = QPushButton("🎨 Resource Packs")
        self.btn_shaders  = QPushButton("🌄 Shaders")
        self.btn_saves    = QPushButton("💾 Saves")
        self.btn_settings = QPushButton("⚙  Settings")

        self.tab_buttons = [
            self.btn_play, self.btn_profiles, self.btn_mods,
            self.btn_modpacks, self.btn_rpacks, self.btn_shaders,
            self.btn_saves, self.btn_settings,
        ]
        for b in self.tab_buttons:
            b.setObjectName("tabButton")
            b.setCheckable(True)
            sl.addWidget(b)
        sl.addStretch()

        self.pages = QStackedWidget()

        self.play_page     = self._make_play_page()
        self.profiles_page = self._make_profiles_page()
        self.mods_panel    = ModsBrowsePanel(self)
        self.packs_panel   = ModpackBrowsePanel(self)
        self.rpacks_page   = self._make_simple_page(
            "Resource Packs", "Put resource pack .zip files here.",
            "Open Resource Packs Folder", "resourcepacks")
        self.shaders_page  = self._make_simple_page(
            "Shaders", "Shader .zip files go here. Requires Iris or OptiFine.",
            "Open Shaders Folder", "shaderpacks")
        self.saves_page    = self._make_simple_page(
            "Saves", "Your Minecraft worlds live here.",
            "Open Saves Folder", "saves")
        self.settings_page = self._make_settings_page()

        for p in [self.play_page, self.profiles_page, self.mods_panel,
                  self.packs_panel, self.rpacks_page, self.shaders_page,
                  self.saves_page, self.settings_page]:
            self.pages.addWidget(p)

        root.addWidget(self.sidebar)
        root.addWidget(self.pages)

        self.btn_play.clicked.connect(lambda: self.switch_page(0))
        self.btn_profiles.clicked.connect(lambda: self.switch_page(1))
        self.btn_mods.clicked.connect(lambda: self.switch_page(2))
        self.btn_modpacks.clicked.connect(lambda: self.switch_page(3))
        self.btn_rpacks.clicked.connect(lambda: self.switch_page(4))
        self.btn_shaders.clicked.connect(lambda: self.switch_page(5))
        self.btn_saves.clicked.connect(lambda: self.switch_page(6))
        self.btn_settings.clicked.connect(lambda: self.switch_page(7))

        self.switch_page(0)
        self._apply_stylesheet()
        self.load_config()

        # Auto-load popular content
        self.mods_panel.refresh_profile_label()
        self.packs_panel.refresh_profile_label()
        self.mods_panel.load_popular()
        self.packs_panel.load_popular()

    # ─────────────── PLAY PAGE ───────────────

    def _make_play_page(self):
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)
        layout.setContentsMargins(30, 25, 30, 25)

        title = QLabel("Play Minecraft")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.play_profile_label = QLabel(f"Profile: {self.current_profile_name}")
        self.play_profile_label.setObjectName("subtitle")
        self.play_profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.play_profile_label)

        self.play_profile_combo = QComboBox()
        self._refresh_profile_combo()
        self.play_profile_combo.currentTextChanged.connect(
            self._on_play_profile_changed)
        layout.addWidget(self.play_profile_combo)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter username")
        layout.addWidget(self.username_input)

        self.version_box = QComboBox()
        self.populate_version_box()
        layout.addWidget(self.version_box)

        lf = QFrame()
        lf.setObjectName("loaderSection")
        lfl = QVBoxLayout(lf)
        lfl.setSpacing(8)
        lfl.setContentsMargins(14, 10, 14, 10)
        lt = QLabel("Mod Loader")
        lt.setObjectName("loaderTitle")
        lfl.addWidget(lt)

        br = QHBoxLayout()
        self.loader_vanilla = QPushButton("Vanilla")
        self.loader_fabric  = QPushButton("Fabric")
        self.loader_forge   = QPushButton("Forge")
        self.loader_buttons_map = {
            "vanilla": self.loader_vanilla,
            "fabric":  self.loader_fabric,
            "forge":   self.loader_forge,
        }
        for name, btn in self.loader_buttons_map.items():
            btn.setObjectName("loaderBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, l=name: self.select_loader(l))
            br.addWidget(btn)
        self.loader_vanilla.setChecked(True)
        lfl.addLayout(br)

        self.loader_info = QLabel("No extra files needed.")
        self.loader_info.setObjectName("loaderInfo")
        lfl.addWidget(self.loader_info)
        layout.addWidget(lf)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.launch_button = QPushButton("Launch Minecraft")
        self.launch_button.clicked.connect(self.launch_minecraft)
        layout.addWidget(self.launch_button)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        return panel

    # ─────────────── PROFILES PAGE ───────────────

    def _make_profiles_page(self):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setSpacing(14)
        layout.setContentsMargins(30, 25, 30, 25)

        t = QLabel("Modpack Profiles")
        t.setObjectName("title")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(t)

        d = QLabel(
            "Each profile has its own mods folder, version and loader.\n"
            "Fabric 1.21 mods will never mix with Forge 1.20 mods.")
        d.setObjectName("subtitle")
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        layout.addWidget(d)

        self.profiles_list = QListWidget()
        self.profiles_list.setObjectName("profilesList")
        layout.addWidget(self.profiles_list)
        self._rebuild_profiles_list()

        br = QHBoxLayout()
        for label, slot in [
            ("➕ New",    self._new_profile),
            ("✏ Edit",   self._edit_profile),
            ("🗑 Delete", self._delete_profile),
            ("📂 Mods",  self._open_profile_mods),
        ]:
            b = QPushButton(label)
            if "Delete" in label:
                b.setObjectName("deleteBtn")
            b.clicked.connect(slot)
            br.addWidget(b)
        layout.addLayout(br)

        use = QPushButton("✔ Use Selected Profile")
        use.clicked.connect(self._select_active_profile)
        layout.addWidget(use)

        self.packs_status = QLabel("")
        self.packs_status.setObjectName("status")
        self.packs_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.packs_status)
        return panel

    # ─────────────── SIMPLE PAGES ───────────────

    def _make_simple_page(self, title, desc, btn_text, folder):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setSpacing(18)
        layout.setContentsMargins(35, 35, 35, 35)
        t = QLabel(title); t.setObjectName("title")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(t)
        d = QLabel(desc); d.setObjectName("subtitle")
        d.setAlignment(Qt.AlignmentFlag.AlignCenter); d.setWordWrap(True)
        layout.addWidget(d)
        b = QPushButton(btn_text)
        b.clicked.connect(lambda: self.open_folder(folder))
        layout.addWidget(b)
        layout.addStretch()
        return panel

    # ─────────────── SETTINGS PAGE ───────────────

    def _make_settings_page(self):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setSpacing(18)
        layout.setContentsMargins(35, 35, 35, 35)
        t = QLabel("Settings"); t.setObjectName("title")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(t)
        i = QLabel("Settings saved automatically in config.json.")
        i.setObjectName("subtitle")
        i.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(i)
        self.ram_label = QLabel("RAM: 4G")
        self.ram_label.setObjectName("subtitle")
        self.ram_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.ram_label)
        self.ram_slider = QSlider(Qt.Orientation.Horizontal)
        self.ram_slider.setMinimum(2)
        self.ram_slider.setMaximum(16)
        self.ram_slider.setValue(4)
        self.ram_slider.setTickInterval(2)
        self.ram_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.ram_slider.valueChanged.connect(self.update_ram_label)
        layout.addWidget(self.ram_slider)
        b = QPushButton("Open Launcher Folder")
        b.clicked.connect(lambda: subprocess.Popen(["xdg-open", os.getcwd()]))
        layout.addWidget(b)
        layout.addStretch()
        return panel

    # ─────────────── NAV ───────────────

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, b in enumerate(self.tab_buttons):
            b.setChecked(i == index)
        if index in (2, 3):
            panel = self.mods_panel if index == 2 else self.packs_panel
            panel.refresh_profile_label()

    # ─────────────── PROFILES ───────────────

    def _rebuild_profiles_list(self):
        self.profiles_list.clear()
        for name, data in self.profiles.items():
            loader  = data.get("loader", "vanilla").capitalize()
            version = data.get("version", "?")
            active  = " ★ ACTIVE" if name == self.current_profile_name else ""
            self.profiles_list.addItem(
                f"{name}  [{loader} {version}]{active}")

    def _refresh_profile_combo(self):
        self.play_profile_combo.blockSignals(True)
        self.play_profile_combo.clear()
        self.play_profile_combo.addItems(list(self.profiles.keys()))
        idx = self.play_profile_combo.findText(self.current_profile_name)
        if idx >= 0:
            self.play_profile_combo.setCurrentIndex(idx)
        self.play_profile_combo.blockSignals(False)

    def _on_play_profile_changed(self, name):
        if name and name in self.profiles:
            self._activate_profile(name)

    def _activate_profile(self, name):
        self.current_profile_name = name
        data = self.profiles[name]
        self.play_profile_label.setText(f"Profile: {name}")
        self.select_loader(data.get("loader", "vanilla"))
        target = data.get("version", "1.21.1")
        for i in range(self.version_box.count()):
            if self.clean_version_name(self.version_box.itemText(i)) == target:
                self.version_box.setCurrentIndex(i)
                break
        self._rebuild_profiles_list()
        self.mods_panel.refresh_profile_label()
        self.packs_panel.refresh_profile_label()

    def _new_profile(self):
        dlg = ProfileDialog(parent=self)
        if dlg.exec():
            name, data = dlg.result_data()
            self.profiles[name] = data
            save_profiles(self.profiles)
            self._rebuild_profiles_list()
            self._refresh_profile_combo()
            self.packs_status.setText(f"Profile '{name}' created.")

    def _edit_profile(self):
        row = self.profiles_list.currentRow()
        if row < 0:
            self.packs_status.setText("Select a profile to edit.")
            return
        name = list(self.profiles.keys())[row]
        dlg = ProfileDialog(existing_name=name,
                            existing_data=self.profiles[name], parent=self)
        if dlg.exec():
            new_name, new_data = dlg.result_data()
            del self.profiles[name]
            self.profiles[new_name] = new_data
            if self.current_profile_name == name:
                self.current_profile_name = new_name
            save_profiles(self.profiles)
            self._rebuild_profiles_list()
            self._refresh_profile_combo()
            self.packs_status.setText("Profile updated.")

    def _delete_profile(self):
        row = self.profiles_list.currentRow()
        if row < 0:
            self.packs_status.setText("Select a profile.")
            return
        if len(self.profiles) == 1:
            self.packs_status.setText("Cannot delete the only profile.")
            return
        name = list(self.profiles.keys())[row]
        if QMessageBox.question(
            self, "Delete", f"Delete '{name}'? Files won't be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            del self.profiles[name]
            if self.current_profile_name == name:
                self.current_profile_name = list(self.profiles.keys())[0]
            save_profiles(self.profiles)
            self._rebuild_profiles_list()
            self._refresh_profile_combo()
            self.packs_status.setText(f"Deleted '{name}'.")

    def _open_profile_mods(self):
        row = self.profiles_list.currentRow()
        if row < 0:
            self.packs_status.setText("Select a profile first.")
            return
        name = list(self.profiles.keys())[row]
        mods = os.path.join(self.profiles[name]["directory"], "mods")
        pathlib.Path(mods).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", mods])

    def _select_active_profile(self):
        row = self.profiles_list.currentRow()
        if row < 0:
            self.packs_status.setText("Select a profile first.")
            return
        name = list(self.profiles.keys())[row]
        self._activate_profile(name)
        idx = self.play_profile_combo.findText(name)
        if idx >= 0:
            self.play_profile_combo.setCurrentIndex(idx)
        self.packs_status.setText(f"Active profile: {name}")

    # ─────────────── LOADER ───────────────

    def select_loader(self, loader):
        self.selected_loader = loader
        for n, b in self.loader_buttons_map.items():
            b.setChecked(n == loader)
        info_map = {
            "vanilla": "No extra files needed.",
            "fabric":  "Fabric API mod recommended in your mods folder.",
            "forge":   "Forge will be downloaded and installed automatically.",
        }
        self.loader_info.setText(info_map.get(loader, ""))
        current = self.clean_version_name(self.version_box.currentText())
        self.version_box.clear()
        self.populate_version_box()
        for i in range(self.version_box.count()):
            if self.clean_version_name(self.version_box.itemText(i)) == current:
                self.version_box.setCurrentIndex(i)
                break

    def clean_version_name(self, v):
        for s in [" ✅ Installed",
                  " ✅ Fabric Installed",
                  " ✅ Forge Installed"]:
            v = v.replace(s, "")
        return v

    def is_version_installed(self, v):
        d = os.path.join(LAUNCHER_DIR, "versions", v)
        return (os.path.exists(os.path.join(d, f"{v}.json")) and
                os.path.exists(os.path.join(d, f"{v}.jar")))

    def is_fabric_installed(self, v):
        d = os.path.join(LAUNCHER_DIR, "versions")
        if not os.path.exists(d): return False
        return any(f.startswith("fabric-loader-") and f.endswith(f"-{v}")
                   for f in os.listdir(d))

    def is_forge_installed(self, v):
        d = os.path.join(LAUNCHER_DIR, "versions")
        if not os.path.exists(d): return False
        return any(f.startswith(f"{v}-forge-") for f in os.listdir(d))

    def populate_version_box(self):
        try:
            for ver in minecraft_launcher_lib.utils.get_version_list():
                if ver["type"] == "release":
                    vid = ver["id"]
                    if self.selected_loader == "vanilla":
                        disp = (f"{vid} ✅ Installed"
                                if self.is_version_installed(vid) else vid)
                    elif self.selected_loader == "fabric":
                        disp = (f"{vid} ✅ Fabric Installed"
                                if self.is_fabric_installed(vid) else vid)
                    else:
                        disp = (f"{vid} ✅ Forge Installed"
                                if self.is_forge_installed(vid) else vid)
                    self.version_box.addItem(disp)
        except Exception:
            self.version_box.addItems(["1.21.1","1.20.1","1.16.5"])

    def refresh_version_list(self):
        current = self.clean_version_name(self.version_box.currentText())
        self.version_box.clear()
        self.populate_version_box()
        for i in range(self.version_box.count()):
            if self.clean_version_name(self.version_box.itemText(i)) == current:
                self.version_box.setCurrentIndex(i)
                break

    # ─────────────── CONFIG ───────────────

    def save_config(self):
        cfg = {
            "username":       self.username_input.text(),
            "version":        self.clean_version_name(
                                  self.version_box.currentText()),
            "ram":            f"{self.ram_slider.value()}G",
            "loader":         self.selected_loader,
            "active_profile": self.current_profile_name,
        }
        with open("config.json", "w") as f:
            json.dump(cfg, f, indent=4)

    def load_config(self):
        if not os.path.exists("config.json"):
            return
        try:
            with open("config.json") as f:
                cfg = json.load(f)
            self.username_input.setText(cfg.get("username", ""))
            self.select_loader(cfg.get("loader", "vanilla"))
            sv = cfg.get("version", "1.20.1")
            for i in range(self.version_box.count()):
                if self.clean_version_name(
                        self.version_box.itemText(i)) == sv:
                    self.version_box.setCurrentIndex(i)
                    break
            self.ram_slider.setValue(
                int(cfg.get("ram", "4G").replace("G", "")))
            self.update_ram_label()
            ap = cfg.get("active_profile", "")
            if ap and ap in self.profiles:
                self._activate_profile(ap)
        except Exception:
            self.status_label.setText("Could not load config.json")

    # ─────────────── FOLDERS / LAUNCH ───────────────

    def open_folder(self, name):
        p = os.path.join(LAUNCHER_DIR, name)
        pathlib.Path(p).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", p])

    def launch_minecraft(self):
        self.save_config()
        username = self.username_input.text().strip() or "Player"
        version  = self.clean_version_name(self.version_box.currentText())
        ram      = f"{self.ram_slider.value()}G"
        pdata    = self.profiles.get(self.current_profile_name, {})
        mc_dir   = pdata.get("directory", LAUNCHER_DIR)

        options = {
            "username":      username,
            "uuid":          "",
            "token":         "",
            "jvmArguments":  [f"-Xmx{ram}"],
            "gameDirectory": mc_dir,
        }
        self.launch_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting…")

        self.install_thread = InstallThread(
            version, self.selected_loader, mc_dir, options)
        self.install_thread.status.connect(self.status_label.setText)
        self.install_thread.progress.connect(self.progress_bar.setValue)
        self.install_thread.max_progress.connect(
            self.progress_bar.setMaximum)
        self.install_thread.finished.connect(self._on_launch_done)
        self.install_thread.error.connect(self._on_launch_error)
        self.install_thread.start()

    def _on_launch_done(self, command):
        subprocess.Popen(command)
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.status_label.setText("Minecraft launched!")
        self.launch_button.setEnabled(True)
        self.refresh_version_list()

    def _on_launch_error(self, msg):
        self.status_label.setText(f"Error: {msg}")
        self.launch_button.setEnabled(True)
        self.progress_bar.setValue(0)

    def update_ram_label(self):
        self.ram_label.setText(f"RAM: {self.ram_slider.value()}G")
        self.save_config()

    def resizeEvent(self, event):
        self.bg.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    # ─────────────── STYLESHEET ───────────────

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QWidget { color:white; font-family:Arial; font-size:14px; }

            QFrame#sidebar {
                background:rgba(0,0,0,200);
                border:2px solid #660000; border-radius:20px;
            }
            QLabel#sidebarTitle {
                font-size:20px; font-weight:bold; color:#ff3333;
            }
            QPushButton#tabButton {
                background:rgba(40,0,0,180); color:white;
                border:1px solid #550000; border-radius:10px;
                padding:10px; text-align:left;
                font-size:13px; font-weight:bold;
            }
            QPushButton#tabButton:hover   { background:#880000; }
            QPushButton#tabButton:checked {
                background:#cc0000; border:1px solid #ff3333; }

            QFrame#panel {
                background:rgba(0,0,0,180);
                border:2px solid #660000; border-radius:20px;
            }
            QFrame#loaderSection {
                background:rgba(30,0,0,160);
                border:1px solid #440000; border-radius:12px;
            }

            QLabel#title    { font-size:30px; font-weight:bold; color:#ff3333; }
            QLabel#subtitle { color:#cccccc; font-size:13px; }
            QLabel#status   { color:#dddddd; }
            QLabel#loaderTitle { font-size:13px; font-weight:bold; color:#ff6666; }
            QLabel#loaderInfo  { font-size:12px; color:#aaaaaa; }

            QWidget#contentCard {
                background:rgba(25,0,0,170);
                border:1px solid #440000; border-radius:10px;
            }
            QWidget#contentCard:hover {
                background:rgba(55,0,0,200); border:1px solid #880000;
            }
            QLabel#modIcon {
                background:rgba(40,0,0,160); border:1px solid #550000;
                border-radius:8px; color:#888; font-size:16px;
            }
            QLabel#modName      { font-size:14px; font-weight:bold; color:#fff; }
            QLabel#modAuthor    { font-size:12px; color:#aaa; }
            QLabel#modDownloads { font-size:12px; color:#ff6666; }
            QLabel#modDesc      { font-size:12px; color:#ccc; }
            QLabel#badge {
                background:rgba(100,0,0,160); color:#ff9999;
                font-size:11px; border-radius:4px; padding:2px 6px;
            }

            QPushButton#dlBtn {
                background:#006600; font-size:12px;
                padding:7px; border-radius:8px;
            }
            QPushButton#dlBtn:hover    { background:#008800; }
            QPushButton#dlBtn:disabled { background:#003300; color:#555; }

            QPushButton#deleteBtn { background:#660000; }
            QPushButton#deleteBtn:hover { background:#990000; }

            QLineEdit, QComboBox {
                background:rgba(20,20,20,220); border:2px solid #550000;
                border-radius:10px; padding:9px; color:white; font-size:14px;
            }
            QLineEdit:focus, QComboBox:focus { border:2px solid #ff3333; }

            QListWidget, QListWidget#profilesList {
                background:rgba(20,20,20,220); border:2px solid #550000;
                border-radius:10px; padding:4px; color:white; font-size:13px;
            }
            QListWidget::item { padding:8px; border-radius:6px; }
            QListWidget::item:selected { background:#cc0000; }

            QScrollArea#modsScroll {
                background:transparent;
                border:2px solid #550000; border-radius:12px;
            }
            QWidget#modsContainer { background:transparent; }

            QSlider::groove:horizontal {
                border:1px solid #550000; height:10px;
                background:rgba(20,20,20,220); border-radius:5px;
            }
            QSlider::handle:horizontal {
                background:#cc0000; border:1px solid #ff3333;
                width:20px; margin:-6px 0; border-radius:10px;
            }
            QSlider::handle:horizontal:hover { background:#ff1a1a; }

            QPushButton {
                background:#cc0000; color:white; border:none;
                border-radius:12px; padding:11px;
                font-size:14px; font-weight:bold;
            }
            QPushButton:hover    { background:#ff1a1a; }
            QPushButton:pressed  { background:#990000; }
            QPushButton:disabled { background:#550000; color:#888; }

            QPushButton#loaderBtn {
                font-size:13px; padding:8px 14px; border-radius:8px;
                background:rgba(60,0,0,200); border:1px solid #550000;
            }
            QPushButton#loaderBtn:hover   { background:#880000; }
            QPushButton#loaderBtn:checked {
                background:#cc0000; border:1px solid #ff3333; }

            QProgressBar {
                border:2px solid #550000; border-radius:10px;
                text-align:center; background:rgba(20,20,20,220);
                color:white; height:22px; font-size:13px;
            }
            QProgressBar::chunk { background:#cc0000; border-radius:8px; }
        """)


# ═══════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════

app = QApplication(sys.argv)
window = Launcher()
window.show()
sys.exit(app.exec())
