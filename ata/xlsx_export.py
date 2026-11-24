"""Exporta os itens de ação da ata para uma planilha .xlsx."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font


def write_action_items_xlsx(path: Path, action_items: list[dict[str, Any]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Itens de Ação"
    headers = ("Tarefa", "Responsável", "Prazo")
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for item in action_items:
        ws.append([
            str(item.get("task") or "").strip(),
            item.get("owner") or "",
            item.get("deadline") or "",
        ])
    widths = (60, 25, 18)
    for col, width in zip("ABC", widths):
        ws.column_dimensions[col].width = width
    wb.save(path)
