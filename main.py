import pathlib
import sys
import os
import json
import subprocess
import zipfile
import requests
import webbrowser
import minecraft_launcher_lib
from minecraft_launcher_lib import microsoft_account

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox,
    QProgressBar, QFrame, QSizePolicy, QStackedWidget, QSlider,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox,
    QScrollArea, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QByteArray
from PyQt6.QtGui import QPixmap, QIcon


# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

LAUNCHER_DIR = os.path.join(os.getcwd(), "minecraft")
PROFILES_FILE = os.path.join(os.getcwd(), "profiles.json")
AUTH_CONFIG_FILE = os.path.join(os.getcwd(), "auth_config.json")
AUTH_FILE = os.path.join(os.getcwd(), "auth.json")
APP_ICON = os.path.join(os.getcwd(), "assets", "icons", "geolauncher.png")
UA = "geocube1770/GeoLauncher/0.6"
MODRINTH_API = "https://api.modrinth.com/v2"


def load_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE) as f:
                return json.load(f)
        except Exception:
            pass

    return {
        "Default": {
            "version": "1.21.1",
            "loader": "vanilla",
            "directory": LAUNCHER_DIR
        }
    }


def save_profiles(profiles):
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=4)


# ═══════════════════════════════════════════════════════
#  Threads
# ═══════════════════════════════════════════════════════

class InstallThread(QThread):
    progress = pyqtSignal(int)
    max_progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, version, loader, mc_dir, options):
        super().__init__()
        self.version = version
        self.loader = loader
        self.mc_dir = mc_dir
        self.options = options

    def run(self):
        try:
            callback = {
                "setStatus": lambda text: self.status.emit(text),
                "setProgress": lambda value: self.progress.emit(value),
                "setMax": lambda value: self.max_progress.emit(value),
            }

            self.status.emit(f"Installing vanilla {self.version}...")
            minecraft_launcher_lib.install.install_minecraft_version(
                self.version,
                self.mc_dir,
                callback
            )

            launch_version = self.version

            if self.loader == "fabric":
                self.status.emit(f"Installing Fabric for {self.version}...")
                fabric_loader = minecraft_launcher_lib.fabric.get_latest_loader_version()

                minecraft_launcher_lib.fabric.install_fabric(
                    self.version,
                    self.mc_dir,
                    loader_version=fabric_loader,
                    callback=callback
                )

                launch_version = f"fabric-loader-{fabric_loader}-{self.version}"

            elif self.loader == "forge":
                self.status.emit(f"Installing Forge for {self.version}...")
                forge_version = minecraft_launcher_lib.forge.find_forge_version(
                    self.version
                )

                if not forge_version:
                    self.error.emit(f"No Forge version found for {self.version}")
                    return

                minecraft_launcher_lib.forge.install_forge_version(
                    forge_version,
                    self.mc_dir,
                    callback=callback
                )

                launch_version = forge_version

            command = minecraft_launcher_lib.command.get_minecraft_command(
                launch_version,
                self.mc_dir,
                self.options
            )

            self.finished.emit(command)

        except Exception as e:
            self.error.emit(str(e))


class ModrinthSearchThread(QThread):
    results = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, query, project_type, version, loader, sort_index):
        super().__init__()
        self.query = query
        self.project_type = project_type
        self.version = version
        self.loader = loader
        self.sort_index = sort_index

    def run(self):
        try:
            sort_map = {
                0: "downloads",
                1: "follows",
                2: "newest",
                3: "updated"
            }

            facets = [[f"project_type:{self.project_type}"]]

            if self.version:
                facets.append([f"versions:{self.version}"])

            if (
                self.loader
                and self.loader != "vanilla"
                and self.project_type == "mod"
            ):
                facets.append([f"categories:{self.loader}"])

            params = {
                "limit": 20,
                "index": sort_map.get(self.sort_index, "downloads"),
                "facets": json.dumps(facets),
            }

            if self.query:
                params["query"] = self.query

            response = requests.get(
                f"{MODRINTH_API}/search",
                params=params,
                headers={"User-Agent": UA},
                timeout=12
            )

            response.raise_for_status()
            self.results.emit(response.json().get("hits", []))

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
            response = requests.get(
                self.url,
                timeout=6,
                headers={"User-Agent": UA}
            )

            if response.status_code == 200:
                self.done.emit(self.row, response.content)

        except Exception:
            pass


class DownloadFileThread(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, version_id, dest_folder):
        super().__init__()
        self.version_id = version_id
        self.dest_folder = dest_folder

    def run(self):
        try:
            self.status.emit("Fetching download info...")

            response = requests.get(
                f"{MODRINTH_API}/version/{self.version_id}",
                headers={"User-Agent": UA},
                timeout=10
            )

            response.raise_for_status()
            files = response.json().get("files", [])

            if not files:
                self.error.emit("No files found.")
                return

            primary = next((f for f in files if f.get("primary")), files[0])
            url = primary["url"]
            filename = primary["filename"]

            self.status.emit(f"Downloading {filename}...")
            pathlib.Path(self.dest_folder).mkdir(parents=True, exist_ok=True)

            dest = os.path.join(self.dest_folder, filename)

            with requests.get(
                url,
                stream=True,
                timeout=120,
                headers={"User-Agent": UA}
            ) as download_response:
                download_response.raise_for_status()

                total = int(download_response.headers.get("content-length", 0))
                downloaded = 0

                with open(dest, "wb") as file:
                    for chunk in download_response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue

                        file.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            self.finished.emit(dest)

        except Exception as e:
            self.error.emit(str(e))


