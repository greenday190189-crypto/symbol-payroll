"""Графический интерфейс приложения «Символ»."""

from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.rules import RuleScope
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.payroll_repository import (
    list_rule_versions, save_rule_version,
)
from symbol_payroll.infrastructure.repositories import (
    create_employee, create_store, list_employees, list_stores,
)


class StoreDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить магазин")
        self.name = QLineEdit()
        form = QFormLayout(self)
        form.addRow("Название магазина", self.name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class EmployeeDialog(QDialog):
    def __init__(self, stores, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить сотрудника")
        self.store = QComboBox()
        for store in stores:
            self.store.addItem(store.name, store.id)
        self.full_name = QLineEdit()
        self.position = QLineEdit()
        self.salary = QLineEdit()
        self.basis = QComboBox()
        self.basis.addItem("Не определено", SalaryBasis.UNCONFIRMED.value)
        self.basis.addItem("До НДФЛ (gross)", SalaryBasis.GROSS.value)
        self.basis.addItem("На руки (net)", SalaryBasis.NET.value)
        form = QFormLayout(self)
        form.addRow("Магазин", self.store)
        form.addRow("ФИО", self.full_name)
        form.addRow("Должность", self.position)
        form.addRow("Оклад, руб. (необязательно)", self.salary)
        form.addRow("Как трактовать оклад", self.basis)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class RuleDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить версию расчётного правила")
        self.code = QLineEdit()
        self.scope = QComboBox()
        for label, value in (
            ("Вся организация", RuleScope.ORGANIZATION.value),
            ("Магазин", RuleScope.STORE.value),
            ("Должность", RuleScope.POSITION.value),
            ("Сотрудник", RuleScope.EMPLOYEE.value),
            ("Исключение на период", RuleScope.PERIOD.value),
        ):
            self.scope.addItem(label, value)
        self.scope_id = QLineEdit("*")
        self.expression = QLineEdit()
        self.expression.setPlaceholderText("Например: salary / 22 * shifts")
        self.start = QDateEdit(QDate.currentDate())
        self.start.setCalendarPopup(True)
        self.end_enabled = QCheckBox("Ограничить дату окончания")
        self.end = QDateEdit(QDate.currentDate())
        self.end.setCalendarPopup(True)
        self.end.setEnabled(False)
        self.end_enabled.toggled.connect(self.end.setEnabled)

        form = QFormLayout(self)
        form.addRow("Код показателя", self.code)
        form.addRow("Область действия", self.scope)
        form.addRow("Идентификатор области", self.scope_id)
        form.addRow("Формула", self.expression)
        form.addRow("Действует с", self.start)
        form.addRow(self.end_enabled)
        form.addRow("Действует по", self.end)
        hint = QLabel(
            "Идентификатор: * для организации; ID магазина/сотрудника или точное "
            "название должности. Исключение на период: ID периода/сотрудника."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        end_date = self.end.date().toPython() if self.end_enabled.isChecked() else None
        return (
            self.code.text().strip(),
            RuleScope(self.scope.currentData()),
            self.scope_id.text().strip(),
            self.expression.text().strip(),
            self.start.date().toPython(),
            end_date,
        )


class DirectoryPage(QWidget):
    def __init__(self, connection, employees: bool) -> None:
        super().__init__()
        self.connection = connection
        self.is_employees = employees
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.add_button = QPushButton("Добавить сотрудника" if employees else "Добавить магазин")
        self.add_button.clicked.connect(self.add_record)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.add_button)
        self.refresh()

    def refresh(self) -> None:
        if self.is_employees:
            records = list_employees(self.connection, include_inactive=True)
            headers = ["ФИО", "Магазин", "Должность", "Оклад", "Тип оклада", "Активен"]
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            stores = {store.id: store.name for store in list_stores(self.connection, True)}
            self.table.setRowCount(len(records))
            for row, item in enumerate(records):
                values = [
                    item.full_name, stores.get(item.store_id, item.store_id),
                    item.position or "—",
                    str(item.salary_amount) if item.salary_amount is not None else "—",
                    item.salary_basis.value, "Да" if item.active else "Нет",
                ]
                for col, value in enumerate(values):
                    self.table.setItem(row, col, QTableWidgetItem(value))
        else:
            records = list_stores(self.connection, include_inactive=True)
            headers = ["Название", "Статус"]
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(records))
            for row, item in enumerate(records):
                self.table.setItem(row, 0, QTableWidgetItem(item.name))
                self.table.setItem(row, 1, QTableWidgetItem("Активен" if item.active else "Неактивен"))
        self.table.resizeColumnsToContents()

    def add_record(self) -> None:
        try:
            if self.is_employees:
                stores = list_stores(self.connection)
                if not stores:
                    QMessageBox.information(self, "Нет магазинов", "Сначала добавьте магазин.")
                    return
                dialog = EmployeeDialog(stores, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                raw_salary = dialog.salary.text().strip().replace(" ", "").replace(",", ".")
                salary = Decimal(raw_salary) if raw_salary else None
                create_employee(
                    self.connection, str(uuid.uuid4()), dialog.store.currentData(),
                    dialog.full_name.text(), dialog.position.text(), salary,
                    SalaryBasis(dialog.basis.currentData()),
                )
            else:
                dialog = StoreDialog(self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                create_store(self.connection, str(uuid.uuid4()), dialog.name.text())
            self.refresh()
        except (ValueError, ArithmeticError) as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))


class RulesPage(QWidget):
    def __init__(self, connection) -> None:
        super().__init__()
        self.connection = connection
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.add_button = QPushButton("Добавить версию правила")
        self.add_button.clicked.connect(self.add_rule)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.add_button)
        self.refresh()

    def refresh(self) -> None:
        rules = list_rule_versions(self.connection)
        headers = ["Показатель", "Область", "ID области", "Формула", "Версия", "С", "По"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            values = [
                rule.code, rule.scope.value, rule.scope_id, rule.expression,
                str(rule.version), rule.effective_from.isoformat(),
                rule.effective_to.isoformat() if rule.effective_to else "Без срока",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def add_rule(self) -> None:
        dialog = RuleDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            code, scope, scope_id, expression, start, end = dialog.values()
            save_rule_version(
                self.connection, code, scope, scope_id, expression, start, end
            )
            self.refresh()
        except (ValueError, ArithmeticError) as exc:
            QMessageBox.warning(self, "Проверьте правило", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))


class MainWindow(QMainWindow):
    def __init__(self, connection) -> None:
        super().__init__()
        self.setWindowTitle("Символ — расчёт заработной платы")
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        tabs = QTabWidget()
        tabs.addTab(DirectoryPage(connection, employees=False), "Магазины")
        tabs.addTab(DirectoryPage(connection, employees=True), "Сотрудники")
        tabs.addTab(RulesPage(connection), "Правила оплаты")
        placeholder = QLabel(
            "Расчётные ведомости будут подключены на следующем этапе.\n"
            "Пока не настроены и не проверены правила, приложение не утверждает расчёты."
        )
        placeholder.setWordWrap(True)
        placeholder.setStyleSheet("padding: 28px; font-size: 16px;")
        tabs.addTab(placeholder, "Расчёты")
        self.setCentralWidget(tabs)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Символ")
    data_dir = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    ))
    connection = connect(data_dir / "symbol_payroll.sqlite3")
    initialize(connection)
    window = MainWindow(connection)
    window.show()
    result = app.exec()
    connection.close()
    return result
