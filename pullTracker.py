import sys
import csv
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox, QComboBox,
    QLineEdit, QCheckBox, QScrollArea, QGridLayout, QFrame,
    QButtonGroup
)


# ---------------------------------------------------------------------------
# WuWa Convene API
# Based on the user's original tracker.
# ---------------------------------------------------------------------------

API_URL = "https://gmserver-api.aki-game2.net/gacha/record/query"
OUTPUT_FILE = "wuwa_convene_records.csv"

# Standard 5-star characters.
# Add/update names here when WuWa adds another permanent standard 5-star.
STANDARD_5_STAR_CHARACTERS = {
    "Verina",
    "Encore",
    "Calcharo",
    "Lingyang",
    "Jianxin",
}

# Permanent / standard 5-star weapons.
# Any other 5-star weapon is treated as a featured/limited 5-star weapon.
STANDARD_5_STAR_WEAPONS = {
    "Emerald of Genesis",
    "Lustrous Razor",
    "Static Mist",
    "Cosmic Ripples",
    "Abyss Surges",
}

# If the API gives localized names, add those names here too.
# A 5-star CHARACTER in this set is treated as a 50/50 LOSS.
# Other 5-star characters are treated as FEATURED/WIN for detection.


def parse_convene_url(url):
    if "#" in url:
        query_part = url.split("#", 1)[1]
        if "?" in query_part:
            query_part = query_part.split("?", 1)[1]
        else:
            query_part = ""
    else:
        query_part = urlparse(url).query

    params = parse_qs(query_part)

    required = ["player_id", "record_id", "svr_id", "resources_id"]
    result = {}

    for key in required:
        if key not in params:
            raise ValueError(f"Missing parameter: {key}")
        result[key] = params[key][0]

    result["lang"] = params.get("lang", ["en"])[0]
    return result


