"""Preenche a coluna "QT." de uma rodada de medição (Medição 01-04) numa
planilha de medição de obra (.xlsx), usando o progresso já apontado no
Diário de Obra (quantidade planejada x porcentagem de cada tarefa).

O layout da planilha (linha do cabeçalho, nº de colunas descritivas
antes do bloco de medição) MUDA de um projeto/cliente pra outro — já
vimos dois arquivos reais com cabeçalhos em linhas diferentes e a
coluna "QT." da rodada 1 em posições diferentes. Por isso as colunas
nunca são fixas: a linha de cabeçalho é achada pela coluna A = "ITEM",
e a coluna "QT." de cada rodada é achada pelo texto "MEDIÇÃO 0N" no
cabeçalho — ela sempre fica imediatamente à direita da "QT." correspondente.

Coluna A = grupo (letra da etapa, ex. "D"), coluna B = item (número,
ex. "1"); linhas de subtotal de categoria repetem o mesmo valor em A e
B (ex. A="A", B="A") e não são tratadas como item.

Só a célula "QT." da rodada escolhida é escrita — fórmulas de subtotal,
ACUMULADO e SALDO já existentes na planilha continuam intactas e
recalculam sozinhas quando o Excel abrir o arquivo.
"""
from __future__ import annotations

import re
import unicodedata

_PADRAO_RODADA = re.compile(r"^MEDICAO\s*0?(\d+)$")


def _normalizar(texto) -> str:
    """Maiúsculas e sem acento — pra comparar texto de cabeçalho sem
    depender de como o Excel guardou os acentos."""
    if texto is None:
        return ""
    forma = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join(c for c in forma if not unicodedata.combining(c))
    return sem_acento.strip().upper()


def _numero_ja_lancado(ws, ws_valores, row: int, col: int) -> tuple[float, bool]:
    """Lê o número já lançado na célula (row, col) de uma rodada
    anterior. Se a célula tiver uma FÓRMULA em vez de um número puro —
    caso real: cliente registra o progresso da rodada com uma fórmula
    tipo "=G13*0.87" —, `.value` devolve o texto da fórmula, não o
    resultado (o openpyxl não calcula fórmulas). Nesse caso, busca o
    valor já calculado em cache em `ws_valores` (o mesmo arquivo aberto
    com data_only=True). Sem conseguir um número de nenhuma das duas
    formas, NÃO assume 0 — isso duplicaria a medição — e devolve
    `resolvido=False` pra quem chama decidir (avisar e não escrever).

    Devolve (valor, resolvido)."""
    bruto = ws.cell(row=row, column=col).value
    if isinstance(bruto, (int, float)):
        return float(bruto), True
    if bruto is None:
        return 0.0, True
    if ws_valores is not None:
        cache = ws_valores.cell(row=row, column=col).value
        if isinstance(cache, (int, float)):
            return float(cache), True
    return 0.0, False


def _localizar_estrutura(ws) -> tuple[int, dict[int, int]]:
    """Acha a linha de cabeçalho (coluna A = "ITEM") e, nela, a coluna
    "QT." de cada rodada de medição (a coluna imediatamente à esquerda
    de cada célula "MEDIÇÃO 0N").

    Devolve (linha_do_cabecalho, {numero_da_rodada: coluna_da_qt})."""
    header_row = None
    for row in range(1, ws.max_row + 1):
        if _normalizar(ws.cell(row=row, column=1).value) == "ITEM":
            header_row = row
            break
    if header_row is None:
        raise ValueError('Não encontrei a linha de cabeçalho (coluna A = '
                         '"ITEM") na planilha de medição.')

    rodada_colunas: dict[int, int] = {}
    for col in range(2, ws.max_column + 1):
        m = _PADRAO_RODADA.match(_normalizar(ws.cell(row=header_row, column=col).value))
        if m:
            rodada_colunas[int(m.group(1))] = col - 1  # QT. fica uma coluna à esquerda

    if not rodada_colunas:
        raise ValueError('Não encontrei nenhuma coluna "MEDIÇÃO 0N" no '
                         'cabeçalho da planilha.')

    return header_row, rodada_colunas


def _linhas_itens(ws, data_start_row: int) -> list[tuple[int, str, str]]:
    """Devolve (linha, grupo, numero) de cada linha de item, a partir de
    `data_start_row`, parando na primeira linha totalmente vazia (fim da
    tabela). Linhas de subtotal de categoria (grupo == numero) são
    puladas."""
    linhas = []
    row = data_start_row
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


def preencher_medicao(wb, atividades: list[dict], rodada: int, wb_valores=None) -> list[str]:
    """Preenche, na 1ª planilha de `wb`, a coluna QT. da `rodada` (1-4)
    com o incremento de cada item desde a última rodada já lançada.
    Muta `wb` in place. Devolve a lista de avisos — itens sem
    correspondência, incrementos que dariam negativo etc. — sem nunca
    levantar exceção por causa deles.

    `wb_valores`: o mesmo arquivo carregado com `data_only=True` —
    usado só pra ler o valor em cache de rodadas anteriores que tenham
    sido preenchidas com fórmula em vez de número puro (ver
    `_numero_ja_lancado`). Opcional; sem ele, uma rodada anterior com
    fórmula gera aviso em vez de arriscar duplicar o valor.

    Levanta ValueError se não achar a estrutura da planilha (cabeçalho
    ou colunas de medição) ou se `rodada` não existir nela."""
    ws = wb.worksheets[0]
    ws_valores = wb_valores.worksheets[0] if wb_valores is not None else None
    header_row, rodada_colunas = _localizar_estrutura(ws)

    if rodada not in rodada_colunas:
        raise ValueError(f"rodada inválida: {rodada} (disponíveis nesta "
                         f"planilha: {sorted(rodada_colunas)})")

    col_atual = rodada_colunas[rodada]
    cols_anteriores = [rodada_colunas[r] for r in sorted(rodada_colunas) if r < rodada]

    mapa_ativ = _mapa_atividades(atividades)
    encontrados: set[tuple[str, str]] = set()
    avisos: list[str] = []

    for row, grupo, numero in _linhas_itens(ws, header_row + 1):
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
        ja_lancado = 0.0
        leitura_falhou = False
        for c in cols_anteriores:
            valor, ok = _numero_ja_lancado(ws, ws_valores, row, c)
            if not ok:
                leitura_falhou = True
                break
            ja_lancado += valor
        if leitura_falhou:
            avisos.append(
                f"Item {item_label}: uma rodada anterior tem fórmula na "
                "coluna QT. e não consegui ler o valor calculado — não "
                "preenchido, confira manualmente pra não duplicar a medição.")
            continue

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
