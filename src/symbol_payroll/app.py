"""Точка входа настольного приложения."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Символ — расчёт заработной платы")
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        label = QLabel(
            "Символ\n\nНачальная версия проекта.\n"
            "Перед расчётом необходимо настроить магазины, сотрудников и правила оплаты."
        )
        label.setStyleSheet("QLabel { padding: 32px; font-size: 18px; }")
        self.setCentralWidget(label)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Символ")
    window = MainWindow()
    window.show()
    return app.exec()