class InstallModpackThread(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, mrpack_path, profile_dir):
        super().__init__()
        self.mrpack_path = mrpack_path
        self.profile_dir = profile_dir

    def run(self):
        try:
            import shutil

            self.status.emit("Unpacking modpack...")
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
                url = entry["downloads"][0]
                rel_path = entry["path"]
                dest = os.path.join(self.profile_dir, rel_path)

                pathlib.Path(os.path.dirname(dest)).mkdir(
                    parents=True,
                    exist_ok=True
                )

                self.status.emit(f"({i + 1}/{total}) {os.path.basename(rel_path)}")
                self.progress.emit(i + 1, total)

                with requests.get(
                    url,
                    stream=True,
                    timeout=60,
                    headers={"User-Agent": UA}
                ) as response:
                    response.raise_for_status()

                    with open(dest, "wb") as file:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                file.write(chunk)

            overrides = os.path.join(tmp, "overrides")

            if os.path.isdir(overrides):
                self.status.emit("Copying overrides...")

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
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(14)

        self.icon_label = QLabel("...")
        self.icon_label.setFixedSize(56, 56)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setObjectName("modIcon")
        layout.addWidget(self.icon_label)

        info = QVBoxLayout()
        info.setSpacing(4)

        top = QHBoxLayout()

        name = QLabel(f"<b>{project.get('title', 'Unknown')}</b>")
        name.setObjectName("modName")

        author = QLabel(f"by {project.get('author', '?')}")
        author.setObjectName("modAuthor")

        downloads = QLabel(f"⬇ {project.get('downloads', 0):,}")
        downloads.setObjectName("modDownloads")

        top.addWidget(name)
        top.addWidget(author)
        top.addStretch()
        top.addWidget(downloads)

        badges = QHBoxLayout()
        badges.setSpacing(5)

        for category in project.get("categories", [])[:4]:
            badge = QLabel(category)
            badge.setObjectName("badge")
            badges.addWidget(badge)

        badges.addStretch()

        desc = QLabel(project.get("description", ""))
        desc.setObjectName("modDesc")
        desc.setWordWrap(True)

        info.addLayout(top)
        info.addLayout(badges)
        info.addWidget(desc)

        layout.addLayout(info, 1)

        self.dl_btn = QPushButton(action_label)
        self.dl_btn.setObjectName("dlBtn")
        self.dl_btn.setFixedWidth(112)
        self.dl_btn.clicked.connect(
            lambda: self.action_requested.emit(self.project)
        )

        layout.addWidget(self.dl_btn)

    def set_icon(self, pixmap):
        self.icon_label.setText("")
        self.icon_label.setPixmap(
            pixmap.scaled(
                56,
                56,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        )

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
        self.project_type = project_type
        self.action_label = action_label
        self.launcher = launcher
        self.search_thread = None
        self.icon_threads = []

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

        placeholder = (
            "Search mods... (empty = popular)"
            if project_type == "mod"
            else "Search modpacks... (empty = popular)"
        )

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(placeholder)
        self.search_input.returnPressed.connect(self.do_search)
        controls.addWidget(self.search_input, 3)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "Most Downloaded",
            "Most Followed",
            "Newest",
            "Recently Updated"
        ])
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
        self.vbox.setSpacing(7)
        self.vbox.setContentsMargins(6, 6, 6, 6)
        self.vbox.addStretch()

        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll)

        self.status_label = QLabel("Loading popular content...")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status_label)

        self.dl_progress = QProgressBar()
        self.dl_progress.setValue(0)
        self.dl_progress.setVisible(False)
        root.addWidget(self.dl_progress)

        if project_type == "mod":
            installed_title = QLabel("Installed Mods")
            installed_title.setObjectName("loaderTitle")
            installed_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(installed_title)

            self.installed_mods_list = QListWidget()
            self.installed_mods_list.setObjectName("profilesList")
            root.addWidget(self.installed_mods_list)

            installed_buttons = QHBoxLayout()

            refresh_btn = QPushButton("Refresh")
            refresh_btn.clicked.connect(self.refresh_installed_mods)
            installed_buttons.addWidget(refresh_btn)

            toggle_btn = QPushButton("Disable / Enable")
            toggle_btn.clicked.connect(self.toggle_selected_mod)
            installed_buttons.addWidget(toggle_btn)

            delete_btn = QPushButton("Delete")
            delete_btn.setObjectName("deleteBtn")
            delete_btn.clicked.connect(self.delete_selected_mod)
            installed_buttons.addWidget(delete_btn)

            root.addLayout(installed_buttons)

            btn = QPushButton("📂 Open Active Profile's Mods Folder")
            btn.clicked.connect(self._open_mods_folder)
            root.addWidget(btn)

            self.refresh_installed_mods()

    def refresh_profile_label(self):
        data = self.launcher.profiles.get(self.launcher.current_profile_name, {})
        loader = data.get("loader", "?").capitalize()
        version = data.get("version", "?")

        if self.project_type == "mod":
            self.profile_label.setText(
                f"Active: {self.launcher.current_profile_name}  [{loader} {version}]"
            )
            self.refresh_installed_mods()
        else:
            self.profile_label.setText(
                f"Installing to: {self.launcher.current_profile_name}  [{loader} {version}]"
            )

    def load_popular(self):
        self.do_search()

    def do_search(self):
        if self.search_thread and self.search_thread.isRunning():
            self.search_thread.terminate()

        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name,
            {}
        )

        version = profile_data.get("version", "")
        loader = profile_data.get("loader", "vanilla")

        if self.project_type == "modpack":
            loader = ""

        query = self.search_input.text().strip()

        self.status_label.setText(
            "Loading popular..."
            if not query
            else f"Searching '{query}'..."
        )

        self._clear_cards()

        self.search_thread = ModrinthSearchThread(
            query,
            self.project_type,
            version,
            loader,
            self.sort_combo.currentIndex()
        )

        self.search_thread.results.connect(self._on_results)
        self.search_thread.error.connect(
            lambda e: self.status_label.setText(f"Error: {e}")
        )

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
                thread = FetchIconThread(idx, icon_url)
                thread.done.connect(self._on_icon)
                thread.start()
                self.icon_threads.append(thread)

        query = self.search_input.text().strip()
        sort_text = self.sort_combo.currentText()

        if query:
            self.status_label.setText(
                f"{len(hits)} result(s) for '{query}' — {sort_text}"
            )
        else:
            self.status_label.setText(
                f"Top {len(hits)} popular — {sort_text}"
            )

    def _on_icon(self, row, data):
        item = self.vbox.itemAt(row)

        if item and item.widget():
            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(data))

            if not pixmap.isNull():
                item.widget().set_icon(pixmap)

    def _clear_cards(self):
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)

            if item.widget():
                item.widget().deleteLater()

    def get_active_mods_folder(self):
        data = self.launcher.profiles.get(
            self.launcher.current_profile_name,
            {}
        )

        mods_folder = os.path.join(
            data.get("directory", LAUNCHER_DIR),
            "mods"
        )

        pathlib.Path(mods_folder).mkdir(
            parents=True,
            exist_ok=True
        )

        return mods_folder

    def refresh_installed_mods(self):
        if self.project_type != "mod":
            return

        if not hasattr(self, "installed_mods_list"):
            return

        mods_folder = self.get_active_mods_folder()
        self.installed_mods_list.clear()

        files = []

        for filename in os.listdir(mods_folder):
            if filename.endswith(".jar") or filename.endswith(".jar.disabled"):
                files.append(filename)

        files.sort()

        if not files:
            item = QListWidgetItem("No mods installed.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.installed_mods_list.addItem(item)
            return

        for filename in files:
            if filename.endswith(".disabled"):
                display_name = f"DISABLED — {filename.replace('.disabled', '')}"
            else:
                display_name = f"ENABLED — {filename}"

            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, filename)
            self.installed_mods_list.addItem(item)

    def get_selected_mod_filename(self):
        if not hasattr(self, "installed_mods_list"):
            return None

        item = self.installed_mods_list.currentItem()

        if item is None:
            return None

        return item.data(Qt.ItemDataRole.UserRole)

    def toggle_selected_mod(self):
        filename = self.get_selected_mod_filename()

        if not filename:
            self.status_label.setText("Select a mod first.")
            return

        mods_folder = self.get_active_mods_folder()
        old_path = os.path.join(mods_folder, filename)

        if filename.endswith(".jar.disabled"):
            new_filename = filename.replace(".jar.disabled", ".jar")
        elif filename.endswith(".jar"):
            new_filename = filename + ".disabled"
        else:
            self.status_label.setText("This file is not a valid mod jar.")
            return

        new_path = os.path.join(mods_folder, new_filename)

        try:
            os.rename(old_path, new_path)
            self.refresh_installed_mods()
            self.status_label.setText(f"Updated: {new_filename}")
        except Exception as e:
            self.status_label.setText(f"Could not update mod: {e}")

    def delete_selected_mod(self):
        filename = self.get_selected_mod_filename()

        if not filename:
            self.status_label.setText("Select a mod first.")
            return

        answer = QMessageBox.question(
            self,
            "Delete Mod",
            f"Delete this mod?\n\n{filename}",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        mods_folder = self.get_active_mods_folder()
        file_path = os.path.join(mods_folder, filename)

        try:
            os.remove(file_path)
            self.refresh_installed_mods()
            self.status_label.setText(f"Deleted: {filename}")
        except Exception as e:
            self.status_label.setText(f"Could not delete mod: {e}")

    def _open_mods_folder(self):
        mods = self.get_active_mods_folder()
        subprocess.Popen(["xdg-open", mods])

    def _on_action(self, project):
        pass


# ═══════════════════════════════════════════════════════
#  Mods panel
# ═══════════════════════════════════════════════════════

class ModsBrowsePanel(BrowsePanel):
    def __init__(self, launcher, parent=None):
        super().__init__("mod", "⬇ Download", launcher, parent)
        self._dl_thread = None

    def _on_action(self, project):
        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name,
            {}
        )

        loader = profile_data.get("loader", "vanilla")
        version = profile_data.get("version", "")
        mods_dir = os.path.join(
            profile_data.get("directory", LAUNCHER_DIR),
            "mods"
        )

        if loader == "vanilla":
            self.status_label.setText(
                "Switch to a Fabric or Forge profile to download mods."
            )
            return

        slug = project.get("slug", project.get("project_id", ""))
        title = project.get("title", slug)

        self.status_label.setText(
            f"Finding {loader} {version} version of {title}..."
        )

        QApplication.processEvents()

        for i in range(self.vbox.count() - 1):
            widget = self.vbox.itemAt(i).widget()

            if widget and widget.project.get("slug") == slug:
                widget.set_status("Downloading...")
                break

        try:
            response = requests.get(
                f"{MODRINTH_API}/project/{slug}/version",
                params={
                    "loaders": json.dumps([loader]),
                    "game_versions": json.dumps([version])
                },
                headers={"User-Agent": UA},
                timeout=10
            )

            response.raise_for_status()
            versions = response.json()

            if not versions:
                self.status_label.setText(
                    f"No {loader} {version} file for {title}."
                )
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
            lambda downloaded, total: self.dl_progress.setValue(
                int(downloaded / total * 100) if total else 0
            )
        )
        self._dl_thread.finished.connect(self._done)
        self._dl_thread.error.connect(
            lambda e: self.status_label.setText(f"Error: {e}")
        )

        self._dl_thread.start()

    def _done(self, path):
        self.status_label.setText(f"✅ Downloaded: {os.path.basename(path)}")
        self.dl_progress.setValue(100)
        self.refresh_installed_mods()

        for i in range(self.vbox.count() - 1):
            widget = self.vbox.itemAt(i).widget()

            if widget:
                widget.reset_button("⬇ Download")


