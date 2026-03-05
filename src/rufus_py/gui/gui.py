import sys
import json
import urllib.parse
import webbrowser
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QGridLayout, QLabel, QComboBox, 
                             QPushButton, QProgressBar, QCheckBox, 
                             QMessageBox, QDialog, QTextEdit, QFileDialog, 
                             QLineEdit, QFrame, QStatusBar, QToolButton, QSpacerItem)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from rufus_py.drives import states
from rufus_py.drives import formatting as fo
from rufus_py.writing.flash_usb import FlashUSB
from rufus_py.drives.autodetect_usb import UsbMonitor

class LogWindow(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Rufus Log")
        self.resize(650, 450)
        layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet("background-color: white; border: 1px solid #ccc;")
        layout.addWidget(self.log_text)
        self.setLayout(layout)

class AboutWindow(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("About")
        self.resize(650, 450)
        layout = QVBoxLayout()
        self.about_text = QTextEdit()
        self.about_text.setReadOnly(True)
        self.about_text.setFont(QFont("Consolas", 9))
        self.about_text.setStyleSheet("background-color: white; border: 1px solid #ccc;")
        layout.addWidget(self.about_text)
        self.setLayout(layout)

class FlashWorker(QThread): # this is so the ui dont freeze when flashing
    finished = pyqtSignal(bool)
    progress = pyqtSignal(str)
    def __init__(self, iso_path: str, mount_path: str):
        super().__init__()
        self.iso_path = iso_path
        self.mount_path = mount_path
    def run(self):
        try:
            self.progress.emit("Unmounting drive...")
            fo.unmount()
            #update progress bar
            self.progress.emit("Flashing ISO to device...")
            result = FlashUSB(self.iso_path, self.mount_path)
            #see above
            if result:
                self.progress.emit("Flashing complete!")
            else:
                self.progress.emit("Flash failed.")
            #see above
            self.finished.emit(result)
            #yay it worked
        except Exception as e:
            self.progress.emit(f"Error: {str(e)}")
            self.finished.emit(False)
            #change progress bar 2: electric boogaloo

class Rufus(QMainWindow):
    def __init__(self, usb_devices=None):
        super().__init__()
        self.monitor = UsbMonitor()
        self.monitor.device_added.connect(self.on_usb_added)
        self.monitor.device_removed.connect(self.on_usb_removed)
        self.monitor.device_list_updated.connect(self.update_usb_list)
        
        self.usb_devices = usb_devices or {}
        self.setWindowTitle("Rufus")
        self.setFixedSize(640, 700) 
        
        self.setStyleSheet("""
            QMainWindow {
                background-color: #F0F0F0;
                font-family: 'Segoe UI', Tahoma, sans-serif;
                font-size: 9pt;
                color: #000000;
            }
            QLabel {
                color: #000000;
                padding: 2px;
            }
            /* Section Headers */
            QLabel#sectionHeader {
                font-size: 16pt;
                font-weight: normal;
                color: #000000;
                padding: 5px 0;
            }
            QComboBox, QLineEdit {
                border: 1px solid #D0D0D0;
                border-radius: 2px;
                padding: 4px 6px;
                background-color: white;
                min-height: 24px;
                max-height: 24px;
                font-size: 9pt;
                selection-background-color: #0078D7;
            }
            QComboBox:focus, QLineEdit:focus {
                border: 1px solid #0078D7;
            }
            QComboBox::drop-down {
                width: 20px;
                border-left: 1px solid #D0D0D0;
            }
            QPushButton {
                background-color: #E1E1E1;
                border: 1px solid #A0A0A0;
                border-radius: 2px;
                padding: 4px 15px;
                min-height: 20px;
                max-height: 20px;
                min-width: 100px;
                max-width: 100px;
                font-size: 9pt;
            }
            QPushButton:hover {
                background-color: #E5F1FB;
                border-color: #0078D7;
            }
            QPushButton:pressed {
                background-color: #D0D0D0;
            }
            QPushButton:disabled {
                color: #888888;
                background-color: #F0F0F0;
                border-color: #D0D0D0;
            }
            #btnStart {
                background-color: #E1E1E1;
                border: 1px solid #A0A0A0;
                border-radius: 2px;
                min-height: 20px;
                max-height: 20px;
                min-width: 100px;
                max-width: 100px;
                padding: 4px 15px;
                font-size: 9pt;
            }
            #btnStart:hover {
                background-color: #E5F1FB;
                border-color: #0078D7;
            }
            #btnStart:pressed {
                background-color: #00AA00;
            }
            #btnStart:disabled {
                color: #888888;
                background-color: #F0F0F0;
                border-color: #D0D0D0;
            }
            QCheckBox {
                spacing: 5px;
                font-size: 9pt;
            }
            QProgressBar {
                border: 1px solid #A0A0A0;
                border-radius: 2px;
                text-align: center;
                background-color: white;
                height: 22px;
                font-size: 9pt;
                color: white;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #00CC00;
            }
            /* Icon Only Buttons */
            QToolButton {
                border: 1px solid #D0D0D0;
                background-color: white;
                border-radius: 2px;
                padding: 4px;
                min-width: 32px;
                max-width: 32px;
                min-height: 32px;
                max-height: 32px;
                font-size: 18px;
            }
            QToolButton:hover {
                background-color: #E5F1FB;
                border-color: #0078D7;
            }
            QToolButton:pressed {
                background-color: #D0D0D0;
            }
            QStatusBar {
                background-color: #F0F0F0;
                border-top: 1px solid #D0D0D0;
                font-size: 9pt;
                color: #000000;
            }
            QLabel#linkLabel {
                color: #000000;
                text-decoration: none;
                font-size: 9pt;
            }
            QLabel#linkLabel:hover {
                color: #0078D7;
                text-decoration: underline;
            }
        """)

        self.init_ui()

    def create_header(self, text):
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 10, 0, 5)
        label = QLabel(text)
        label.setObjectName("sectionHeader")
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("background-color: #000000; min-height: 1px; max-height: 1px;")
        
        layout.addWidget(label)
        layout.addWidget(line, 1)
        return layout
    
    def update_usb_list(self,devices:dict):
        self.combo_device.clear()
        
        if not devices:
            self.combo_device.addItem("No USB devices found")
            return
        
        for node in devices:
            self.combo_device.addItem(node)

    def on_usb_added(self, node):
        QMessageBox.information(self, "USB Inserted", f"{node} connected")

    def on_usb_removed(self, node):
        QMessageBox.warning(self, "USB Removed", f"{node} disconnected")
    
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(15, 10, 15, 10)

        main_layout.addLayout(self.create_header("Drive Properties"))

        lbl_device = QLabel("Device")
        lbl_device.setStyleSheet("font-weight: normal; font-size: 9pt; padding-bottom: 2px;")
        self.combo_device = QComboBox()
        
        # Populate combo box with detected USB devices
        # if self.usb_devices:
            
        #     for path, label in self.usb_devices.items():
        #         self.combo_device.addItem(f"{label} ({path})")
        # else:
        #     self.combo_device.addItem("No USB devices found")
        
        device_layout = QVBoxLayout()
        device_layout.setSpacing(2)
        device_layout.addWidget(lbl_device)
        device_layout.addWidget(self.combo_device)
        main_layout.addLayout(device_layout)

        lbl_boot = QLabel("Boot selection")
        lbl_boot.setStyleSheet("font-weight: normal; font-size: 9pt; padding-bottom: 2px;")
        
        boot_row = QHBoxLayout()
        boot_row.setSpacing(5)
        
        self.combo_boot = QComboBox()
        self.combo_boot.setEditable(True)
        self.combo_boot.lineEdit().setReadOnly(True)
        self.combo_boot.addItem("installationmedia.iso")
        
        lbl_check = QLabel("✓") 
        lbl_check.setStyleSheet("font-size: 14pt; color: #666; padding: 0 5px;")
        
        btn_select = QPushButton("SELECT")
        btn_select.clicked.connect(self.browse_file)

        
        boot_row.addWidget(self.combo_boot, 1)
        boot_row.addWidget(lbl_check)
        boot_row.addWidget(btn_select)
        
        boot_layout = QVBoxLayout()
        boot_layout.setSpacing(2)
        boot_layout.addWidget(lbl_boot)
        boot_layout.addLayout(boot_row)
        main_layout.addLayout(boot_layout)

        lbl_image = QLabel("Image option")
        lbl_image.setStyleSheet("font-weight: normal; font-size: 9pt; padding-bottom: 2px;")
        self.combo_image_option = QComboBox()
        self.combo_image_option.addItem("Standard Windows installation")
        #self.combo_image_option.addItem("Windows To Go")
        self.combo_image_option.addItem("Standard Linux")
        self.combo_image_option.currentTextChanged.connect(self.update_image_option)

        image_layout = QVBoxLayout()
        image_layout.setSpacing(2)
        image_layout.addWidget(lbl_image)
        image_layout.addWidget(self.combo_image_option)
        main_layout.addLayout(image_layout)

        grid_part = QGridLayout()
        grid_part.setSpacing(10)
        grid_part.setColumnStretch(1, 1)
        grid_part.setColumnStretch(3, 1)
        
        lbl_part = QLabel("Partition scheme")
        lbl_part.setStyleSheet("font-weight: normal; font-size: 9pt;")
        self.combo_partition = QComboBox()
        self.combo_partition.addItem("GPT")
        self.combo_partition.addItem("MBR")
        self.combo_partition.currentTextChanged.connect(self.update_partition_scheme)

        lbl_target = QLabel("Target system")
        lbl_target.setStyleSheet("font-weight: normal; font-size: 9pt;")
        self.combo_target = QComboBox()
        self.combo_target.addItem("UEFI (non CSM)")
        self.combo_target.addItem("BIOS (or UEFI-CSM)")
        self.combo_target.currentTextChanged.connect(self.update_target_system)

        grid_part.addWidget(lbl_part, 0, 0)
        grid_part.addWidget(self.combo_partition, 1, 0)
        grid_part.addWidget(lbl_target, 0, 2)
        grid_part.addWidget(self.combo_target, 1, 2)
        
        main_layout.addLayout(grid_part)
        
        main_layout.addSpacing(15)

        main_layout.addLayout(self.create_header("Format Options"))

        lbl_vol = QLabel("Volume label")
        lbl_vol.setStyleSheet("font-weight: normal; font-size: 9pt; padding-bottom: 2px;")
        self.input_label = QLineEdit("Volume label")
        self.input_label.textChanged.connect(self.update_new_label)
        
        vol_layout = QVBoxLayout()
        vol_layout.setSpacing(2)
        vol_layout.addWidget(lbl_vol)
        vol_layout.addWidget(self.input_label)
        main_layout.addLayout(vol_layout)

        grid_fmt = QGridLayout()
        grid_fmt.setSpacing(10)
        grid_fmt.setColumnStretch(1, 1)
        grid_fmt.setColumnStretch(3, 1)
        
        lbl_fs = QLabel("File system")
        lbl_fs.setStyleSheet("font-weight: normal; font-size: 9pt;")
        self.combo_fs = QComboBox()
        self.all_fs_options = ["NTFS", "FAT32", "exFAT", "ext4", "UDF"]
        self.combo_fs.addItems(self.all_fs_options)
        self.combo_fs.currentTextChanged.connect(self.updateFS)
        
        lbl_cluster = QLabel("Cluster size")
        lbl_cluster.setStyleSheet("font-weight: normal; font-size: 9pt;")
        self.combo_cluster = QComboBox()
        self.combo_cluster.addItem("4096 bytes (Default)")
        self.combo_cluster.addItem("8192 bytes")
        self.combo_cluster.currentTextChanged.connect(self.update_cluster_size)
        grid_fmt.addWidget(lbl_fs, 0, 0)
        grid_fmt.addWidget(self.combo_fs, 1, 0)
        grid_fmt.addWidget(lbl_cluster, 0, 2)
        grid_fmt.addWidget(self.combo_cluster, 1, 2)
        
        main_layout.addLayout(grid_fmt)

        self.chk_quick = QCheckBox("Quick format")
        self.chk_quick.setChecked(True)
        self.chk_quick.stateChanged.connect(self.update_QF)

        self.chk_extended = QCheckBox("Create extended label and icon files")
        self.chk_extended.setChecked(True)
        self.chk_extended.stateChanged.connect(self.update_create_extended)
        
        bad_blocks_row = QHBoxLayout()
        self.chk_badblocks = QCheckBox("Check device for bad blocks")
        self.combo_badblocks = QComboBox()
        self.combo_badblocks.addItem("1 pass")
        self.combo_badblocks.setFixedWidth(100)
        self.combo_badblocks.setEnabled(False)
        self.chk_badblocks.stateChanged.connect(self.update_check_bad)
        
        bad_blocks_row.addWidget(self.chk_badblocks)
        bad_blocks_row.addWidget(self.combo_badblocks)
        bad_blocks_row.addStretch()

        chk_layout = QVBoxLayout()
        chk_layout.setSpacing(5)
        chk_layout.addWidget(self.chk_quick)
        chk_layout.addWidget(self.chk_extended)
        chk_layout.addLayout(bad_blocks_row)
        
        main_layout.addLayout(chk_layout)
        
        main_layout.addSpacing(15)

        main_layout.addLayout(self.create_header("Status"))

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("")
        main_layout.addWidget(self.progress_bar)

        bottom_controls = QHBoxLayout()
        bottom_controls.setSpacing(10)
        bottom_controls.setContentsMargins(0, 10, 0, 0)
        
        icons_layout = QHBoxLayout()
        icons_layout.setSpacing(5)
        
        btn_icon1 = QToolButton()
        btn_icon1.setText("🌐")
        btn_icon1.setToolTip("Download updates")
        btn_icon1.clicked.connect(lambda: webbrowser.open('http://www.github.com/hog185/rufus-py'))

        
        btn_icon2 = QToolButton()
        btn_icon2.setText("ℹ")
        btn_icon2.setToolTip("About")
        btn_icon2.clicked.connect(self.show_about)
        
        btn_icon3 = QToolButton()
        btn_icon3.setText("⚙")
        btn_icon3.setToolTip("Settings")
        
        btn_icon4 = QToolButton()
        btn_icon4.setText("📄")
        btn_icon4.setToolTip("Log")
        btn_icon4.clicked.connect(self.show_log)
        
        icons_layout.addWidget(btn_icon1)
        icons_layout.addWidget(btn_icon2)
        icons_layout.addWidget(btn_icon3)
        icons_layout.addWidget(btn_icon4)
        icons_layout.addStretch()
        
        self.btn_start = QPushButton("START")
        self.btn_start.setObjectName("btnStart")
        self.btn_start.setFixedSize(100, 50)
        self.btn_start.clicked.connect(self.start_process)

        self.btn_cancel = QPushButton("CANCEL")
        self.btn_cancel.setFixedSize(100, 50)
        self.btn_cancel.clicked.connect(self.cancel_process)
        
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_cancel)
        
        bottom_controls.addLayout(icons_layout, 1)
        bottom_controls.addLayout(btn_layout)
        
        main_layout.addLayout(bottom_controls)
        
        main_layout.addStretch()

        central_widget.setLayout(main_layout)
        
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("", 0)



    def updateFS(self):
        states.currentFS = self.combo_fs.currentIndex()
        # print(f"Global state updated to: {states.currentFS}")
    
    def update_image_option(self):
        states.image_option = self.combo_image_option.currentIndex()
        # print(f"Global state updated to: {states.image_option}")
        self._update_filesystem_options()
    
    def _update_filesystem_options(self):
        states.image_option = self.combo_image_option.currentText()
        self.combo_fs.blockSignals(True)
        if states.image_option == "Standard Linux":
            self.combo_fs.clear()
            self.combo_fs.addItem("UDF")
            # print("UDF Only")
        else:
            self.combo_fs.clear()
            self.combo_fs.addItems(self.all_fs_options)
            self.combo_fs.setCurrentText("NTFS")
            # print("windows options")
        self.combo_fs.blockSignals(False)
        self.updateFS()

    def update_partition_scheme(self):
        states.partition_scheme = self.combo_partition.currentIndex()
        # print(f"Global state updated to: {states.partition_scheme}")

    def update_target_system(self):
        states.target_system = self.combo_target.currentIndex()
        # print(f"Global state updated to: {states.target_system}")
    
    def update_new_label(self, current_text):
        states.new_label = current_text
        # print(f"Stored in state: {states.new_label}")
    
    def update_cluster_size(self):
        states.cluster_size = self.combo_cluster.currentIndex()
        print(f"Global state updated to: {states.cluster_size}")

    def update_QF(self):
        if self.chk_quick.isChecked():
            states.QF = 0
            # print(states.QF)
        else:
            states.QF = 1
            # print(states.QF)

    def update_create_extended(self):
        if self.chk_extended.isChecked():
            states.create_extended = 0
            # print(states.create_extended)
        else:
            states.create_extended = 1
            # print(states.create_extended)

    def update_check_bad(self):
        if self.chk_badblocks.isChecked():
            states.check_bad = 0
            # print(states.check_bad)
        else:
            states.check_bad = 1
            # print(states.check_bad)

    def browse_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select Disk Image", "", "ISO Images (*.iso);;All Files (*)")
        if file_name:
            states.iso_path=file_name

            clean_name = file_name.split("/")[-1].split("\\")[-1]
            self.combo_boot.setItemText(0, clean_name)
            self.input_label.setText(clean_name.split('.')[0].upper())

            self.log_message(f"Selected image: {file_name}")
            # print(f"iso path:{states.iso_path}")

    def show_log(self):
        self.log_window = LogWindow()
        self.log_window.show()

    def show_about(self):
        self.about_window = AboutWindow()
        about_content = "filler text, change this later"
        self.about_window.about_text.setPlainText(about_content)
        self.about_window.show()

    def log_message(self, msg):
        if hasattr(self, 'log_window'):
            self.log_window.log_text.append(f"[INFO] {msg}")

    def about_message(self, msg):
        if hasattr(self, 'about_window'):
            self.about_window.about_text.append(f"Rufus-Py is a disk image writer written in py for linux")

    def ready(self):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("READY FOR ACTION")
    
    def get_selected_mount_path(self) -> str:
        text = self.combo_device.currentText()
        if '(' in text and ')' in text:
            return text.split('(')[1].split(')')[0].strip()
        return ""
    
    def cancel_process(self):
        reply = QMessageBox.question(self, "Cancel", "Are you sure you want to cancel?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            if hasattr(self, 'flash_worker') and self.flash_worker.isRunning():
                self.flash_worker.terminate()
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
            self.btn_start.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.statusBar.showMessage("Ready", 0)
    
    def on_flash_finished(self, success: bool):
        #yayyyyyy
        if success:
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("Complete! 100%")
            QMessageBox.information(self, "Success", "USB drive flashed successfully!")
        else:
            self.progress_bar.setFormat("Failed")
            QMessageBox.critical(self, "Error", "Failed to flash USB drive.") #uh oh
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.statusBar.showMessage("Ready", 0)

    
    def start_process(self):
        #error handling woah so cool im such a good programmer, this will surely never fail
        if not getattr(states, 'iso_path', '') or not Path(states.iso_path).exists():
            QMessageBox.warning(self, "No Image", "Please select a valid installation file first.")
            return
        mount_path = self.get_selected_mount_path()
        if not mount_path:
            QMessageBox.warning(self, "No Device", "Please select a USB device first.")
            return
        
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Preparing...")
        self.statusBar.showMessage("Flashing...", 0)
        #progress bar:3c
        self.flash_worker = FlashWorker(states.iso_path, mount_path)
        self.flash_worker.progress.connect(lambda msg: self.statusBar.showMessage(msg, 0))
        # self.flash_worker.finished.connect(self.on_flash_finished)
        self.flash_worker.start()
        

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    
    # Parse USB devices from command line argument
    usb_devices = {}
    if len(sys.argv) > 1:
        try:
            # Decode the URL-encoded JSON data
            decoded_data = urllib.parse.unquote(sys.argv[1])
            usb_devices = json.loads(decoded_data)
            print("Successfully parsed USB devices:", usb_devices)
        except Exception as e:
            print(f"Error parsing USB devices: {e}")
            usb_devices = {}
    else:
        print("No USB devices data received")
    
    window = Rufus(usb_devices)
    window.show()
    sys.exit(app.exec())
