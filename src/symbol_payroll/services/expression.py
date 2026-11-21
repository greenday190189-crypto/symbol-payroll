"""Безопасное вычисление ограниченных арифметических выражений.

Разрешены только числа, имена из контекста и арифметические операции.
Вызовы функций, атрибуты, индексация и произвольный Python запрещены.
"""

import ast
import operator
from decimal import Decimal, InvalidOperation


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class ExpressionError(ValueError):
    pass


def evaluate(expression: str, values: dict[str, Decimal]) -> Decimal:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError("Некорректное выражение") from exc

    def visit(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ExpressionError(f"Неизвестный параметр: {node.id}")
            return Decimal(values[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ExpressionError("Деление на ноль")
            try:
                return _BINOPS[type(node.op)](left, right)
            except (InvalidOperation, ZeroDivisionError) as exc:
                raise ExpressionError("Ошибка арифметической операции") from exc
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
            return _UNARYOPS[type(node.op)](visit(node.operand))
        raise ExpressionError(f"Недопустимая конструкция: {type(node).__name__}")

    return visit(tree)
