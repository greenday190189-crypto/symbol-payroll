"""Графический интерфейс приложения «Символ»."""

from __future__ import annotations

import calendar
import json
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QPlainTextEdit, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.rules import RuleScope
from symbol_payroll.domain.tax import TaxMode
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.legacy_import import import_legacy_directory
from symbol_payroll.legacy_directory_data import LEGACY_DIRECTORY
from symbol_payroll.infrastructure.backup import (
    create_backup, create_daily_backup, default_backup_name, restore_backup, validate_backup,
)
from symbol_payroll.infrastructure.excel_import import (
    FIELD_LABELS, ColumnMapping, apply_import_preview, build_import_preview,
    list_workbook_sheets, read_headers, suggest_mapping,
)
from symbol_payroll.infrastructure.period_inputs import (
    PeriodEmployeeInput, list_period_inputs, save_period_input,
)
from symbol_payroll.infrastructure.payroll_export import export_period_workbook
from symbol_payroll.infrastructure.termination_export import export_termination_workbook
from symbol_payroll.infrastructure.payroll_repository import (
    create_period, list_period_snapshots, list_periods, list_rule_versions,
    save_rule_version, transition_period,
)
from symbol_payroll.services.payroll_service import calculate_period
from symbol_payroll.services.termination_service import (
    approve_termination_case, calculate_termination_case,
    create_termination_case, list_termination_cases,
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
        self.expression = QPlainTextEdit()
        self.expression.setPlaceholderText("Например: salary / 22 * shifts")
        self.expression.setMinimumHeight(76)
        self.expression.setToolTip("Формула определяет сумму начисления или удержания. Используйте только переменные из справки ниже.")
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
        formula_help = QLabel(
            "ДОСТУПНЫЕ ПЕРЕМЕННЫЕ\n"
            "salary — оклад до НДФЛ (gross); salary_net — оклад на руки (net);\n"
            "shifts — смены; hours — часы; sales — продажи; plan — план;\n"
            "advances — аванс; deductions — прочие удержания; manual_tax — ручной НДФЛ;\n"
            "fixed_adjustments — корректировки. Можно добавлять свои числовые параметры\n"
            "в таблице расчёта → «Доп. параметры» (например, commission_rate).\n\n"
            "СИНТАКСИС: числа, имена переменных, скобки и операции +, -, *, /.\n"
            "Функции, проценты как знак %, степени и Python-код не поддерживаются.\n\n"
            "ПРИМЕРЫ:\n"
            "salary / 22 * shifts — оплата пропорционально сменам при делителе 22;\n"
            "sales * commission_rate — процент от продаж (ставка задаётся отдельно);\n"
            "sales * 0.03 — 3% от продаж; 0.03 — это 3%, знак процента не пишется."
        )
        formula_help.setWordWrap(True)
        formula_help.setStyleSheet("QLabel { color: #526174; background: #f1f5f9; padding: 10px; border-radius: 8px; }")
        form.addRow("Как писать формулу", formula_help)
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
            self.expression.toPlainText().strip(),
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


class ExcelImportDialog(QDialog):
    def __init__(self, connection, period_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.period_id = period_id
        self.preview = None
        self.setWindowTitle(f"Импорт Excel — {period_id}")
        self.resize(980, 680)

        self.file_path = QLineEdit()
        self.file_path.setReadOnly(True)
        self.browse_button = QPushButton("Выбрать файл…")
        self.browse_button.clicked.connect(self.choose_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self.file_path, 1)
        file_row.addWidget(self.browse_button)

        self.sheet = QComboBox()
        self.sheet.currentTextChanged.connect(self.load_headers)
        self.store = QComboBox()
        for store in list_stores(connection):
            self.store.addItem(store.name, store.id)
        self.header_row = QSpinBox()
        self.header_row.setRange(1, 1000)
        self.header_row.setValue(1)
        self.header_row.valueChanged.connect(self.load_headers)

        form = QFormLayout()
        form.addRow("Лист Excel", self.sheet)
        form.addRow("Магазин для этого листа", self.store)
        form.addRow("Строка заголовков", self.header_row)

        self.mapping_controls: dict[str, QComboBox] = {}
        mapping_form = QFormLayout()
        for field, label in FIELD_LABELS.items():
            control = QComboBox()
            control.addItem("Не импортировать", None)
            self.mapping_controls[field] = control
            mapping_form.addRow(label, control)

        self.preview_button = QPushButton("Проверить и показать предпросмотр")
        self.preview_button.clicked.connect(self.make_preview)
        self.import_button = QPushButton("Импортировать проверенные данные")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.apply_import)
        self.status = QLabel(
            "Выберите файл, лист и магазин. Не сопоставленные числовые колонки сохранят "
            "текущие значения сотрудника; отсутствующие в файле сотрудники не изменяются. "
            "Данные не будут записаны до проверки и подтверждения."
        )
        self.status.setWordWrap(True)

        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(7)
        self.preview_table.setHorizontalHeaderLabels(
            ["Строка Excel", "Сотрудник", "Смены", "Часы", "Продажи", "Аванс", "Проверка"]
        )
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.setAlternatingRowColors(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)
        cancel = QPushButton("Закрыть")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        layout = QVBoxLayout(self)
        layout.addLayout(file_row)
        layout.addLayout(form)
        layout.addWidget(QLabel("Сопоставление колонок"))
        layout.addLayout(mapping_form)
        layout.addWidget(self.status)
        layout.addWidget(self.preview_table, 1)
        layout.addLayout(buttons)

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите расчётную ведомость", "",
            "Excel (*.xlsx *.xlsm);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            sheets = list_workbook_sheets(path)
            self.file_path.setText(path)
            self.sheet.blockSignals(True)
            self.sheet.clear()
            self.sheet.addItems(sheets)
            self.sheet.blockSignals(False)
            self.preview = None
            self.preview_table.setRowCount(0)
            self.import_button.setEnabled(False)
            self.load_headers()
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось открыть Excel", str(exc))

    def load_headers(self, *_args) -> None:
        path = self.file_path.text()
        sheet_name = self.sheet.currentText()
        if not path or not sheet_name:
            return
        try:
            headers = read_headers(path, sheet_name, self.header_row.value())
            suggestion = suggest_mapping(headers)
            for field, control in self.mapping_controls.items():
                control.blockSignals(True)
                control.clear()
                control.addItem("Не импортировать", None)
                for index, header in enumerate(headers):
                    label = header or f"Колонка {index + 1}"
                    control.addItem(f"{label} (столбец {index + 1})", index)
                selected = getattr(suggestion, field)
                found = control.findData(selected) if selected is not None else 0
                control.setCurrentIndex(max(found, 0))
                control.blockSignals(False)
            self.preview = None
            self.import_button.setEnabled(False)
            self.status.setText(
                f"Заголовки прочитаны: {len(headers)} колонок. "
                "Проверьте автоматическое сопоставление и запустите предпросмотр."
            )
        except Exception as exc:
            self.status.setText(f"Не удалось прочитать заголовки: {exc}")

    def make_preview(self) -> None:
        try:
            if not self.file_path.text():
                raise ValueError("Сначала выберите Excel-файл")
            mapping = ColumnMapping(**{
                field: control.currentData()
                for field, control in self.mapping_controls.items()
            })
            self.preview = build_import_preview(
                self.connection, self.file_path.text(), self.sheet.currentText(),
                self.store.currentData(), self.header_row.value(), mapping,
            )
            self.preview_table.setRowCount(len(self.preview.rows))
            for row_index, row in enumerate(self.preview.rows):
                values = [
                    str(row.source_row), row.employee_name,
                    str(row.values.get("shifts")) if row.values.get("shifts") is not None else "—",
                    str(row.values.get("hours")) if row.values.get("hours") is not None else "—",
                    str(row.values.get("sales")) if row.values.get("sales") is not None else "—",
                    str(row.values.get("advances")) if row.values.get("advances") is not None else "—",
                    "ОШИБКА: " + "; ".join(row.errors) if row.errors else "Готово",
                ]
                for column, value in enumerate(values):
                    self.preview_table.setItem(
                        row_index, column, QTableWidgetItem(value)
                    )
            self.preview_table.resizeColumnsToContents()
            if self.preview.errors:
                self.status.setText(
                    f"Проверено строк: {len(self.preview.rows)}. "
                    f"Допущено: {self.preview.valid_count}. "
                    f"Ошибок: {len(self.preview.errors)}. "
                    "Импорт заблокирован до исправления всех ошибок."
                )
                self.import_button.setEnabled(False)
            else:
                self.status.setText(
                    f"Предпросмотр готов: {self.preview.valid_count} строк без ошибок. "
                    "Проверьте данные и нажмите «Импортировать»."
                )
                self.import_button.setEnabled(self.preview.valid_count > 0)
        except Exception as exc:
            self.preview = None
            self.import_button.setEnabled(False)
            QMessageBox.warning(self, "Предпросмотр не выполнен", str(exc))

    def apply_import(self) -> None:
        if self.preview is None:
            return
        try:
            count = apply_import_preview(self.connection, self.period_id, self.preview)
            QMessageBox.information(
                self, "Импорт завершён",
                f"Импортировано строк: {count}. Проверьте показатели в таблице расчёта.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Импорт отменён", str(exc))


class CalculationsPage(QWidget):
    INPUT_COLUMNS = (
        ("Смены", "shifts"), ("Часы", "hours"), ("Продажи", "sales"),
        ("План", "plan"), ("Аванс", "advances"),
        ("Удержания", "deductions"), ("НДФЛ", "manual_tax"),
        ("Корректировка", "fixed_adjustments"),
        ("Доп. параметры (JSON)", "extra_values_json"),
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
        self.import_button = QPushButton("Импорт Excel")
        self.import_button.clicked.connect(self.import_excel)
        self.export_button = QPushButton("Экспорт Excel")
        self.export_button.clicked.connect(self.export_excel)
        self.export_button.setEnabled(False)
        self.tax_mode = QComboBox()
        self.tax_mode.addItem("НДФЛ не рассчитывать", TaxMode.DISABLED.value)
        self.tax_mode.addItem("НДФЛ введён вручную", TaxMode.MANUAL.value)
        self.tax_mode.addItem("НДФЛ из внешнего расчёта", TaxMode.IMPORTED.value)
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
        actions.addWidget(self.import_button)
        actions.addWidget(self.export_button)
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
        input_help = {
            "Смены": "Фактически отработанные смены за месяц. Используется в формулах, например salary / 22 * shifts.",
            "Часы": "Фактически отработанные часы за месяц.",
            "Продажи": "Фактическая выручка сотрудника за месяц, в рублях.",
            "План": "План продаж сотрудника за месяц, в рублях.",
            "Аванс": "Уже выплаченная сумма. В расчёте учитывается как удержание из суммы к выплате.",
            "Удержания": "Прочие подтверждённые удержания. Не вводите сюда аванс или НДФЛ повторно.",
            "НДФЛ": "Сумма налога, рассчитанная вне приложения или вручную. Автоматический налоговый расчёт пока отключён.",
            "Корректировка": "Ручная доплата/корректировка. Проверьте её категорию в детализации.",
            "Доп. параметры (JSON)": 'Дополнительные числовые переменные для формул. Пример: {"commission_rate": "0.02", "returns": "1500"}. В формуле используйте имя без кавычек: sales * commission_rate.',
        }
        for index, (label, _) in enumerate(self.INPUT_COLUMNS, start=1):
            header = self.input_table.horizontalHeaderItem(index)
            if header:
                header.setToolTip(input_help[label])
        self.input_table.verticalHeader().setDefaultSectionSize(30)
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
            self.export_button.setEnabled(False)
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
                if field_name == "extra_values_json":
                    value = json.dumps(
                        {key: str(amount) for key, amount in item.extra_values.items()},
                        ensure_ascii=False,
                    )
                else:
                    value = str(getattr(item, field_name))
                self.input_table.setItem(row, column, QTableWidgetItem(value))
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
        self.import_button.setEnabled(editable)
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
            extra_values = {}
            for column, (_, field_name) in enumerate(self.INPUT_COLUMNS, start=1):
                item = self.input_table.item(row, column)
                raw = item.text().strip() if item else "0"
                if field_name == "extra_values_json":
                    payload = json.loads(raw or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Дополнительные параметры должны быть JSON-объектом")
                    extra_values = {
                        str(key): Decimal(str(value).replace(",", "."))
                        for key, value in payload.items()
                    }
                else:
                    values[field_name] = Decimal(
                        raw.replace(" ", "").replace(",", ".") or "0"
                    )
            result.append(PeriodEmployeeInput(
                period_id, employee_id,
                shifts=values["shifts"], hours=values["hours"],
                advances=values["advances"], sales=values["sales"],
                plan=values["plan"], deductions=values["deductions"],
                manual_tax=values["manual_tax"],
                fixed_adjustments=values["fixed_adjustments"],
                extra_values=extra_values,
            ))
        return result


    def import_excel(self) -> None:
        period_id = self.period.currentData()
        if not period_id:
            QMessageBox.warning(self, "Импорт Excel", "Сначала создайте расчётный месяц")
            return
        dialog = ExcelImportDialog(self.connection, period_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.load_period()
            self.status.setText(
                "Импорт завершён. Проверьте показатели и выполните расчёт ведомости."
            )

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
        self.export_button.setEnabled(bool(snapshots))

    def export_excel(self) -> None:
        period_id = self.period.currentData()
        if not period_id:
            return
        default_name = f"Ведомость_{period_id}.xlsx"
        destination, _ = QFileDialog.getSaveFileName(
            self, "Сохранить расчётную ведомость",
            str(Path.home() / default_name),
            "Книга Excel (*.xlsx)",
        )
        if not destination:
            return
        if not destination.lower().endswith(".xlsx"):
            destination += ".xlsx"
        try:
            path = export_period_workbook(self.connection, period_id, destination)
            QMessageBox.information(
                self, "Экспорт завершён",
                f"Ведомость сохранена:\\n{path}\\n\\n"
                "Файл сформирован из сохранённых снимков без повторного расчёта.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось экспортировать ведомость", str(exc))

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


class TerminationDialog(QDialog):
    def __init__(self, employees, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Оформить увольнение")
        self.resize(620, 520)
        self.employee = QComboBox()
        for employee in employees:
            self.employee.addItem(
                f"{employee.full_name} — {employee.position or 'должность не указана'}",
                employee.id,
            )
        self.termination_date = QDateEdit(QDate.currentDate())
        self.termination_date.setCalendarPopup(True)
        self.shifts = QLineEdit("0")
        self.hours = QLineEdit("0")
        self.advances = QLineEdit("0")
        self.deductions = QLineEdit("0")
        self.tax = QLineEdit("0")
        self.components = QLineEdit("[]")
        self.components.setPlaceholderText(
            '[{"code":"final_salary","title":"Оплата фактически отработанного времени",'
            '"amount":"30000","category":"earning"}]'
        )
        self.notes = QLineEdit()
        form = QFormLayout(self)
        form.addRow("Сотрудник", self.employee)
        form.addRow("Дата увольнения", self.termination_date)
        form.addRow("Фактически отработано смен", self.shifts)
        form.addRow("Фактически отработано часов", self.hours)
        form.addRow("Ранее выплаченные авансы", self.advances)
        form.addRow("Прочие удержания", self.deductions)
        form.addRow("НДФЛ (ручное значение)", self.tax)
        form.addRow("Окончательные начисления (JSON)", self.components)
        form.addRow("Примечание", self.notes)
        hint = QLabel(
            "Укажите начисления отдельными строками JSON. Категории: earning, bonus, "
            "net_salary, deduction, unclassified. Полный оклад и компенсация отпуска "
            "автоматически не добавляются."
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
        def amount(field: QLineEdit) -> Decimal:
            return Decimal(field.text().strip().replace(" ", "").replace(",", ".") or "0")
        components = json.loads(self.components.text().strip() or "[]")
        if not isinstance(components, list):
            raise ValueError("Окончательные начисления должны быть JSON-массивом")
        return {
            "employee_id": self.employee.currentData(),
            "termination_date": self.termination_date.date().toPython(),
            "worked_shifts": amount(self.shifts),
            "worked_hours": amount(self.hours),
            "advances": amount(self.advances),
            "other_deductions": amount(self.deductions),
            "manual_tax": amount(self.tax),
            "components": components,
            "notes": self.notes.text(),
        }


class TerminationsPage(QWidget):
    def __init__(self, connection, employee_directory: DirectoryPage) -> None:
        super().__init__()
        self.connection = connection
        self.employee_directory = employee_directory
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Сотрудник", "Магазин", "Дата увольнения", "Статус", "К выплате", "ID дела",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.status = QLabel(
            "Окончательный расчёт вводится отдельно от месячной ведомости. "
            "Проверьте компоненты и НДФЛ до утверждения."
        )
        self.create_button = QPushButton("Оформить увольнение")
        self.calculate_button = QPushButton("Рассчитать")
        self.approve_button = QPushButton("Утвердить расчёт")
        self.export_button = QPushButton("Экспорт Excel")
        self.create_button.clicked.connect(self.create_case)
        self.calculate_button.clicked.connect(self.calculate_selected)
        self.approve_button.clicked.connect(self.approve_selected)
        self.export_button.clicked.connect(self.export_selected)
        actions = QHBoxLayout()
        actions.addWidget(self.create_button)
        actions.addWidget(self.calculate_button)
        actions.addWidget(self.approve_button)
        actions.addWidget(self.export_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.table)
        layout.addLayout(actions)
        self.refresh()

    def selected_case_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self) -> None:
        cases = list_termination_cases(self.connection)
        self.table.setRowCount(len(cases))
        for row, case in enumerate(cases):
            result = json.loads(case["result_json"]) if case["result_json"] else {}
            values = [
                case["full_name"], case["store_name"], case["termination_date"],
                case["status"], result.get("net_total", "—"), case["id"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, case["id"])
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.update_actions()

    def update_actions(self) -> None:
        case_id = self.selected_case_id()
        case = next(
            (item for item in list_termination_cases(self.connection) if item["id"] == case_id),
            None,
        ) if case_id else None
        self.calculate_button.setEnabled(bool(case and case["status"] in ("draft", "calculated")))
        self.approve_button.setEnabled(bool(case and case["status"] == "calculated"))
        self.export_button.setEnabled(bool(case and case["result_json"]))

    def create_case(self) -> None:
        employees = list_employees(self.connection)
        if not employees:
            QMessageBox.information(
                self, "Нет активных сотрудников",
                "Нет сотрудников, для которых можно оформить увольнение.",
            )
            return
        dialog = TerminationDialog(employees, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.values()
            create_termination_case(self.connection, **values)
            self.employee_directory.refresh()
            self.refresh()
            self.status.setText(
                "Увольнение зарегистрировано. Сотрудник исключён из будущих периодов; "
                "месяц увольнения остаётся доступен для расчёта фактического времени."
            )
        except (ValueError, ArithmeticError, LookupError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Проверьте данные увольнения", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось оформить увольнение", str(exc))

    def calculate_selected(self) -> None:
        case_id = self.selected_case_id()
        if not case_id:
            QMessageBox.information(self, "Выберите дело", "Сначала выберите сотрудника.")
            return
        try:
            result = calculate_termination_case(self.connection, case_id)
            self.refresh()
            self.status.setText(
                f"Расчёт сохранён: начислено {result['gross_total']} руб.; "
                f"НДФЛ {result['tax_amount']} руб.; удержания "
                f"{result['deduction_total']} руб.; к выплате {result['net_total']} руб.\n"
                f"{result['calculation_note']}"
            )
        except (ValueError, ArithmeticError, LookupError) as exc:
            QMessageBox.warning(self, "Окончательный расчёт не выполнен", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка расчёта увольнения", str(exc))

    def export_selected(self) -> None:
        case_id = self.selected_case_id()
        if not case_id:
            QMessageBox.information(self, "Выберите дело", "Сначала выберите расчёт увольнения.")
            return
        case = next(
            (item for item in list_termination_cases(self.connection) if item["id"] == case_id),
            None,
        )
        if case is None or not case["result_json"]:
            QMessageBox.information(
                self, "Нет расчёта", "Сначала выполните и сохраните окончательный расчёт."
            )
            return
        default_name = f"Окончательный_расчёт_{case['termination_date']}_{case['full_name']}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить окончательный расчёт", default_name, "Excel (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            export_termination_workbook(self.connection, case_id, path)
            self.status.setText(f"Excel-файл сохранён: {path}")
        except (ValueError, LookupError) as exc:
            QMessageBox.warning(self, "Не удалось экспортировать расчёт", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка экспорта", str(exc))

    def approve_selected(self) -> None:
        case_id = self.selected_case_id()
        if not case_id:
            return
        answer = QMessageBox.question(
            self, "Утверждение окончательного расчёта",
            "Утвердить окончательный расчёт? После утверждения его данные будут неизменяемыми.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            approve_termination_case(self.connection, case_id)
            self.refresh()
            self.status.setText("Окончательный расчёт утверждён и защищён от изменений.")
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось утвердить расчёт", str(exc))


class MainWindow(QMainWindow):
    def __init__(self, connection, database_path: Path) -> None:
        super().__init__()
        self.connection = connection
        self.database_path = database_path
        self.setWindowTitle("Символ — расчёт заработной платы")
        self.resize(1280, 820)
        self.setMinimumSize(980, 680)
        self.setStyleSheet("""
            QWidget { font-size: 10pt; color: #1f2937; }
            QMainWindow, QDialog { background: #f6f8fb; }
            QTabWidget::pane { border: 1px solid #dbe3ed; background: #ffffff; border-radius: 10px; top: -1px; }
            QTabBar::tab { background: #e9eef5; color: #475569; padding: 10px 18px; margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
            QTabBar::tab:selected { background: #ffffff; color: #0f172a; font-weight: 600; }
            QPushButton { background: #e8eef7; border: 1px solid #d3deeb; border-radius: 7px; padding: 8px 13px; min-height: 18px; }
            QPushButton:hover { background: #dce7f5; }
            QPushButton:default { background: #2457a6; color: white; }
            QLineEdit, QComboBox, QDateEdit, QSpinBox, QPlainTextEdit { background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px; selection-background-color: #b9d2f4; }
            QTableWidget { background: white; alternate-background-color: #f7f9fc; border: 1px solid #dbe3ed; gridline-color: #e5eaf1; border-radius: 8px; }
            QHeaderView::section { background: #edf2f8; color: #334155; padding: 8px; border: 0; border-bottom: 1px solid #dbe3ed; font-weight: 600; }
            QLabel { color: #334155; }
        """)
        tabs = QTabWidget()
        employee_page = DirectoryPage(connection, employees=True)
        tabs.addTab(CalculationsPage(connection), "Расчёты")
        tabs.addTab(employee_page, "Сотрудники")
        tabs.addTab(DirectoryPage(connection, employees=False), "Магазины")
        tabs.addTab(RulesPage(connection), "Правила оплаты")
        tabs.addTab(TerminationsPage(connection, employee_page), "Увольнения")
        backup_button = QPushButton("Создать резервную копию")
        restore_button = QPushButton("Восстановить из копии")
        backup_button.clicked.connect(self.create_backup_dialog)
        restore_button.clicked.connect(self.restore_backup_dialog)
        backup_bar = QHBoxLayout()
        backup_bar.addWidget(backup_button)
        backup_bar.addWidget(restore_button)
        backup_bar.addStretch(1)
        root = QVBoxLayout()
        root.addLayout(backup_bar)
        root.addWidget(tabs)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

    def create_backup_dialog(self) -> None:
        default_path = self.database_path.parent / "backups" / default_backup_name()
        path, _ = QFileDialog.getSaveFileName(
            self, "Создать резервную копию", str(default_path), "SQLite (*.sqlite3)"
        )
        if not path:
            return
        if not path.lower().endswith(".sqlite3"):
            path += ".sqlite3"
        try:
            create_backup(self.connection, path)
            QMessageBox.information(
                self, "Резервная копия создана",
                f"Копия создана и проверена:\n{path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка резервного копирования", str(exc))

    def restore_backup_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать резервную копию", str(self.database_path.parent),
            "SQLite (*.sqlite3 *.db);;Все файлы (*)",
        )
        if not path:
            return
        try:
            validate_backup(path)
        except Exception as exc:
            QMessageBox.critical(self, "Резервная копия не прошла проверку", str(exc))
            return
        answer = QMessageBox.warning(
            self, "Подтверждение восстановления",
            "Текущая база будет заменена выбранной копией. Перед восстановлением "
            "автоматически создаётся страховочная копия текущей базы. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        safety_path = self.database_path.parent / "backups" / (
            "before_restore_" + default_backup_name()
        )
        try:
            restore_backup(self.connection, path, safety_path)
            QMessageBox.information(
                self, "Восстановление завершено",
                "База восстановлена. Страховочная копия текущего состояния:\n"
                f"{safety_path}\n\nПерезапустите приложение, чтобы обновить все открытые данные.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка восстановления", str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Символ")
    data_dir = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    ))
    connection = connect(data_dir / "symbol_payroll.sqlite3")
    initialize(connection)
    directory_count = connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    employee_count = connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    if directory_count == 0 and employee_count == 0:
        answer = QMessageBox.question(
            None,
            "Импорт исходной ведомости",
            "В базе пока нет магазинов и сотрудников. Импортировать справочник из "
            "переданного Excel-файла?\n\n"
            "Будут добавлены 8 магазинов и 43 именных записи сотрудников. "
            "Суммы окладов сохранятся с типом «не определено» — gross/net не угадываем.\n\n"
            "12 строк с названиями должностей без ФИО останутся для ручной привязки. "
            "Исторические показатели и формулы Excel не станут автоматическими правилами.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                imported = import_legacy_directory(connection)
                QMessageBox.information(
                    None,
                    "Импорт завершён",
                    f"Добавлено магазинов: {imported['stores']}\\n"
                    f"Сотрудников: {imported['employees']}\\n"
                    f"Строк без ФИО для ручной привязки: {imported['role_placeholders']}\\n\\n"
                    "Проверьте список сотрудников и уточните тип каждого оклада. "
                    "Правила оплаты из Excel не активированы.",
                )
            except Exception as exc:
                QMessageBox.critical(None, "Ошибка импорта", str(exc))
    try:
        create_daily_backup(connection, data_dir / "backups", retention=30)
    except Exception as exc:
        QMessageBox.warning(
            None, "Не удалось создать автоматическую копию",
            f"Приложение продолжит работу, но резервная копия не создана:\n{exc}",
        )
    window = MainWindow(connection, data_dir / "symbol_payroll.sqlite3")
    window.show()
    result = app.exec()
    connection.close()
    return result
