"""Testes da lógica de coleta de dados usada no relatório PDF (cabeçalho
completo da obra + tabela de atividades com percentual/quantidade)."""
import auditar_relatorio as core


class _ClienteFake:
    """Stub de DiarioObraClient: devolve respostas pré-programadas por
    caminho, sem fazer nenhuma chamada de rede."""

    def __init__(self, respostas: dict):
        self.respostas = respostas
        self.chamadas: list[str] = []

    def get_json(self, caminho: str) -> dict:
        self.chamadas.append(caminho)
        if caminho not in self.respostas:
            raise core.AppError(f"sem resposta configurada para {caminho}")
        return self.respostas[caminho]


def test_montar_mapa_tarefas_inclui_percentual_e_quantidade():
    cronograma = {
        "cronograma": [
            {"item": "A", "descricao": "PRÉ-OBRA", "tarefas": [
                {"_id": "t1", "item": "1",
                 "descricao": "FORNECIMENTO DE SEGURO\n-\nCRONOGRAMA: X",
                 "porcentagem": 100, "totalFotos": 0,
                 "controleDeProducao": {"ativo": True, "quantidade": 1,
                                        "unidade": "VB", "realizado": 1}},
                {"_id": "t2", "item": "2", "descricao": "SEM PRODUCAO",
                 "porcentagem": 0, "totalFotos": 2},
            ]},
        ]
    }
    cliente = _ClienteFake({"/obras/OBRA1/lista-de-tarefas": cronograma})

    mapa, atividades = core.montar_mapa_tarefas(cliente, "OBRA1")

    assert mapa["t1"] == {"codigo": "1", "grupo": "A", "grupo_desc": "PRÉ-OBRA",
                          "descricao": "FORNECIMENTO DE SEGURO\n-\nCRONOGRAMA: X",
                          "total_fotos": 0}
    assert len(atividades) == 2
    assert atividades[0] == {"grupo": "A", "grupo_desc": "PRÉ-OBRA", "codigo": "1",
                             "descricao": "FORNECIMENTO DE SEGURO",
                             "porcentagem": 100, "quantidade": 1, "unidade": "VB",
                             "realizado": 1}
    # Tarefa sem controleDeProducao: quantidade/unidade/realizado ficam
    # None (não quebra).
    assert atividades[1]["quantidade"] is None
    assert atividades[1]["unidade"] is None
    assert atividades[1]["realizado"] is None
    assert atividades[1]["porcentagem"] == 0


def test_coletar_fotos_obra_inclui_cabecalho_completo_no_contexto():
    obra_info = {"nome": "OBRA X", "cliente": "ACME", "numeroContrato": "123",
                 "prazo": {"contratual": 10, "decorrido": 5, "aVencer": 5}}
    cliente = _ClienteFake({
        "/obras/OBRA1/lista-de-tarefas": {"cronograma": []},
        "/obras/OBRA1": obra_info,
    })

    fotos, ctx = core.coletar_fotos_obra(cliente, "OBRA1")

    assert fotos == []
    assert ctx["cabecalho"] == obra_info
    assert ctx["atividades"] == []
    assert ctx["nome"] == "OBRA X"


def test_coletar_fotos_obra_segue_sem_cabecalho_se_a_busca_falhar():
    """Se /obras/{id} falhar, o resto da coleta não deve quebrar — só fica
    sem o cabeçalho completo (nome genérico como fallback)."""
    cliente = _ClienteFake({
        "/obras/OBRA1/lista-de-tarefas": {"cronograma": []},
        # sem resposta para /obras/OBRA1 -> AppError, deve ser engolido
    })

    fotos, ctx = core.coletar_fotos_obra(cliente, "OBRA1")

    assert ctx["cabecalho"] == {}
    assert ctx["nome"] == "obra_OBRA1"
