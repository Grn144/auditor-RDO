"""Testes de fumaça do gerador de PDF: garantem que ele roda sem exceção
e produz um PDF válido para os formatos de dados que o app realmente
passa (com/sem foto da obra, com/sem fotos, atividades vazias etc.)."""
import io
from pathlib import Path

from PIL import Image as PILImage

import relatorio_pdf as rp

LOGO = Path(__file__).resolve().parent.parent / "static" / "officez_logo.png"


def _jpeg_sintetico(largura: int, altura: int) -> bytes:
    """Gera uma foto "de câmera" sintética (sem depender de nenhum
    arquivo real) só pra testar redimensionamento/corte."""
    img = PILImage.new("RGB", (largura, altura), color=(120, 140, 160))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


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
             # cada foto é uma FUNÇÃO que devolve os bytes, não os bytes
             # em si — é assim que o app.py passa (leitura preguiçosa do
             # zip, uma foto de cada vez, pra não estourar memória).
             "fotos": [LOGO.read_bytes, None]},
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


def test_fotos_sao_lidas_sob_demanda_nao_de_uma_vez():
    """Trava o ponto central da correção de memória: a função que busca a
    foto só pode ser chamada quando o card é efetivamente desenhado — se
    `gerar_pdf` (ou algo no meio do caminho) lesse tudo de antemão, o
    contador teria mais de 1 chamada por foto antes mesmo de `gerar_pdf`
    rodar, ou o app.py ficaria obrigado a manter todos os bytes na
    memória ao mesmo tempo (o que é exatamente o que essa mudança evita)."""
    cabecalho, atividades, resumo, _ = _dados_exemplo()
    chamadas = []

    def carregar():
        chamadas.append(1)
        return LOGO.read_bytes()

    grupos_fotos = [
        {"letra": "a", "desc": "PRÉ-OBRA", "tarefas": [
            {"codigo": "A1", "descricao": "TAREFA 1", "fotos": [carregar]},
        ]},
    ]
    assert chamadas == []  # nada foi lido só por montar a estrutura

    pdf = rp.gerar_pdf(cabecalho, atividades, resumo, grupos_fotos,
                       "25/08/2026 10:00")

    assert pdf[:5] == b"%PDF-"
    assert chamadas == [1]  # lida exatamente uma vez, na hora de desenhar


def test_cortar_para_preencher_reduz_foto_em_alta_resolucao():
    """Trava a segunda metade da correção de memória: cortar pela
    proporção certa não basta — uma foto de celular (aqui simulada em
    4000x3000) não pode ir pro PDF em resolução original só porque vai
    aparecer pequena (~8cm) no card. Sem isso, algumas centenas de fotos
    assim sozinhas já estouram os 512MB do Render free tier."""
    original = _jpeg_sintetico(4000, 3000)

    saida = rp._cortar_para_preencher(original, 4000 / 3000, 800, 600)

    resultado = PILImage.open(io.BytesIO(saida))
    assert resultado.size == (800, 600)
    assert len(saida) < len(original)


def test_cortar_para_preencher_nunca_amplia_foto_pequena():
    """Uma foto já menor que o alvo de exibição fica como está — nunca é
    esticada pra cima (perderia qualidade à toa)."""
    original = _jpeg_sintetico(200, 150)

    saida = rp._cortar_para_preencher(original, 200 / 150, 800, 600)

    resultado = PILImage.open(io.BytesIO(saida))
    assert resultado.size == (200, 150)


def test_reduzir_para_exibicao_reduz_logo_em_alta_resolucao():
    """Mesma lógica pra imagem/logo da obra no cabeçalho: também não pode
    ir pro PDF em resolução original."""
    original = _jpeg_sintetico(3000, 3000)

    saida = rp._reduzir_para_exibicao(original, 400, 400)

    resultado = PILImage.open(io.BytesIO(saida))
    assert resultado.width <= 400 and resultado.height <= 400
    assert len(saida) < len(original)


def test_gerar_pdf_com_muitas_fotos_em_alta_resolucao_fica_pequeno():
    """Teste de ponta a ponta simulando um relatório grande (100 fotos em
    resolução de câmera de celular): o PDF final tem que ficar
    proporcional ao conteúdo exibido, não ao tamanho original das
    fotos — sinal de que o redimensionamento está realmente em uso em
    todo o pipeline, não só na função isolada."""
    cabecalho, atividades, _, _ = _dados_exemplo()
    foto_grande = _jpeg_sintetico(4000, 3000)
    tarefas = [
        {"codigo": f"A{i}", "descricao": f"TAREFA {i}", "fotos": [lambda d=foto_grande: d]}
        for i in range(100)
    ]
    grupos_fotos = [{"letra": "a", "desc": "PRÉ-OBRA", "tarefas": tarefas}]
    resumo = {"tarefas": 100, "fotos": 100, "grupos": 1}

    pdf = rp.gerar_pdf(cabecalho, atividades, resumo, grupos_fotos,
                       "25/08/2026 10:00")

    assert pdf[:5] == b"%PDF-"
    # 100 fotos de ~4000x3000 sem redução passariam de dezenas de MB
    # cada uma incorporada; com a redução, o PDF inteiro fica na casa de
    # poucos MB.
    assert len(pdf) < 20 * 1024 * 1024