def fetch_records(info, pool_type):
    payload = {
        "playerId": info["player_id"],
        "cardPoolId": info["resources_id"],
        "cardPoolType": pool_type,
        "languageCode": info["lang"],
        "recordId": info["record_id"],
        "serverId": info["svr_id"],
    }

    response = requests.post(API_URL, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def extract_records(data):
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ["data", "records", "record", "list"]:
        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for subkey in ["records", "list", "data"]:
                if isinstance(value.get(subkey), list):
                    return value[subkey]

    return []


def fetch_all_records(info):
    all_records = []

    for pool_type in range(1, 9):
        try:
            data = fetch_records(info, pool_type)
            records = extract_records(data)

            for record in records:
                if isinstance(record, dict):
                    if "cardPoolType" not in record:
                        record["cardPoolType"] = pool_type
                    all_records.append(record)

        except requests.RequestException as e:
            print(f"Pool {pool_type}: request failed: {e}")
        except Exception as e:
            print(f"Pool {pool_type}: error: {e}")

    unique = {}

    for record in all_records:
        key = (
            record.get("time"),
            record.get("name"),
            record.get("qualityLevel"),
            record.get("cardPoolType"),
            record.get("resourceId"),
        )
        unique[key] = record

    all_records = list(unique.values())
    all_records.sort(key=lambda x: x.get("time", ""))
    return all_records


def save_csv(records, output_file=OUTPUT_FILE):
    if not records:
        return

    fieldnames = set()
    for record in records:
        if isinstance(record, dict):
            fieldnames.update(record.keys())

    preferred = [
        "time", "name", "qualityLevel", "cardPoolType",
        "resourceType", "resourceId", "count"
    ]

    fields = [f for f in preferred if f in fieldnames]
    fields += sorted(f for f in fieldnames if f not in fields)

    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def read_csv(file_path):
    rows = []

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        required = {"time", "name", "qualityLevel", "resourceType"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                "This does not appear to be a WuWa convene CSV.\n\n"
                "Required columns: time, name, qualityLevel, resourceType"
            )

        for row in reader:
            try:
                row["qualityLevel"] = int(row["qualityLevel"])
                row["datetime"] = datetime.strptime(
                    row["time"], "%Y-%m-%d %H:%M:%S"
                )
                rows.append(row)
            except (ValueError, TypeError):
                continue

    rows.sort(key=lambda x: x["datetime"])
    return rows


# ---------------------------------------------------------------------------
# 50/50 detection
# ---------------------------------------------------------------------------

def is_character(row):
    # The actual WuWa CSV uses "Resonator" for characters.
    return row["resourceType"].strip().casefold() in {
        "resonator",
        "character",
    }


def is_weapon(row):
    return row["resourceType"].strip().casefold() == "weapon"


def is_standard_5_star(row):
    name = row["name"].strip().casefold()
    standard = {x.casefold() for x in STANDARD_5_STAR_CHARACTERS}
    return (
        row["qualityLevel"] == 5
        and is_character(row)
        and name in standard
    )


def is_standard_5_star_weapon(row):
    name = row["name"].strip().casefold()
    standard = {x.casefold() for x in STANDARD_5_STAR_WEAPONS}
    return (
        row["qualityLevel"] == 5
        and is_weapon(row)
        and name in standard
    )


def is_featured_5_star_weapon(row):
    return (
        row["qualityLevel"] == 5
        and is_weapon(row)
        and not is_standard_5_star_weapon(row)
    )


# Only this pool is the featured character banner in the supplied CSV.
# Standard 5★ characters in this pool are 50/50 losses.
FEATURED_CHARACTER_POOL = "Resonators Accurate Modulation"


def annotate_5050(rows):
    """
    Calculate pity strictly per cardPoolType.

    For each unique cardPoolType:
      - Count only pulls belonging to that exact pool.
      - Process chronologically.
      - A 5★ displays the number of pulls in that same pool since the
        previous 5★, including the current 5★ pull.
      - Reset only that pool after its 5★.

    Pulls from other cardPoolType values never affect this counter.
    """
    ordered = list(enumerate(rows))
    ordered.sort(key=lambda pair: (pair[1]["datetime"], pair[0]))

    pulls_since_5star = {}
    calculated = []

    for original_index, row in ordered:
        item = dict(row)
        pool = str(row.get("cardPoolType", "")).strip()

        if pool not in pulls_since_5star:
            pulls_since_5star[pool] = 0

        try:
            pull_count = int(row.get("count", 1) or 1)
        except (TypeError, ValueError):
            pull_count = 1
        pull_count = max(1, pull_count)

        pulls_since_5star[pool] += pull_count
        item["5050"] = ""
        item["pity"] = ""

        if row["qualityLevel"] == 5:
            item["pity"] = pulls_since_5star[pool]

            if pool == FEATURED_CHARACTER_POOL and is_character(row):
                item["5050"] = "LOSS" if is_standard_5_star(row) else "WIN"

            # Reset ONLY this exact cardPoolType.
            pulls_since_5star[pool] = 0

        calculated.append((original_index, item))

    calculated.sort(key=lambda pair: (pair[1]["datetime"], pair[0]))
    return [item for _, item in calculated]



# ---------------------------------------------------------------------------
# Tile UI
# ---------------------------------------------------------------------------

class PullTile(QFrame):
    def __init__(self, row, index, clicked_callback):
        super().__init__()
        self.row = row
        self.index = index

        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumSize(190, 125)
        self.setMaximumHeight(145)

        q = row["qualityLevel"]
        resource = row["resourceType"].strip().casefold()
        is_loss = row["5050"] == "LOSS"

        # Classification
        if is_loss:
            category = "50/50 LOSS"
            border = "#ff3b30"
            title_color = "#ff6259"
        elif q == 5 and resource in ("resonator", "character"):
            category = "5★ CHARACTER"
            border = "#ffd447"
            title_color = "#ffd447"
        elif q == 5 and resource == "weapon":
            if is_standard_5_star_weapon(row):
                category = "5★ STANDARD WEAPON"
                border = "#7f8794"
                title_color = "#c3cad5"
            else:
                category = "5★ FEATURED WEAPON"
                border = "#ffd447"
                title_color = "#ffd447"
        elif q == 4 and resource in ("resonator", "character"):
            category = "4★ CHARACTER"
            border = "#b47cff"
            title_color = "#c99cff"
        elif q == 4 and resource == "weapon":
            category = "4★ WEAPON"
            border = "#b47cff"
            title_color = "#c99cff"
        else:
            category = "3★"
            border = "#555b66"
            title_color = "#a1a7b0"

        self.setStyleSheet(f"""
            QFrame {{
                background: #17191e;
                border: 2px solid {border};
                border-radius: 10px;
            }}
            QFrame:hover {{
                background: #20232a;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        category_label = QLabel(category)
        category_label.setStyleSheet(
            f"color: {title_color}; border: none; font-weight: bold;"
        )
        layout.addWidget(category_label)

        name = QLabel(row["name"])
        name.setWordWrap(True)
        name.setStyleSheet(
            "color: #f2f3f5; border: none; font-size: 14px; font-weight: bold;"
        )
        layout.addWidget(name, 1)

        time = QLabel(row["time"])
        time.setStyleSheet(
            "color: #9aa1ac; border: none; font-size: 11px;"
        )
        layout.addWidget(time)

        details = []
        if q >= 4:
            details.append(f"{q}★")
        if row["pity"]:
            details.append(f"Pity {row['pity']}")
        if row["5050"]:
            details.append(row["5050"])

        detail_label = QLabel("  •  ".join(details) if details else resource.title())
        detail_label.setStyleSheet(
            "color: #b8bec8; border: none; font-size: 11px;"
        )
        layout.addWidget(detail_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked_callback(self.index)
        super().mousePressEvent(event)


class WuWaTimelineViewer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("WuWa Convene Timeline + 50/50 Detector")
        self.resize(1500, 900)

        self.rows = []
        self.filtered_rows = []
        self.selected_index = -1
        self.source_path = None
        self.player_info = None

        self.setup_ui()

    def setup_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)

        # URL / tracker bar
        tracker_bar = QHBoxLayout()

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Paste WuWa convene records URL here..."
        )
        tracker_bar.addWidget(self.url_edit, 1)

        self.fetch_button = QPushButton("Fetch Records")
        tracker_bar.addWidget(self.fetch_button)

        self.open_button = QPushButton("Open CSV")
        tracker_bar.addWidget(self.open_button)

        self.save_button = QPushButton("Save CSV")
        self.save_button.setEnabled(False)
        tracker_bar.addWidget(self.save_button)

        main.addLayout(tracker_bar)

        # Search
        search_bar = QHBoxLayout()

        search_bar.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search item name...")
        search_bar.addWidget(self.search_edit, 1)

        self.highlight_loss = QCheckBox("Highlight 50/50 losses")
        self.highlight_loss.setChecked(True)
        search_bar.addWidget(self.highlight_loss)

        self.stats_label = QLabel("No records loaded")
        search_bar.addWidget(self.stats_label)

        main.addLayout(search_bar)

        # Explicit filter buttons. Only one is active at a time.
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Display:"))

        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)

        filters = [
            ("5★ Character", "5★ Characters"),
            ("4★ Character", "4★ Characters"),
            ("5★ Featured Weapon", "5★ Featured Weapons"),
            ("5★ Standard Weapon", "5★ Standard Weapons"),
            ("4★ Weapon", "4★ Weapons"),
            ("50/50 Loss", "50/50 losses only"),
        ]

        for i, (label, filter_name) in enumerate(filters):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("filter_name", filter_name)
            self.filter_group.addButton(button, i)
            filter_bar.addWidget(button)

            if i == 0:
                button.setChecked(True)

        filter_bar.addStretch()
        main.addLayout(filter_bar)

        # Tile area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background: #101216;
            }
        """)

        self.tile_container = QWidget()
        self.tile_container.setStyleSheet("background: #101216;")
        self.tile_layout = QGridLayout(self.tile_container)
        self.tile_layout.setContentsMargins(8, 8, 8, 8)
        self.tile_layout.setHorizontalSpacing(8)
        self.tile_layout.setVerticalSpacing(8)
        self.scroll.setWidget(self.tile_container)

        main.addWidget(self.scroll, 1)

        # Selected pull information
        self.info_label = QLabel(
            "Paste a WuWa convene URL or open a CSV."
        )
        self.info_label.setStyleSheet("""
            QLabel {
                background: #17191e;
                border: 1px solid #353943;
                border-radius: 7px;
                padding: 8px;
                color: #e5e7eb;
            }
        """)
        main.addWidget(self.info_label)

        # Navigation
        nav = QHBoxLayout()

        self.previous_button = QPushButton("← Previous")
        self.next_button = QPushButton("Next →")
        self.reset_view_button = QPushButton("Scroll to Top")

        nav.addWidget(self.previous_button)
        nav.addWidget(self.next_button)
        nav.addWidget(self.reset_view_button)
        nav.addStretch()

        self.legend = QLabel(
            "5★ Character / Featured Weapon = gold   |   "
            "50/50 LOSS = red   |   4★ = purple   |   3★ = gray   |   "
            "Pity = pulls since previous 5★ in the same cardPoolType"
        )
        nav.addWidget(self.legend)

        main.addLayout(nav)

        # Connections
        self.fetch_button.clicked.connect(self.fetch_from_url)
        self.open_button.clicked.connect(self.open_csv)
        self.save_button.clicked.connect(self.save_current_csv)
        self.search_edit.textChanged.connect(self.refresh_tiles)
        self.highlight_loss.stateChanged.connect(self.refresh_tiles)
        self.filter_group.idClicked.connect(self.filter_changed)

        self.previous_button.clicked.connect(self.previous_pull)
        self.next_button.clicked.connect(self.next_pull)
        self.reset_view_button.clicked.connect(self.scroll_to_top)

        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_rows(self, rows, source_path=None):
        if not rows:
            raise ValueError("No valid convene records found.")

        self.rows = annotate_5050(rows)
        self.source_path = source_path
        self.selected_index = -1

        self.save_button.setEnabled(True)
        self.selected_index = -1

        self.apply_filter()

    def fetch_from_url(self):
        url = self.url_edit.text().strip()

        if not url:
            QMessageBox.warning(
                self,
                "Missing URL",
                "Paste your WuWa convene records URL first."
            )
            return

        try:
            info = parse_convene_url(url)
        except Exception as e:
            QMessageBox.critical(self, "Invalid URL", str(e))
            return

        self.fetch_button.setEnabled(False)
        self.info_label.setText("Fetching convene records...")

        QApplication.processEvents()

        try:
            records = fetch_all_records(info)

            if not records:
                raise ValueError(
                    "No records were returned from the WuWa API."
                )

            self.player_info = info
            self.load_rows(records)

            save_csv(records)

            self.info_label.setText(
                f"Fetched {len(records)} records | "
                f"Player: {info['player_id']} | "
                f"CSV: {Path(OUTPUT_FILE).resolve()}"
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Fetch Error",
                str(e)
            )
            self.info_label.setText("Fetch failed.")

        finally:
            self.fetch_button.setEnabled(True)

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open WuWa Convene CSV",
            "",
            "CSV Files (*.csv);;All Files (*)"
        )

        if not path:
            return

        try:
            rows = read_csv(path)
            self.load_rows(rows, path)
            self.info_label.setText(
                f"Loaded {len(rows)} records | {path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "CSV Error", str(e))

    def save_current_csv(self):
        if not self.rows:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save WuWa Convene CSV",
            OUTPUT_FILE,
            "CSV Files (*.csv)"
        )

        if not path:
            return

        try:
            # Save the detector fields as well.
            fieldnames = [
                "time", "name", "qualityLevel", "cardPoolType",
                "resourceType", "resourceId", "count", "5050", "pity"
            ]

            with open(
                path,
                "w",
                newline="",
                encoding="utf-8-sig"
            ) as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=fieldnames,
                    extrasaction="ignore"
                )
                writer.writeheader()
                writer.writerows(self.rows)

            self.info_label.setText(
                f"Saved {len(self.rows)} records to {path}"
            )

        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def current_filter(self):
        checked = self.filter_group.checkedButton()
        if checked is None:
            return "All pulls"
        return checked.property("filter_name")

    def filter_changed(self, button_id):
        self.selected_index = -1
        self.refresh_tiles()

    def row_matches_filter(self, row):
        filter_name = self.current_filter()

        q = row["qualityLevel"]
        char = is_character(row)
        weapon = is_weapon(row)

        if filter_name == "5★ Characters":
            return q == 5 and char

        if filter_name == "4★ Characters":
            return q == 4 and char

        if filter_name == "5★ Featured Weapons":
            return is_featured_5_star_weapon(row)

        if filter_name == "5★ Standard Weapons":
            return is_standard_5_star_weapon(row)

        if filter_name == "4★ Weapons":
            return q == 4 and weapon

        if filter_name == "50/50 losses only":
            return row["5050"] == "LOSS"

        return False

    def get_filtered_rows(self):
        if not self.rows:
            return []

        search = self.search_edit.text().strip().lower()

        result = []

        for row in self.rows:
            if not self.row_matches_filter(row):
                continue

            if search and search not in row["name"].lower():
                continue

            result.append(row)

        return result

    def refresh_tiles(self):
        if not self.rows:
            return

        self.filtered_rows = self.get_filtered_rows()

        self.clear_tiles()
        self.build_tiles()
        self.update_stats()

        if not self.filtered_rows:
            self.info_label.setText("No pulls match the selected filter.")
        elif self.selected_index >= len(self.filtered_rows):
            self.selected_index = -1

    def clear_tiles(self):
        while self.tile_layout.count():
            item = self.tile_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def build_tiles(self):
        # Four cards per row on a normal desktop window.
        columns = 4

        for i, row in enumerate(self.filtered_rows):
            tile = PullTile(row, i, self.select_pull)

            # If selected, visually emphasize it.
            if i == self.selected_index:
                tile.setStyleSheet(tile.styleSheet() + """
                    QFrame {
                        background: #292d36;
                        border-width: 3px;
                    }
                """)

            self.tile_layout.addWidget(
                tile,
                i // columns,
                i % columns
            )

        self.tile_layout.setRowStretch(
            (len(self.filtered_rows) // columns) + 1,
            1
        )

    def apply_filter(self):
        # Compatibility with the loading code.
        self.selected_index = -1
        self.refresh_tiles()

    # ------------------------------------------------------------------
    # Tile selection / navigation
    # ------------------------------------------------------------------

    def select_pull(self, index):
        if index < 0 or index >= len(self.filtered_rows):
            return

        self.selected_index = index
        row = self.filtered_rows[index]

        resource = row["resourceType"].strip().lower()

        if is_standard_5_star_weapon(row):
            category = "Standard 5★ Weapon"
        elif is_featured_5_star_weapon(row):
            category = "Featured 5★ Weapon"
        elif is_standard_5_star(row):
            category = "Standard 5★ Character / 50/50 LOSS"
        elif row["qualityLevel"] == 5 and is_character(row):
            category = "Featured 5★ Character"
        elif row["qualityLevel"] == 4 and is_character(row):
            category = "4★ Character"
        elif row["qualityLevel"] == 4 and is_weapon(row):
            category = "4★ Weapon"
        else:
            category = "3★"

        result = [
            row["time"],
            row["name"],
            f"{row['qualityLevel']}★",
            category,
        ]

        if row["5050"]:
            result.append(row["5050"])

        if row["pity"]:
            result.append(f"Pity {row['pity']}")

        self.info_label.setText("  |  ".join(result))

        self.rebuild_selection()

        # Scroll selected tile into view.
        self.scroll_selected_tile(index)

    def rebuild_selection(self):
        self.clear_tiles()
        self.build_tiles()

    def scroll_selected_tile(self, index):
        item = self.tile_layout.itemAtPosition(index // 4, index % 4)
        if item and item.widget():
            self.scroll.ensureWidgetVisible(item.widget())

    def previous_pull(self):
        if not self.filtered_rows:
            return

        if self.selected_index <= 0:
            self.select_pull(0)
        else:
            self.select_pull(self.selected_index - 1)

    def next_pull(self):
        if not self.filtered_rows:
            return

        if self.selected_index < 0:
            self.select_pull(0)
        elif self.selected_index < len(self.filtered_rows) - 1:
            self.select_pull(self.selected_index + 1)
        else:
            self.select_pull(len(self.filtered_rows) - 1)

    def scroll_to_top(self):
        self.scroll.verticalScrollBar().setValue(0)

    def update_stats(self):
        if not self.rows:
            self.stats_label.setText("No records loaded")
            return

        five_chars = [
            r for r in self.rows
            if r["qualityLevel"] == 5
            and is_character(r)
        ]

        losses = [
            r for r in five_chars
            if r["5050"] == "LOSS"
        ]

        featured_weapons = [
            r for r in self.rows if is_featured_5_star_weapon(r)
        ]

        standard_weapons = [
            r for r in self.rows if is_standard_5_star_weapon(r)
        ]

        four_star_weapons = [
            r for r in self.rows
            if r["qualityLevel"] == 4
            and is_weapon(r)
        ]

        self.stats_label.setText(
            f"Pulls: {len(self.rows)}  |  "
            f"5★ Char: {len(five_chars)}  |  "
            f"Featured 5★ Weapon: {len(featured_weapons)}  |  "
            f"Standard 5★ Weapon: {len(standard_weapons)}  |  "
            f"4★ Weapon: {len(four_star_weapons)}  |  "
            f"50/50 Losses: {len(losses)}"
        )

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.previous_pull()
        elif event.key() == Qt.Key_Right:
            self.next_pull()
        elif event.key() == Qt.Key_Home:
            self.select_pull(0)
        elif event.key() == Qt.Key_End:
            if self.filtered_rows:
                self.select_pull(len(self.filtered_rows) - 1)
        else:
            super().keyPressEvent(event)



def main():
    app = QApplication(sys.argv)

    window = WuWaTimelineViewer()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