# ═══════════════════════════════════════════════════════
#  Modpacks panel
# ═══════════════════════════════════════════════════════

class ModpackBrowsePanel(BrowsePanel):
    def __init__(self, launcher, parent=None):
        super().__init__("modpack", "⬇ Install", launcher, parent)
        self._dl_thread = None
        self._install_thread = None

    def _on_action(self, project):
        profile_data = self.launcher.profiles.get(
            self.launcher.current_profile_name,
            {}
        )

        profile_dir = profile_data.get("directory", LAUNCHER_DIR)
        slug = project.get("slug", project.get("project_id", ""))
        title = project.get("title", slug)

        self.status_label.setText(f"Finding latest version of {title}...")
        QApplication.processEvents()

        for i in range(self.vbox.count() - 1):
            widget = self.vbox.itemAt(i).widget()

            if widget and widget.project.get("slug") == slug:
                widget.set_status("Installing...")
                break

        try:
            response = requests.get(
                f"{MODRINTH_API}/project/{slug}/version",
                headers={"User-Agent": UA},
                timeout=10
            )

            response.raise_for_status()
            versions = response.json()

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
            lambda downloaded, total: self.dl_progress.setValue(
                int(downloaded / total * 100) if total else 0
            )
        )
        self._dl_thread.finished.connect(
            lambda path: self._start_install(path, profile_dir)
        )
        self._dl_thread.error.connect(
            lambda e: self.status_label.setText(f"Download error: {e}")
        )

        self._dl_thread.start()

    def _start_install(self, mrpack_path, profile_dir):
        self.status_label.setText("Installing modpack files...")
        self.dl_progress.setValue(0)

        self._install_thread = InstallModpackThread(mrpack_path, profile_dir)

        self._install_thread.status.connect(self.status_label.setText)
        self._install_thread.progress.connect(
            lambda done, total: self.dl_progress.setValue(
                int(done / total * 100) if total else 0
            )
        )
        self._install_thread.finished.connect(self._done)
        self._install_thread.error.connect(
            lambda e: self.status_label.setText(f"Install error: {e}")
        )

        self._install_thread.start()

    def _done(self):
        self.status_label.setText(
            "✅ Modpack installed! All mods are in your profile folder."
        )

        self.dl_progress.setValue(100)

        import shutil
        shutil.rmtree(
            os.path.join(os.getcwd(), "_tmp_mrpack"),
            ignore_errors=True
        )

        for i in range(self.vbox.count() - 1):
            widget = self.vbox.itemAt(i).widget()

            if widget:
                widget.reset_button("⬇ Install")


