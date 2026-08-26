"""Preenche a coluna "QT." de uma rodada de medição (Medição 01-04) numa
planilha de medição de obra (.xlsx), usando o progresso já apontado no
Diário de Obra (quantidade planejada x porcentagem de cada tarefa).

Formato esperado da planilha (ver docs/superpowers/specs): cabeçalho na
linha 7, itens a partir da linha 8, coluna A = grupo (letra da etapa,
ex. "D"), coluna B = item (número, ex. "1"); linhas de subtotal de
categoria repetem o mesmo valor em A e B (ex. A="A", B="A") e não são
tratadas como item. Cada rodada de medição tem sua coluna "QT." fixa:
Medição 01 = P, 02 = S, 03 = V, 04 = Y.

Só a célula "QT." da rodada escolhida é escrita — fórmulas de subtotal,
ACUMULADO e SALDO já existentes na planilha continuam intactas e
recalculam sozinhas quando o Excel abrir o arquivo.
"""
from __future__ import annotations

HEADER_ROW = 7
DATA_START_ROW = 8

# Coluna (1-indexada) da célula "QT." de cada rodada de medição.
RODADA_COLUNAS = {1: 16, 2: 19, 3: 22, 4: 25}  # P, S, V, Y


def _numero(valor) -> float:
    return float(valor) if isinstance(valor, (int, float)) else 0.0


def _linhas_itens(ws) -> list[tuple[int, str, str]]:
    """Devolve (linha, grupo, numero) de cada linha de item, parando na
    primeira linha totalmente vazia (fim da tabela). Linhas de subtotal
    de categoria (grupo == numero) são puladas."""
    linhas = []
    row = DATA_START_ROW
    while True:
        a = ws.cell(row=row, column=1).value
        c = ws.cell(row=row, column=3).value
        if a is None and c is None:
            break
        b = ws.cell(row=row, column=2).value
        if a is not None and b is not None and str(a).strip() != str(b).strip():
            linhas.append((row, str(a).strip(), str(b).strip()))
        row += 1
    return linhas


def _mapa_atividades(atividades: list[dict]) -> dict[tuple[str, str], dict]:
    mapa = {}
    for ativ in atividades:
        grupo = str(ativ.get("grupo") or "").strip().upper()
        codigo = str(ativ.get("codigo") or "").strip()
        if grupo and codigo:
            mapa[(grupo, codigo)] = ativ
    return mapa


def preencher_medicao(wb, atividades: list[dict], rodada: int) -> list[str]:
    """Preenche, na 1ª planilha de `wb`, a coluna QT. da `rodada` (1-4)
    com o incremento de cada item desde a última rodada já lançada.
    Muta `wb` in place. Devolve a lista de avisos — itens sem
    correspondência, incrementos que dariam negativo etc. — sem nunca
    levantar exceção por causa deles."""
    if rodada not in RODADA_COLUNAS:
        raise ValueError(f"rodada inválida: {rodada} (esperado 1-4)")

    ws = wb.worksheets[0]
    col_atual = RODADA_COLUNAS[rodada]
    cols_anteriores = [RODADA_COLUNAS[r] for r in range(1, rodada)]

    mapa_ativ = _mapa_atividades(atividades)
    encontrados: set[tuple[str, str]] = set()
    avisos: list[str] = []

    for row, grupo, numero in _linhas_itens(ws):
        chave = (grupo.upper(), numero)
        ativ = mapa_ativ.get(chave)
        item_label = f"{grupo}{numero}"
        if ativ is None:
            avisos.append(f"Item {item_label} da planilha não encontrado "
                          "nas tarefas do app.")
            continue
        encontrados.add(chave)

        quantidade = ativ.get("quantidade")
        if quantidade is None:
            avisos.append(f"Item {item_label}: tarefa sem quantidade "
                          "cadastrada no app — não preenchido.")
            continue

        porcentagem = ativ.get("porcentagem") or 0
        ja_lancado = sum(_numero(ws.cell(row=row, column=c).value)
                         for c in cols_anteriores)
        realizado_atual = quantidade * (porcentagem / 100)
        incremento = round(realizado_atual - ja_lancado, 2)

        if incremento < 0:
            avisos.append(
                f"Item {item_label}: já lançado ({ja_lancado:g}) é maior "
                f"que o realizado atual ({realizado_atual:g}) — não "
                "preenchido, confira manualmente.")
            continue

        ws.cell(row=row, column=col_atual).value = incremento

    for (grupo, numero), _ativ in mapa_ativ.items():
        if (grupo, numero) not in encontrados:
            avisos.append(f"Tarefa {grupo}{numero} do app não encontrada "
                          "na planilha de medição.")

    return avisos
