"""Testes da lógica de coleta de dados usada no relatório PDF (cabeçalho
completo da obra + tabela de atividades com percentual/quantidade)."""
import pytest

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


def test_montar_mapa_tarefas_sanitiza_codigo_com_travessia_de_diretorio():
    """Achado de segurança: o campo "item" da tarefa é texto livre no
    Diário de Obra — sem sanitização, alguém com acesso ao cronograma
    dessa obra (não precisa ser usuário deste app) poderia colocar
    "../../etc/cron.d/x" e o nome do arquivo baixado escaparia da pasta
    de destino (o código vira o nome do arquivo, ver `_nome_foto`)."""
    cronograma = {
        "cronograma": [
            {"item": "A", "descricao": "GRUPO", "tarefas": [
                {"_id": "t1", "item": "../../../etc/cron.d/malicioso",
                 "descricao": "TAREFA", "porcentagem": 0, "totalFotos": 0},
            ]},
        ]
    }
    cliente = _ClienteFake({"/obras/OBRA1/lista-de-tarefas": cronograma})

    mapa, _atividades = core.montar_mapa_tarefas(cliente, "OBRA1")

    codigo = mapa["t1"]["codigo"]
    assert "/" not in codigo
    assert "\\" not in codigo


def test_codigo_sanitizado_nao_escapa_da_pasta_de_destino(tmp_path):
    """Prova de ponta a ponta: monta o código a partir de um "item"
    malicioso, gera o nome do arquivo (`_nome_foto`) e confere que o
    caminho final continua DENTRO da pasta de destino."""
    cronograma = {
        "cronograma": [
            {"item": "A", "descricao": "GRUPO", "tarefas": [
                {"_id": "t1", "item": "../../../evil",
                 "descricao": "TAREFA", "porcentagem": 0, "totalFotos": 0},
            ]},
        ]
    }
    cliente = _ClienteFake({"/obras/OBRA1/lista-de-tarefas": cronograma})
    mapa, _ = core.montar_mapa_tarefas(cliente, "OBRA1")

    nome_arquivo = core._nome_foto(mapa["t1"]["codigo"], 1, ".jpg")
    destino = tmp_path / "pasta_destino"
    destino.mkdir()
    caminho = (destino / nome_arquivo).resolve()

    assert caminho.parent == destino.resolve()


def test_caminho_dentro_da_pasta_aceita_caminho_normal(tmp_path):
    base = tmp_path / "destino"
    base.mkdir()
    assert core._caminho_dentro_da_pasta(base, base / "a" / "1.jpg") is True


def test_caminho_dentro_da_pasta_recusa_travessia(tmp_path):
    base = tmp_path / "destino"
    base.mkdir()
    # tenta escapar pra pasta irmã, fora de `base`
    assert core._caminho_dentro_da_pasta(base, base / ".." / "fora.jpg") is False


def test_baixar_fotos_recusa_gravar_fora_da_pasta_de_destino(tmp_path, monkeypatch):
    """Defesa em profundidade: mesmo se algum bug futuro deixar passar um
    `nome_arquivo` com travessia sem sanitizar, `baixar_fotos` não pode
    gravar fora da pasta de destino — tem que pular a foto com erro."""
    chamadas_download = []
    monkeypatch.setattr(core, "_baixar_arquivo",
                        lambda url, caminho, **kw: chamadas_download.append(caminho))

    foto_maliciosa = core.Foto(codigo="x", descricao="d", url="https://x/1.jpg",
                               arquivo_origem="1.jpg", nome_arquivo="../../fora.jpg")
    destino = tmp_path / "destino"

    core.baixar_fotos([foto_maliciosa], destino, forcar=False)

    assert chamadas_download == []  # nunca chegou a tentar baixar
    assert foto_maliciosa.caminho is None


def test_baixar_arquivo_recusa_url_que_aponta_pra_ip_privado(monkeypatch, tmp_path):
    """Achado de segurança: a URL da foto vem da API do Diário de Obra —
    controlada por quem tem acesso ao cronograma da obra, não é dado
    100% confiável. O download em massa (usado por `/api/processar` e
    pela CLI) precisa da MESMA proteção contra SSRF que o proxy de
    imagem (`/api/img`) já tem, não pode buscar qualquer URL às cegas."""
    def stub_privado(host, *a, **k):
        return [(2, 1, 6, "", ("192.168.1.1", 0))]
    monkeypatch.setattr(core.socket, "getaddrinfo", stub_privado)

    with pytest.raises(core.AppError):
        core._baixar_arquivo("https://interno.exemplo.com/x.jpg", tmp_path / "x.jpg")


def test_baixar_arquivo_recusa_url_nao_https(tmp_path):
    with pytest.raises(core.AppError):
        core._baixar_arquivo("http://exemplo.com/x.jpg", tmp_path / "x.jpg")


def test_gerar_csv_neutraliza_formula_na_descricao(tmp_path):
    """Achado de segurança (CSV Injection / CWE-1236): a descrição da
    tarefa é texto livre no Diário de Obra e vai direto pro
    auditoria.csv, que o README recomenda abrir no Excel. Um valor
    começando com "=", "+", "-" ou "@" é interpretado como fórmula pelo
    Excel/LibreOffice ao abrir — precisa neutralizar, não gravar cru.

    Importante: checa o valor JÁ PARSEADO (como o Excel veria a célula),
    não o texto bruto do CSV — o módulo `csv` já envolve em aspas campos
    com aspas dentro, o que esconderia o "=" real sem provar nada."""
    import csv as csv_module
    foto = core.Foto(codigo="1", descricao='=CMD|"/C calc"!A1',
                     url="https://x/1.jpg", arquivo_origem="1.jpg",
                     grupo="A", subpasta="a", nome_arquivo="1.jpg")

    caminho = core.gerar_csv([foto], tmp_path)

    with caminho.open(encoding="utf-8-sig", newline="") as f:
        linhas = list(csv_module.reader(f, delimiter=";"))
    descricao_final = linhas[1][4]
    assert not descricao_final.startswith(("=", "+", "-", "@"))


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
