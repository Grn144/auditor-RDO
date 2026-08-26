"""Testes do preenchimento da planilha de medição: casamento de linhas
(grupo+item) com as tarefas do app e cálculo do incremento por rodada."""
import pytest

import planilha_medicao as pm


def _wb_com_linhas(linhas):
    """Monta um workbook mínimo no formato da planilha de medição:
    coluna A=grupo, B=item, C=descrição, e valores extras (ex.: QT. de
    rodadas já lançadas) nas colunas indicadas.

    `linhas`: lista de dicts {row, a, b, c, valores: {coluna: valor}}."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for linha in linhas:
        ws.cell(row=linha["row"], column=1, value=linha["a"])
        ws.cell(row=linha["row"], column=2, value=linha["b"])
        ws.cell(row=linha["row"], column=3, value=linha.get("c", ""))
        for coluna, valor in linha.get("valores", {}).items():
            ws.cell(row=linha["row"], column=coluna, value=valor)
    return wb


def test_preenche_incremento_quando_nao_ha_rodada_anterior():
    wb = _wb_com_linhas([{"row": 8, "a": "D", "b": "1", "c": "ITEM D1"}])
    atividades = [{"grupo": "D", "codigo": "1", "porcentagem": 90, "quantidade": 1}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=1)

    assert wb.active.cell(row=8, column=16).value == 0.9  # coluna P = QT. medição 01
    assert avisos == []


def test_calcula_so_o_incremento_desde_a_rodada_anterior():
    """Exemplo real do usuário: medição 1 lançou 0,8 de um item que
    agora está 100% — a medição 2 recebe só a diferença (0,2)."""
    wb = _wb_com_linhas([{"row": 8, "a": "A", "b": "1", "c": "ITEM A1",
                           "valores": {16: 0.8}}])
    atividades = [{"grupo": "A", "codigo": "1", "porcentagem": 100, "quantidade": 1}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=2)

    assert wb.active.cell(row=8, column=19).value == 0.2  # coluna S = QT. medição 02
    assert avisos == []


def test_nao_escreve_e_avisa_quando_incremento_seria_negativo():
    wb = _wb_com_linhas([{"row": 8, "a": "A", "b": "1", "c": "ITEM A1",
                           "valores": {16: 0.9}}])
    atividades = [{"grupo": "A", "codigo": "1", "porcentagem": 50, "quantidade": 1}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=2)

    assert wb.active.cell(row=8, column=19).value is None
    assert len(avisos) == 1
    assert "A1" in avisos[0]


def test_avisa_item_da_planilha_sem_tarefa_correspondente_no_app():
    wb = _wb_com_linhas([{"row": 8, "a": "A", "b": "1", "c": "ITEM A1"}])

    avisos = pm.preencher_medicao(wb, atividades=[], rodada=1)

    assert len(avisos) == 1
    assert "A1" in avisos[0]


def test_avisa_tarefa_do_app_sem_linha_correspondente_na_planilha():
    wb = _wb_com_linhas([])
    atividades = [{"grupo": "B", "codigo": "2", "porcentagem": 50, "quantidade": 2}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=1)

    assert len(avisos) == 1
    assert "B2" in avisos[0]


def test_ignora_linha_de_cabecalho_de_categoria():
    """Linhas onde grupo == item (ex.: A/A) são o subtotal da categoria,
    não um item — não devem ser tratadas como item nem gerar aviso."""
    wb = _wb_com_linhas([
        {"row": 8, "a": "A", "b": "A", "c": "PRÉ-OBRA"},
        {"row": 9, "a": "A", "b": "1", "c": "ITEM A1"},
    ])
    atividades = [{"grupo": "A", "codigo": "1", "porcentagem": 100, "quantidade": 2}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=1)

    assert wb.active.cell(row=9, column=16).value == 2.0
    assert avisos == []


def test_avisa_quando_tarefa_sem_quantidade_cadastrada():
    wb = _wb_com_linhas([{"row": 8, "a": "A", "b": "1", "c": "ITEM A1"}])
    atividades = [{"grupo": "A", "codigo": "1", "porcentagem": 40, "quantidade": None}]

    avisos = pm.preencher_medicao(wb, atividades, rodada=1)

    assert wb.active.cell(row=8, column=16).value is None
    assert len(avisos) == 1


def test_rodada_invalida_levanta_erro():
    wb = _wb_com_linhas([])
    with pytest.raises(ValueError):
        pm.preencher_medicao(wb, [], rodada=5)
