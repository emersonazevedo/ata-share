from openpyxl import load_workbook

from ata.xlsx_export import write_action_items_xlsx


def test_write_action_items_xlsx(tmp_path):
    path = tmp_path / "acoes.xlsx"
    write_action_items_xlsx(path, [
        {"task": "revisar PR", "owner": "Ana", "deadline": "sexta"},
        {"task": "atualizar doc", "owner": None, "deadline": None},
    ])
    wb = load_workbook(path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("Tarefa", "Responsável", "Prazo")
    assert rows[1] == ("revisar PR", "Ana", "sexta")
    assert rows[2] == ("atualizar doc", None, None)


def test_write_action_items_xlsx_empty(tmp_path):
    path = tmp_path / "acoes.xlsx"
    write_action_items_xlsx(path, [])
    wb = load_workbook(path)
    rows = list(wb.active.iter_rows(values_only=True))
    assert rows == [("Tarefa", "Responsável", "Prazo")]