# ═══════════════════════════════════════════════════════
#  Profile dialog
# ═══════════════════════════════════════════════════════

class ProfileDialog(QDialog):
    def __init__(self, existing_name="", existing_data=None, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Modpack Profile")
        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))
        self.setMinimumWidth(420)

        if existing_data is None:
            existing_data = {
                "version": "1.21.1",
                "loader": "fabric",
                "directory": ""
            }

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
            for version in minecraft_launcher_lib.utils.get_version_list():
                if version["type"] == "release":
                    self.version_combo.addItem(version["id"])
        except Exception:
            self.version_combo.addItems([
                "1.21.1",
                "1.20.1",
                "1.19.4",
                "1.18.2",
                "1.16.5"
            ])

        index = self.version_combo.findText(
            existing_data.get("version", "1.21.1")
        )

        if index >= 0:
            self.version_combo.setCurrentIndex(index)

        layout.addWidget(self.version_combo)

        layout.addWidget(QLabel("Mod Loader:"))

        row = QHBoxLayout()
        self.loader_btns = {}

        for loader in ["vanilla", "fabric", "forge"]:
            button = QPushButton(loader.capitalize())
            button.setCheckable(True)
            button.setObjectName("loaderBtn")
            button.clicked.connect(lambda _, l=loader: self._select_loader(l))
            self.loader_btns[loader] = button
            row.addWidget(button)

        layout.addLayout(row)
        self._select_loader(existing_data.get("loader", "fabric"))

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )

        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._style()

    def _select_loader(self, loader):
        self._loader = loader

        for name, button in self.loader_btns.items():
            button.setChecked(name == loader)

    def result_data(self):
        name = self.name_input.text().strip() or "Unnamed"
        version = self.version_combo.currentText()

        directory = os.path.join(
            os.getcwd(),
            "profiles",
            name.replace(" ", "_").lower()
        )

        return name, {
            "version": version,
            "loader": self._loader,
            "directory": directory
        }

    def _style(self):
        self.setStyleSheet("""
            QDialog {
                background: #120607;
                color: white;
                font-family: Arial;
            }

            QLabel {
                color: #dddddd;
                font-size: 13px;
            }

            QLineEdit, QComboBox {
                background: rgba(255,255,255,24);
                border: 1px solid rgba(255,80,80,140);
                border-radius: 10px;
                padding: 8px;
                color: white;
                font-size: 13px;
            }

            QPushButton {
                background: rgba(210, 0, 0, 210);
                color: white;
                border: 1px solid rgba(255, 80, 80, 130);
                border-radius: 10px;
                padding: 8px 16px;
                font-weight: bold;
            }

            QPushButton:hover {
                background: rgba(255, 30, 30, 230);
            }

            QPushButton#loaderBtn {
                background: rgba(255,255,255,20);
                border: 1px solid rgba(255,80,80,90);
            }

            QPushButton#loaderBtn:checked {
                background: rgba(210, 0, 0, 220);
                border: 1px solid rgba(255,100,100,200);
            }
        """)


# ═══════════════════════════════════════════════════════
#  Main launcher window
# ═══════════════════════════════════════════════════════

