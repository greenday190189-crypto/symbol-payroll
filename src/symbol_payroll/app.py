"""Графический интерфейс приложения «Символ»."""

from __future__ import annotations

import calendar
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.rules import RuleScope
from symbol_payroll.domain.tax import TaxMode
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.period_inputs import (
    PeriodEmployeeInput, list_period_inputs, save_period_input,
)
from symbol_payroll.infrastructure.payroll_repository import (
    create_period, list_period_snapshots, list_periods, list_rule_versions,
    save_rule_version, transition_period,
)
from symbol_payroll.services.payroll_service import calculate_period
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
        self.title = QLineEdit()
        self.title.setPlaceholderText("Например: Оплата смен")
        self.category = QComboBox()
        for label, value in (
            ("Начисление", "earning"), ("Премия", "bonus"),
            ("Сумма на руки", "net_salary"), ("Удержание", "deduction"),
            ("Не классифицировано", "unclassified"),
        ):
            self.category.addItem(label, value)
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
        form.addRow("Название строки", self.title)
        form.addRow("Категория", self.category)
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
            self.category.currentData(),
            self.title.text().strip(),
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
        headers = ["Показатель", "Название", "Категория", "Область", "ID области", "Формула", "Версия", "С", "По"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            values = [
                rule.code, rule.title, rule.category, rule.scope.value,
                rule.scope_id, rule.expression, str(rule.version), rule.effective_from.isoformat(),
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
            code, scope, scope_id, expression, start, end, category, title = dialog.values()
            save_rule_version(
                self.connection, code, scope, scope_id, expression, start, end,
                category=category, title=title,
            )
            self.refresh()
        except (ValueError, ArithmeticError) as exc:
            QMessageBox.warning(self, "Проверьте правило", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))


class CalculationsPage(QWidget):
    INPUT_COLUMNS = (
        ("Смены", "shifts"), ("Часы", "hours"), ("Продажи", "sales"),
        ("План", "plan"), ("Аванс", "advances"),
        ("Удержания", "deductions"), ("НДФЛ", "manual_tax"),
        ("Корректировка", "fixed_adjustments"),
    )

    def __init__(self, connection) -> None:
        super().__init__()
        self.connection = connection
        self.period = QComboBox()
        self.period.currentIndexChanged.connect(self.load_period)
        self.month = QDateEdit(QDate.currentDate())
        self.month.setDisplayFormat("MM.yyyy")
        self.month.setCalendarPopup(True)
        self.create_button = QPushButton("Создать месяц")
        self.create_button.clicked.connect(self.create_month)
        self.save_button = QPushButton("Сохранить вводные")
        self.save_button.clicked.connect(self.save_inputs)
        self.tax_mode = QComboBox()
        self.tax_mode.addItem("НДФЛ не рассчитывать", TaxMode.DISABLED.value)
        self.tax_mode.addItem("НДФЛ введён вручную", TaxMode.MANUAL.value)
        self.tax_mode.addItem("НДФЛ импортирован", TaxMode.IMPORTED.value)
        self.tax_mode.addItem("Автоматический НДФЛ (нужен налоговый профиль)", TaxMode.AUTOMATIC.value)
        self.tax_mode.model().item(3).setEnabled(False)
        self.tax_mode.setToolTip(
            "Автоматический расчёт НДФЛ пока не подключён: требуется подтверждённый налоговый профиль."
        )
        self.calculate_button = QPushButton("Рассчитать ведомость")
        self.calculate_button.clicked.connect(self.run_calculation)
        self.approve_button = QPushButton("Утвердить ведомость")
        self.approve_button.clicked.connect(self.approve_period)
        self.status = QLabel("Создайте расчётный месяц.")
        self.status.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("Период:"))
        top.addWidget(self.period, 1)
        top.addWidget(self.month)
        top.addWidget(self.create_button)
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(QLabel("Режим НДФЛ:"))
        actions.addWidget(self.tax_mode)
        actions.addWidget(self.calculate_button)
        actions.addWidget(self.approve_button)

        self.input_table = QTableWidget()
        self.input_table.setAlternatingRowColors(True)
        self.input_table.setColumnCount(1 + len(self.INPUT_COLUMNS))
        self.input_table.setHorizontalHeaderLabels(
            ["Сотрудник", *(label for label, _ in self.INPUT_COLUMNS)]
        )
        self.input_table.setEditTriggers(QTableWidget.EditTrigger.AllEditTriggers)
        self.input_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self.result_table = QTableWidget()
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setColumnCount(6)
        self.result_table.setHorizontalHeaderLabels(
            ["Сотрудник", "Начислено gross", "НДФЛ", "Удержания", "К выплате", "Расшифровка"]
        )

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Вводные показатели за месяц"))
        layout.addWidget(self.input_table, 3)
        layout.addLayout(actions)
        layout.addWidget(QLabel("Результат расчёта"))
        layout.addWidget(self.result_table, 3)
        self.refresh_periods()

    def refresh_periods(self, selected_id: str | None = None) -> None:
        self.period.blockSignals(True)
        self.period.clear()
        for item in list_periods(self.connection):
            self.period.addItem(f"{item.id} — {item.status}", item.id)
        index = self.period.findData(selected_id) if selected_id else 0
        if index >= 0 and self.period.count():
            self.period.setCurrentIndex(index)
        self.period.blockSignals(False)
        self.load_period()

    def create_month(self) -> None:
        selected = self.month.date()
        start = date(selected.year(), selected.month(), 1)
        end = date(selected.year(), selected.month(),
                   calendar.monthrange(selected.year(), selected.month())[1])
        period_id = start.strftime("%Y-%m")
        try:
            existing = self.connection.execute(
                "SELECT id FROM payroll_periods WHERE id = ?", (period_id,)
            ).fetchone()
            if existing is None:
                create_period(self.connection, period_id, start, end)
            self.refresh_periods(period_id)
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось создать период", str(exc))

    def load_period(self, *_args) -> None:
        period_id = self.period.currentData()
        self.input_table.setRowCount(0)
        self.result_table.setRowCount(0)
        if not period_id:
            self.status.setText("Создайте расчётный месяц.")
            self.set_editable(False)
            return
        period = self.connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
        ).fetchone()
        if period is None:
            return
        editable = period["status"] in ("draft", "calculated")
        self.set_editable(editable)
        employees = list_employees(self.connection)
        inputs = list_period_inputs(self.connection, period_id)
        self.input_table.setRowCount(len(employees))
        for row, employee in enumerate(employees):
            name = QTableWidgetItem(employee.full_name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setData(Qt.ItemDataRole.UserRole, employee.id)
            self.input_table.setItem(row, 0, name)
            item = inputs.get(employee.id, PeriodEmployeeInput(period_id, employee.id))
            for column, (_, field_name) in enumerate(self.INPUT_COLUMNS, start=1):
                self.input_table.setItem(
                    row, column, QTableWidgetItem(str(getattr(item, field_name)))
                )
        self.input_table.resizeColumnsToContents()
        self.load_results(period_id)
        self.status.setText(
            f"Период {period_id}. Статус: {period['status']}. "
            "Для gross/net используются только подтверждённые настройки оклада."
        )

    def set_editable(self, editable: bool) -> None:
        self.input_table.setEditTriggers(
            QTableWidget.EditTrigger.AllEditTriggers if editable
            else QTableWidget.EditTrigger.NoEditTriggers
        )
        self.save_button.setEnabled(editable)
        self.calculate_button.setEnabled(editable)
        self.approve_button.setEnabled(False)

    def _read_inputs(self) -> list[PeriodEmployeeInput]:
        period_id = self.period.currentData()
        if not period_id:
            raise ValueError("Сначала создайте или выберите расчётный месяц")
        result = []
        for row in range(self.input_table.rowCount()):
            name_item = self.input_table.item(row, 0)
            employee_id = name_item.data(Qt.ItemDataRole.UserRole)
            values = {}
            for column, (_, field_name) in enumerate(self.INPUT_COLUMNS, start=1):
                item = self.input_table.item(row, column)
                raw = item.text().strip() if item else "0"
                values[field_name] = Decimal(raw.replace(" ", "").replace(",", ".") or "0")
            result.append(PeriodEmployeeInput(
                period_id, employee_id,
                shifts=values["shifts"], hours=values["hours"],
                advances=values["advances"], sales=values["sales"],
                plan=values["plan"], deductions=values["deductions"],
                manual_tax=values["manual_tax"],
                fixed_adjustments=values["fixed_adjustments"],
            ))
        return result

    def save_inputs(self) -> None:
        try:
            for item in self._read_inputs():
                save_period_input(self.connection, item)
            self.status.setText("Вводные показатели сохранены.")
        except (ValueError, ArithmeticError, LookupError) as exc:
            QMessageBox.warning(self, "Проверьте вводные данные", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))

    def run_calculation(self) -> None:
        period_id = self.period.currentData()
        try:
            for item in self._read_inputs():
                save_period_input(self.connection, item)
            mode = TaxMode(self.tax_mode.currentData())
            result = calculate_period(self.connection, period_id, mode)
            self.render_result(result)
            self.refresh_periods(period_id)
            warnings = "\n".join(result.warnings)
            self.status.setText(
                f"Расчёт сохранён. Сотрудников: {len(result.employees)}. "
                f"Начислено: {result.total_gross:.2f} руб.; "
                f"удержания: {result.total_deductions:.2f} руб.; "
                f"к выплате: {result.total_net:.2f} руб."
                + (f"\nПредупреждения:\n{warnings}" if warnings else "")
            )
            self.approve_button.setEnabled(True)
        except (ValueError, ArithmeticError, LookupError) as exc:
            QMessageBox.warning(self, "Расчёт не выполнен", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка расчёта", str(exc))

    def render_result(self, result) -> None:
        self.result_table.setRowCount(len(result.employees))
        for row, employee in enumerate(result.employees):
            breakdown = "; ".join(
                f"{line.title}: {line.amount:.2f}"
                for line in employee.components
            )
            values = [
                employee.full_name, f"{employee.gross_total:.2f}",
                f"{employee.tax_amount:.2f}", f"{employee.deduction_total:.2f}",
                f"{employee.net_total:.2f}", breakdown,
            ]
            for column, value in enumerate(values):
                self.result_table.setItem(row, column, QTableWidgetItem(value))
        self.result_table.resizeColumnsToContents()

    def load_results(self, period_id: str) -> None:
        snapshots = list_period_snapshots(self.connection, period_id)
        self.result_table.setRowCount(len(snapshots))
        for row, entry in enumerate(snapshots):
            data = entry["snapshot"]
            components = data.get("components", [])
            breakdown = "; ".join(
                f"{line['title']}: {Decimal(line['amount']):.2f}"
                for line in components
            )
            values = [
                data.get("full_name", entry["employee_id"]),
                data.get("gross_total", "0"), data.get("tax_amount", "0"),
                data.get("deduction_total", "0"), data.get("net_total", "0"),
                breakdown,
            ]
            for column, value in enumerate(values):
                self.result_table.setItem(row, column, QTableWidgetItem(value))
        self.result_table.resizeColumnsToContents()
        status = self.connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
        ).fetchone()["status"]
        self.approve_button.setEnabled(status == "calculated" and bool(snapshots))

    def approve_period(self) -> None:
        period_id = self.period.currentData()
        if not period_id:
            return
        answer = QMessageBox.question(
            self, "Утверждение ведомости",
            "Утвердить расчёт? После утверждения вводные данные и снимки будут заблокированы.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            transition_period(self.connection, period_id, "approved")
            self.refresh_periods(period_id)
            self.status.setText(
                f"Период {period_id} утверждён. Его расчётные снимки защищены от изменений."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось утвердить ведомость", str(exc))


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
        tabs.addTab(CalculationsPage(connection), "Расчёты")
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
