"""Testes de fumaça do gerador de PDF: garantem que ele roda sem exceção
e produz um PDF válido para os formatos de dados que o app realmente
passa (com/sem foto da obra, com/sem fotos, atividades vazias etc.)."""
from pathlib import Path

import relatorio_pdf as rp

LOGO = Path(__file__).resolve().parent.parent / "static" / "officez_logo.png"


def _dados_exemplo():
    cabecalho = {
        "nome": "OBRA TESTE", "numeroContrato": "1", "cliente": "ACME",
        "endereco": "Rua X, 100", "responsavel": "Fulano",
        "status": {"descricao": "Em Andamento"},
        "prazo": {"contratual": 10, "decorrido": 5, "aVencer": 5},
    }
    atividades = [
        {"grupo": "A", "grupo_desc": "PRÉ-OBRA", "codigo": "1",
         "descricao": "TAREFA 1", "porcentagem": 100, "quantidade": 1,
         "unidade": "VB"},
        {"grupo": "A", "grupo_desc": "PRÉ-OBRA", "codigo": "2",
         "descricao": "TAREFA 2 SEM QUANTIDADE", "porcentagem": 40,
         "quantidade": None, "unidade": None},
    ]
    resumo = {"tarefas": 1, "fotos": 2, "grupos": 1}
    grupos_fotos = [
        {"letra": "a", "desc": "PRÉ-OBRA", "tarefas": [
            {"codigo": "A1", "descricao": "TAREFA 1",
             "fotos": [LOGO.read_bytes(), None]},
        ]},
    ]
    return cabecalho, atividades, resumo, grupos_fotos


def test_gerar_pdf_produz_bytes_validos():
    cabecalho, atividades, resumo, grupos_fotos = _dados_exemplo()
    pdf = rp.gerar_pdf(cabecalho, atividades, resumo, grupos_fotos,
                       "25/08/2026 10:00")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_gerar_pdf_funciona_sem_cabecalho_atividades_ou_fotos():
    """Caso extremo: obra sem nada preenchido não pode derrubar a geração."""
    pdf = rp.gerar_pdf({}, [], {"tarefas": 0, "fotos": 0, "grupos": 0}, [],
                       "25/08/2026 10:00")
    assert pdf[:5] == b"%PDF-"


def test_gerar_pdf_com_imagem_da_obra():
    cabecalho, atividades, resumo, grupos_fotos = _dados_exemplo()
    pdf = rp.gerar_pdf(cabecalho, atividades, resumo, grupos_fotos,
                       "25/08/2026 10:00", imagem_obra=LOGO.read_bytes())
    assert pdf[:5] == b"%PDF-"


def test_gerar_pdf_com_imagem_da_obra_invalida_nao_quebra():
    """Bytes que não são uma imagem válida (download falho, por exemplo)
    não podem derrubar a geração — só ficam sem a imagem no cabeçalho."""
    cabecalho, atividades, resumo, grupos_fotos = _dados_exemplo()
    pdf = rp.gerar_pdf(cabecalho, atividades, resumo, grupos_fotos,
                       "25/08/2026 10:00", imagem_obra=b"nao-e-uma-imagem")
    assert pdf[:5] == b"%PDF-"