class Launcher(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("GeoLauncher")
        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))

        self.setGeometry(100, 100, 1080, 700)
        self.setMinimumSize(860, 580)

        self.install_thread = None
        self.selected_loader = "vanilla"
        self.profiles = load_profiles()
        self.current_profile_name = list(self.profiles.keys())[0]

        self.auth_config = self.load_auth_config()
        self.auth_data = self.load_auth_data()
        self.ms_login_url = None
        self.ms_state = None
        self.ms_code_verifier = None

        self.bg = QLabel(self)
        self.bg.setGeometry(0, 0, self.width(), self.height())
        self.bg.setScaledContents(True)
        self.bg.setPixmap(QPixmap("assets/background.jpg"))
        self.bg.lower()

        root = QHBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(16)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(200)

        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(14, 18, 14, 18)
        sidebar_layout.setSpacing(10)

        label = QLabel("GeoLauncher")
        label.setObjectName("sidebarTitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addWidget(label)

        self.btn_play = QPushButton("▶  Play")
        self.btn_profiles = QPushButton("📦 Profiles")
        self.btn_mods = QPushButton("🧩 Mods")
        self.btn_modpacks = QPushButton("🌐 Modpacks")
        self.btn_rpacks = QPushButton("🎨 Resource Packs")
        self.btn_shaders = QPushButton("🌄 Shaders")
        self.btn_saves = QPushButton("💾 Saves")
        self.btn_settings = QPushButton("⚙  Settings")

        self.tab_buttons = [
            self.btn_play,
            self.btn_profiles,
            self.btn_mods,
            self.btn_modpacks,
            self.btn_rpacks,
            self.btn_shaders,
            self.btn_saves,
            self.btn_settings,
        ]

        for button in self.tab_buttons:
            button.setObjectName("tabButton")
            button.setCheckable(True)
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        self.pages = QStackedWidget()

        self.play_page = self._make_play_page()
        self.profiles_page = self._make_profiles_page()
        self.mods_panel = ModsBrowsePanel(self)
        self.packs_panel = ModpackBrowsePanel(self)
        self.rpacks_page = self._make_simple_page(
            "Resource Packs",
            "Put resource pack .zip files here.",
            "Open Resource Packs Folder",
            "resourcepacks"
        )
        self.shaders_page = self._make_simple_page(
            "Shaders",
            "Shader .zip files go here. Requires Iris or OptiFine.",
            "Open Shaders Folder",
            "shaderpacks"
        )
        self.saves_page = self._make_simple_page(
            "Saves",
            "Your Minecraft worlds live here.",
            "Open Saves Folder",
            "saves"
        )
        self.settings_page = self._make_settings_page()

        for page in [
            self.play_page,
            self.profiles_page,
            self.mods_panel,
            self.packs_panel,
            self.rpacks_page,
            self.shaders_page,
            self.saves_page,
            self.settings_page
        ]:
            self.pages.addWidget(page)

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
        self.update_auth_status()

        self.mods_panel.refresh_profile_label()
        self.packs_panel.refresh_profile_label()
        self.mods_panel.load_popular()
        self.packs_panel.load_popular()

    # ─────────────── PLAY PAGE ───────────────

    def _make_play_page(self):
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

        layout = QVBoxLayout(panel)
        layout.setSpacing(12)
        layout.setContentsMargins(34, 28, 34, 28)

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
            self._on_play_profile_changed
        )
        layout.addWidget(self.play_profile_combo)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter offline username")
        layout.addWidget(self.username_input)

        self.version_box = QComboBox()
        self.populate_version_box()
        layout.addWidget(self.version_box)

        loader_frame = QFrame()
        loader_frame.setObjectName("loaderSection")

        loader_layout = QVBoxLayout(loader_frame)
        loader_layout.setSpacing(8)
        loader_layout.setContentsMargins(14, 10, 14, 10)

        loader_title = QLabel("Mod Loader")
        loader_title.setObjectName("loaderTitle")
        loader_layout.addWidget(loader_title)

        button_row = QHBoxLayout()

        self.loader_vanilla = QPushButton("Vanilla")
        self.loader_fabric = QPushButton("Fabric")
        self.loader_forge = QPushButton("Forge")

        self.loader_buttons_map = {
            "vanilla": self.loader_vanilla,
            "fabric": self.loader_fabric,
            "forge": self.loader_forge,
        }

        for name, button in self.loader_buttons_map.items():
            button.setObjectName("loaderBtn")
            button.setCheckable(True)
            button.clicked.connect(lambda _, l=name: self.select_loader(l))
            button_row.addWidget(button)

        self.loader_vanilla.setChecked(True)
        loader_layout.addLayout(button_row)

        self.loader_info = QLabel("No extra files needed.")
        self.loader_info.setObjectName("loaderInfo")
        loader_layout.addWidget(self.loader_info)

        layout.addWidget(loader_frame)

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
        layout.setContentsMargins(34, 28, 34, 28)

        title = QLabel("Modpack Profiles")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        desc = QLabel(
            "Each profile has its own mods folder, version and loader.\n"
            "Fabric 1.21 mods will never mix with Forge 1.20 mods."
        )
        desc.setObjectName("subtitle")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.profiles_list = QListWidget()
        self.profiles_list.setObjectName("profilesList")
        layout.addWidget(self.profiles_list)

        self._rebuild_profiles_list()

        button_row = QHBoxLayout()

        profile_buttons = [
            ("➕ New", self._new_profile),
            ("✏ Edit", self._edit_profile),
            ("🗑 Delete", self._delete_profile),
            ("📂 Mods", self._open_profile_mods),
        ]

        for label, slot in profile_buttons:
            button = QPushButton(label)

            if "Delete" in label:
                button.setObjectName("deleteBtn")

            button.clicked.connect(slot)
            button_row.addWidget(button)

        layout.addLayout(button_row)

        use = QPushButton("✔ Use Selected Profile")
        use.clicked.connect(self._select_active_profile)
        layout.addWidget(use)

        self.packs_status = QLabel("")
        self.packs_status.setObjectName("status")
        self.packs_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.packs_status)

        return panel

    # ─────────────── SIMPLE PAGES ───────────────

    def _make_simple_page(self, title_text, desc_text, button_text, folder):
        panel = QFrame()
        panel.setObjectName("panel")

        layout = QVBoxLayout(panel)
        layout.setSpacing(18)
        layout.setContentsMargins(38, 38, 38, 38)

        title = QLabel(title_text)
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        desc = QLabel(desc_text)
        desc.setObjectName("subtitle")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        button = QPushButton(button_text)
        button.clicked.connect(lambda: self.open_folder(folder))
        layout.addWidget(button)

        layout.addStretch()

        return panel

    # ─────────────── SETTINGS PAGE ───────────────

    def _make_settings_page(self):
        panel = QFrame()
        panel.setObjectName("panel")

        layout = QVBoxLayout(panel)
        layout.setSpacing(14)
        layout.setContentsMargins(38, 32, 38, 32)

        title = QLabel("Settings")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        info = QLabel("Settings saved automatically in config.json.")
        info.setObjectName("subtitle")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)

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

        open_launcher_folder = QPushButton("Open Launcher Folder")
        open_launcher_folder.clicked.connect(
            lambda: subprocess.Popen(["xdg-open", os.getcwd()])
        )
        layout.addWidget(open_launcher_folder)

        auth_section = QFrame()
        auth_section.setObjectName("loaderSection")

        auth_layout = QVBoxLayout(auth_section)
        auth_layout.setSpacing(10)
        auth_layout.setContentsMargins(14, 12, 14, 12)

        auth_title = QLabel("Microsoft Account")
        auth_title.setObjectName("loaderTitle")
        auth_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth_layout.addWidget(auth_title)

        self.auth_status_label = QLabel("Not logged in.")
        self.auth_status_label.setObjectName("subtitle")
        self.auth_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.auth_status_label.setWordWrap(True)
        auth_layout.addWidget(self.auth_status_label)

        login_button = QPushButton("Login with Microsoft")
        login_button.clicked.connect(self.start_microsoft_login)
        auth_layout.addWidget(login_button)

        self.redirect_input = QLineEdit()
        self.redirect_input.setPlaceholderText(
            "Paste redirected URL here after login"
        )
        auth_layout.addWidget(self.redirect_input)

        complete_button = QPushButton("Complete Login")
        complete_button.clicked.connect(self.complete_microsoft_login)
        auth_layout.addWidget(complete_button)

        logout_button = QPushButton("Logout Microsoft Account")
        logout_button.clicked.connect(self.logout_microsoft_account)
        logout_button.setObjectName("deleteBtn")
        auth_layout.addWidget(logout_button)

        layout.addWidget(auth_section)
        layout.addStretch()

        return panel

    # ─────────────── MICROSOFT AUTH ───────────────

    def load_auth_config(self):
        if not os.path.exists(AUTH_CONFIG_FILE):
            return {}

        try:
            with open(AUTH_CONFIG_FILE, "r") as file:
                return json.load(file)
        except Exception:
            return {}

    def load_auth_data(self):
        if not os.path.exists(AUTH_FILE):
            return None

        try:
            with open(AUTH_FILE, "r") as file:
                return json.load(file)
        except Exception:
            return None

    def save_auth_data(self):
        if self.auth_data is None:
            return

        with open(AUTH_FILE, "w") as file:
            json.dump(self.auth_data, file, indent=4)

    def update_auth_status(self):
        if not hasattr(self, "auth_status_label"):
            return

        if self.auth_data:
            name = self.auth_data.get("name", "Unknown")
            self.auth_status_label.setText(f"Logged in as: {name}")
            self.username_input.setPlaceholderText("Using Microsoft account")
        else:
            self.auth_status_label.setText(
                "Not logged in. Offline username mode is active."
            )
            self.username_input.setPlaceholderText("Enter offline username")

    def start_microsoft_login(self):
        client_id = self.auth_config.get("client_id", "")
        redirect_uri = self.auth_config.get("redirect_uri", "http://localhost")

        if client_id == "":
            self.auth_status_label.setText(
                "Missing client_id in auth_config.json"
            )
            return

        try:
            login_url, state, code_verifier = (
                microsoft_account.get_secure_login_data(
                    client_id,
                    redirect_uri
                )
            )

            self.ms_login_url = login_url
            self.ms_state = state
            self.ms_code_verifier = code_verifier

            webbrowser.open(login_url)

            self.auth_status_label.setText(
                "Browser opened. Log in, then copy the full redirected URL and paste it below."
            )

        except Exception as e:
            self.auth_status_label.setText(f"Login start error: {e}")

    def complete_microsoft_login(self):
        client_id = self.auth_config.get("client_id", "")
        redirect_uri = self.auth_config.get("redirect_uri", "http://localhost")
        redirected_url = self.redirect_input.text().strip()

        if client_id == "":
            self.auth_status_label.setText(
                "Missing client_id in auth_config.json"
            )
            return

        if redirected_url == "":
            self.auth_status_label.setText("Paste the redirected URL first.")
            return

        if not self.ms_state or not self.ms_code_verifier:
            self.auth_status_label.setText(
                "Click 'Login with Microsoft' first."
            )
            return

        try:
            auth_code = microsoft_account.parse_auth_code_url(
                redirected_url,
                self.ms_state
            )

            self.auth_status_label.setText("Completing Microsoft login...")
            QApplication.processEvents()

            self.auth_data = microsoft_account.complete_login(
                client_id,
                None,
                redirect_uri,
                auth_code,
                self.ms_code_verifier
            )

            self.save_auth_data()
            self.update_auth_status()

        except Exception as e:
            self.auth_status_label.setText(f"Login error: {e}")

    def refresh_microsoft_login(self):
        if not self.auth_data:
            return

        client_id = self.auth_config.get("client_id", "")
        redirect_uri = self.auth_config.get("redirect_uri", "http://localhost")
        refresh_token = self.auth_data.get("refresh_token", "")

        if client_id == "" or refresh_token == "":
            return

        try:
            self.auth_data = microsoft_account.complete_refresh(
                client_id,
                None,
                redirect_uri,
                refresh_token
            )

            self.save_auth_data()
            self.update_auth_status()

        except Exception:
            self.auth_data = None
            self.update_auth_status()

    def logout_microsoft_account(self):
        self.auth_data = None

        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)

        self.update_auth_status()

    # ─────────────── NAV ───────────────

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)

        for i, button in enumerate(self.tab_buttons):
            button.setChecked(i == index)

        if index in (2, 3):
            panel = self.mods_panel if index == 2 else self.packs_panel
            panel.refresh_profile_label()

            if index == 2:
                self.mods_panel.refresh_installed_mods()

    # ─────────────── PROFILES ───────────────

    def _rebuild_profiles_list(self):
        self.profiles_list.clear()

        for name, data in self.profiles.items():
            loader = data.get("loader", "vanilla").capitalize()
            version = data.get("version", "?")
            active = " ★ ACTIVE" if name == self.current_profile_name else ""

            self.profiles_list.addItem(
                f"{name}  [{loader} {version}]{active}"
            )

    def _refresh_profile_combo(self):
        self.play_profile_combo.blockSignals(True)
        self.play_profile_combo.clear()
        self.play_profile_combo.addItems(list(self.profiles.keys()))

        index = self.play_profile_combo.findText(self.current_profile_name)

        if index >= 0:
            self.play_profile_combo.setCurrentIndex(index)

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
        dialog = ProfileDialog(parent=self)

        if dialog.exec():
            name, data = dialog.result_data()

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

        dialog = ProfileDialog(
            existing_name=name,
            existing_data=self.profiles[name],
            parent=self
        )

        if dialog.exec():
            new_name, new_data = dialog.result_data()

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

        answer = QMessageBox.question(
            self,
            "Delete",
            f"Delete '{name}'? Files won't be removed.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )

        if answer == QMessageBox.StandardButton.Yes:
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

        index = self.play_profile_combo.findText(name)

        if index >= 0:
            self.play_profile_combo.setCurrentIndex(index)

        self.packs_status.setText(f"Active profile: {name}")

    # ─────────────── LOADER ───────────────

    def select_loader(self, loader):
        self.selected_loader = loader

        for name, button in self.loader_buttons_map.items():
            button.setChecked(name == loader)

        info_map = {
            "vanilla": "No extra files needed.",
            "fabric": "Fabric API mod recommended in your mods folder.",
            "forge": "Forge will be downloaded and installed automatically.",
        }

        self.loader_info.setText(info_map.get(loader, ""))

        current = self.clean_version_name(self.version_box.currentText())

        self.version_box.clear()
        self.populate_version_box()

        for i in range(self.version_box.count()):
            if self.clean_version_name(self.version_box.itemText(i)) == current:
                self.version_box.setCurrentIndex(i)
                break

    def clean_version_name(self, version):
        for suffix in [
            " ✅ Installed",
            " ✅ Fabric Installed",
            " ✅ Forge Installed"
        ]:
            version = version.replace(suffix, "")

        return version

    def is_version_installed(self, version):
        directory = os.path.join(LAUNCHER_DIR, "versions", version)

        return (
            os.path.exists(os.path.join(directory, f"{version}.json"))
            and os.path.exists(os.path.join(directory, f"{version}.jar"))
        )

    def is_fabric_installed(self, version):
        directory = os.path.join(LAUNCHER_DIR, "versions")

        if not os.path.exists(directory):
            return False

        return any(
            folder.startswith("fabric-loader-")
            and folder.endswith(f"-{version}")
            for folder in os.listdir(directory)
        )

    def is_forge_installed(self, version):
        directory = os.path.join(LAUNCHER_DIR, "versions")

        if not os.path.exists(directory):
            return False

        return any(
            folder.startswith(f"{version}-forge-")
            for folder in os.listdir(directory)
        )

    def populate_version_box(self):
        try:
            for version in minecraft_launcher_lib.utils.get_version_list():
                if version["type"] != "release":
                    continue

                version_id = version["id"]

                if self.selected_loader == "vanilla":
                    display = (
                        f"{version_id} ✅ Installed"
                        if self.is_version_installed(version_id)
                        else version_id
                    )
                elif self.selected_loader == "fabric":
                    display = (
                        f"{version_id} ✅ Fabric Installed"
                        if self.is_fabric_installed(version_id)
                        else version_id
                    )
                else:
                    display = (
                        f"{version_id} ✅ Forge Installed"
                        if self.is_forge_installed(version_id)
                        else version_id
                    )

                self.version_box.addItem(display)

        except Exception:
            self.version_box.addItems([
                "1.21.1",
                "1.20.1",
                "1.16.5"
            ])

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
        config = {
            "username": self.username_input.text(),
            "version": self.clean_version_name(self.version_box.currentText()),
            "ram": f"{self.ram_slider.value()}G",
            "loader": self.selected_loader,
            "active_profile": self.current_profile_name,
        }

        with open("config.json", "w") as file:
            json.dump(config, file, indent=4)

    def load_config(self):
        if not os.path.exists("config.json"):
            return

        try:
            with open("config.json") as file:
                config = json.load(file)

            self.username_input.setText(config.get("username", ""))

            self.select_loader(config.get("loader", "vanilla"))

            saved_version = config.get("version", "1.20.1")

            for i in range(self.version_box.count()):
                if self.clean_version_name(self.version_box.itemText(i)) == saved_version:
                    self.version_box.setCurrentIndex(i)
                    break

            self.ram_slider.setValue(
                int(config.get("ram", "4G").replace("G", ""))
            )
            self.update_ram_label()

            active_profile = config.get("active_profile", "")

            if active_profile and active_profile in self.profiles:
                self._activate_profile(active_profile)

        except Exception:
            self.status_label.setText("Could not load config.json")

    # ─────────────── FOLDERS / LAUNCH ───────────────

    def open_folder(self, name):
        path = os.path.join(LAUNCHER_DIR, name)

        pathlib.Path(path).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", path])

    def launch_minecraft(self):
        self.save_config()

        username = self.username_input.text().strip() or "Player"
        version = self.clean_version_name(self.version_box.currentText())
        ram = f"{self.ram_slider.value()}G"

        profile_data = self.profiles.get(self.current_profile_name, {})
        mc_dir = profile_data.get("directory", LAUNCHER_DIR)

        self.refresh_microsoft_login()

        if self.auth_data:
            username = self.auth_data.get("name", username)
            uuid = self.auth_data.get("id", "")
            token = self.auth_data.get("access_token", "")
        else:
            uuid = ""
            token = ""

        options = {
            "username": username,
            "uuid": uuid,
            "token": token,
            "jvmArguments": [f"-Xmx{ram}"],
            "gameDirectory": mc_dir,
        }

        self.launch_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting...")

        self.install_thread = InstallThread(
            version,
            self.selected_loader,
            mc_dir,
            options
        )

        self.install_thread.status.connect(self.status_label.setText)
        self.install_thread.progress.connect(self.progress_bar.setValue)
        self.install_thread.max_progress.connect(
            self.progress_bar.setMaximum
        )
        self.install_thread.finished.connect(self._on_launch_done)
        self.install_thread.error.connect(self._on_launch_error)
        self.install_thread.start()

    def _on_launch_done(self, command):
        subprocess.Popen(command)

        self.progress_bar.setValue(self.progress_bar.maximum())
        self.status_label.setText("Minecraft launched!")
        self.launch_button.setEnabled(True)
        self.refresh_version_list()

    def _on_launch_error(self, message):
        self.status_label.setText(f"Error: {message}")
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
            QWidget {
                color: #f4f4f4;
                font-family: Arial;
                font-size: 14px;
            }

            QFrame#sidebar {
                background: rgba(8, 8, 12, 178);
                border: 1px solid rgba(255, 70, 70, 120);
                border-radius: 22px;
            }

            QLabel#sidebarTitle {
                font-size: 21px;
                font-weight: bold;
                color: #ff3b3b;
                padding: 6px;
            }

            QPushButton#tabButton {
                background: rgba(255, 255, 255, 18);
                color: #f2f2f2;
                border: 1px solid rgba(255, 80, 80, 55);
                border-radius: 12px;
                padding: 10px 12px;
                text-align: left;
                font-size: 13px;
                font-weight: bold;
            }

            QPushButton#tabButton:hover {
                background: rgba(255, 60, 60, 70);
                border: 1px solid rgba(255, 90, 90, 160);
            }

            QPushButton#tabButton:checked {
                background: rgba(220, 0, 0, 210);
                border: 1px solid rgba(255, 120, 120, 230);
            }

            QFrame#panel {
                background: rgba(10, 10, 14, 162);
                border: 1px solid rgba(255, 70, 70, 125);
                border-radius: 24px;
            }

            QFrame#loaderSection {
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 70, 70, 85);
                border-radius: 16px;
            }

            QLabel#title {
                font-size: 31px;
                font-weight: bold;
                color: #ff3333;
            }

            QLabel#subtitle {
                color: #d8d8d8;
                font-size: 13px;
            }

            QLabel#status {
                color: #e0e0e0;
                font-size: 13px;
            }

            QLabel#loaderTitle {
                font-size: 13px;
                font-weight: bold;
                color: #ff6b6b;
            }

            QLabel#loaderInfo {
                font-size: 12px;
                color: #c9c9c9;
            }

            QWidget#contentCard {
                background: rgba(255, 255, 255, 16);
                border: 1px solid rgba(255, 80, 80, 55);
                border-radius: 14px;
            }

            QWidget#contentCard:hover {
                background: rgba(255, 60, 60, 42);
                border: 1px solid rgba(255, 100, 100, 140);
            }

            QLabel#modIcon {
                background: rgba(0, 0, 0, 95);
                border: 1px solid rgba(255, 90, 90, 80);
                border-radius: 12px;
                color: #aaa;
                font-size: 16px;
            }

            QLabel#modName {
                font-size: 14px;
                font-weight: bold;
                color: #ffffff;
            }

            QLabel#modAuthor {
                font-size: 12px;
                color: #b6b6b6;
            }

            QLabel#modDownloads {
                font-size: 12px;
                color: #ff7777;
            }

            QLabel#modDesc {
                font-size: 12px;
                color: #d0d0d0;
            }

            QLabel#badge {
                background: rgba(255, 55, 55, 45);
                color: #ffb0b0;
                font-size: 11px;
                border-radius: 6px;
                padding: 2px 7px;
            }

            QPushButton#dlBtn {
                background: rgba(0, 130, 45, 210);
                font-size: 12px;
                padding: 7px;
                border-radius: 10px;
                border: 1px solid rgba(80, 255, 140, 100);
            }

            QPushButton#dlBtn:hover {
                background: rgba(0, 170, 55, 235);
            }

            QPushButton#dlBtn:disabled {
                background: rgba(0, 55, 20, 160);
                color: #777;
            }

            QPushButton#deleteBtn {
                background: rgba(120, 0, 0, 190);
            }

            QPushButton#deleteBtn:hover {
                background: rgba(180, 0, 0, 230);
            }

            QLineEdit, QComboBox {
                background: rgba(255, 255, 255, 22);
                border: 1px solid rgba(255, 80, 80, 85);
                border-radius: 12px;
                padding: 9px;
                color: white;
                font-size: 14px;
            }

            QLineEdit:hover, QComboBox:hover {
                background: rgba(255, 255, 255, 30);
                border: 1px solid rgba(255, 90, 90, 135);
            }

            QLineEdit:focus, QComboBox:focus {
                border: 1px solid rgba(255, 110, 110, 230);
                background: rgba(0, 0, 0, 120);
            }

            QListWidget, QListWidget#profilesList {
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 80, 80, 75);
                border-radius: 12px;
                padding: 5px;
                color: white;
                font-size: 13px;
            }

            QListWidget::item {
                padding: 8px;
                border-radius: 8px;
            }

            QListWidget::item:hover {
                background: rgba(255, 60, 60, 45);
            }

            QListWidget::item:selected {
                background: rgba(220, 0, 0, 210);
            }

            QScrollArea#modsScroll {
                background: transparent;
                border: 1px solid rgba(255, 80, 80, 70);
                border-radius: 16px;
            }

            QWidget#modsContainer {
                background: transparent;
            }

            QSlider::groove:horizontal {
                border: 1px solid rgba(255, 80, 80, 90);
                height: 9px;
                background: rgba(255,255,255,18);
                border-radius: 5px;
            }

            QSlider::handle:horizontal {
                background: #dd0000;
                border: 1px solid rgba(255,160,160,210);
                width: 20px;
                margin: -6px 0;
                border-radius: 10px;
            }

            QSlider::handle:horizontal:hover {
                background: #ff2222;
            }

            QPushButton {
                background: rgba(205, 0, 0, 210);
                color: white;
                border: 1px solid rgba(255, 100, 100, 95);
                border-radius: 13px;
                padding: 11px;
                font-size: 14px;
                font-weight: bold;
            }

            QPushButton:hover {
                background: rgba(255, 25, 25, 230);
                border: 1px solid rgba(255, 150, 150, 180);
            }

            QPushButton:pressed {
                background: rgba(145, 0, 0, 230);
            }

            QPushButton:disabled {
                background: rgba(75, 0, 0, 130);
                color: #888;
            }

            QPushButton#loaderBtn {
                font-size: 13px;
                padding: 8px 14px;
                border-radius: 10px;
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 80, 80, 75);
            }

            QPushButton#loaderBtn:hover {
                background: rgba(255, 60, 60, 60);
            }

            QPushButton#loaderBtn:checked {
                background: rgba(210, 0, 0, 220);
                border: 1px solid rgba(255, 130, 130, 210);
            }

            QProgressBar {
                border: 1px solid rgba(255, 80, 80, 90);
                border-radius: 11px;
                text-align: center;
                background: rgba(255,255,255,18);
                color: white;
                height: 22px;
                font-size: 13px;
            }

            QProgressBar::chunk {
                background: rgba(220, 0, 0, 225);
                border-radius: 10px;
            }
        """)


# ═══════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════

app = QApplication(sys.argv)
if os.path.exists(APP_ICON):
    app.setWindowIcon(QIcon(APP_ICON))
window = Launcher()
window.show()
sys.exit(app.exec())

