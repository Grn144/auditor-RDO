"""Geração do relatório de obra em PDF.

Mesmo conteúdo do RDO oficial do sistema Diário de Obra (cabeçalho da
obra, tabela de atividades e galeria de fotos) — sem vereditos de IA,
com visual mais profissional. Usa ReportLab (Python puro, sem
dependência de sistema), pra funcionar igual no Windows local e no
Render free tier.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

BRAND = colors.HexColor("#1f6feb")
BRAND_DARK = colors.HexColor("#0b3d91")
TEXTO = colors.HexColor("#101828")
MUTED = colors.HexColor("#5d6b85")
OK = colors.HexColor("#15803d")
OK_BG = colors.HexColor("#dcfce7")
BAD = colors.HexColor("#b91c1c")
BORDA = colors.HexColor("#dce3ec")
FUNDO_CLARO = colors.HexColor("#f4f6f9")

LOGO_PATH = Path(__file__).parent / "static" / "officez_logo.png"
MARGEM = 18 * mm
SELO_LARGURA = 34 * mm  # selo de status na tabela de atividades ("Concluída · 100%")

_styles = getSampleStyleSheet()
ESTILO_LABEL = ParagraphStyle(
    "Label", parent=_styles["Normal"], fontSize=7, textColor=MUTED,
    leading=9, spaceAfter=1)
ESTILO_VALOR = ParagraphStyle(
    "Valor", parent=_styles["Normal"], fontSize=9.5, textColor=TEXTO,
    leading=12, fontName="Helvetica-Bold")
ESTILO_TITULO_OBRA = ParagraphStyle(
    "TituloObra", parent=_styles["Normal"], fontSize=16,
    textColor=TEXTO, leading=19, fontName="Helvetica-Bold")
ESTILO_SUPRA = ParagraphStyle(
    "Supra", parent=_styles["Normal"], fontSize=8.5, textColor=BRAND,
    leading=10, fontName="Helvetica-Bold", spaceAfter=2)
ESTILO_GRUPO = ParagraphStyle(
    "Grupo", parent=_styles["Normal"], fontSize=10.5,
    textColor=colors.white, fontName="Helvetica-Bold", leading=13)
ESTILO_TAREFA_COD = ParagraphStyle(
    "TarefaCod", parent=_styles["Normal"], fontSize=9,
    textColor=TEXTO, fontName="Helvetica-Bold", leading=12)
ESTILO_TAREFA_DESC = ParagraphStyle(
    "TarefaDesc", parent=_styles["Normal"], fontSize=9,
    textColor=TEXTO, leading=12)
ESTILO_QTD = ParagraphStyle(
    "Qtd", parent=_styles["Normal"], fontSize=8.5, textColor=MUTED,
    leading=11, alignment=2)  # 2 = direita
ESTILO_LEGENDA = ParagraphStyle(
    "Legenda", parent=_styles["Normal"], fontSize=8, textColor=MUTED,
    leading=10.5)
ESTILO_CARD_TITULO = ParagraphStyle(
    "CardTitulo", parent=_styles["Normal"], fontSize=8.8, textColor=TEXTO,
    fontName="Helvetica-Bold", leading=11.5)
ESTILO_LEGENDA_COD = ParagraphStyle(
    "LegendaCod", parent=_styles["Normal"], fontSize=8,
    textColor=TEXTO, fontName="Helvetica-Bold", leading=10.5)


def _rotulo_status(porcentagem: int) -> tuple[str, colors.Color, colors.Color]:
    """Retorna (rótulo, cor do texto, cor de fundo do selo)."""
    if porcentagem >= 100:
        return "Concluída", OK, OK_BG
    if porcentagem > 0:
        return "Em andamento", BRAND, colors.HexColor("#eaf1ff")
    return "Não iniciada", MUTED, colors.HexColor("#eef2f7")


def _texto(v, padrao: str = "—") -> str:
    v = (v if v is not None else "").__str__().strip() if not isinstance(v, str) else v.strip()
    return v or padrao


class _DocumentoRelatorio(BaseDocTemplate):
    """DocTemplate com rodapé (número de página) em todas as páginas."""

    def __init__(self, *args, gerado_em: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._gerado_em = gerado_em
        frame = Frame(MARGEM, MARGEM, A4[0] - 2 * MARGEM,
                       A4[1] - 2 * MARGEM - 8 * mm, id="conteudo")
        self.addPageTemplates([
            PageTemplate(id="pagina", frames=[frame], onPage=self._rodape)
        ])

    def _rodape(self, canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(BORDA)
        canvas.setLineWidth(0.5)
        y = MARGEM - 4 * mm
        canvas.line(MARGEM, y + 4 * mm, A4[0] - MARGEM, y + 4 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGEM, y - 2 * mm,
                           f"Gerado por Auditor RDO em {self._gerado_em}")
        canvas.drawRightString(A4[0] - MARGEM, y - 2 * mm,
                                f"Página {doc.page}")
        canvas.restoreState()


def _caixa_imagem_obra(dados_img: bytes, largura_caixa: float, altura_caixa: float):
    try:
        leitor = ImageReader(io.BytesIO(dados_img))
        nl, na = leitor.getSize()
        escala = min(largura_caixa / nl, altura_caixa / na)
        w, h = nl * escala, na * escala
        img = Image(io.BytesIO(dados_img), width=w, height=h)
        img.hAlign = "CENTER"
    except Exception:  # noqa: BLE001 — imagem inválida/corrompida: ignora
        return None
    # Sem fundo/moldura: a logo do cliente fica "solta" sobre o branco da
    # página — colocar uma caixa cinza atrás dela cria uma borda visível
    # em volta de logos com fundo próprio (ex.: fundo azul da AXA), o que
    # fica com cara de retalho colado em vez de parte do cabeçalho.
    caixa = Table([[img]], colWidths=[largura_caixa])
    caixa.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return caixa


def _bloco_cabecalho(cabecalho: dict, resumo: dict,
                     imagem_obra: Optional[bytes] = None) -> list:
    elementos = []

    if LOGO_PATH.exists():
        img = ImageReader(str(LOGO_PATH))
        lw, lh = img.getSize()
        largura = 34 * mm
        altura = largura * lh / lw
        elementos.append(Image(str(LOGO_PATH), width=largura, height=altura,
                                hAlign="LEFT"))
        elementos.append(Spacer(1, 5 * mm))

    elementos.append(Paragraph("RELATÓRIO DE OBRA", ESTILO_SUPRA))
    elementos.append(Paragraph(_texto(cabecalho.get("nome")), ESTILO_TITULO_OBRA))
    elementos.append(Spacer(1, 5 * mm))

    prazo = cabecalho.get("prazo") or {}
    status = (cabecalho.get("status") or {}).get("descricao")
    campos = [
        ("CONTRATO", _texto(cabecalho.get("numeroContrato"))),
        ("CLIENTE", _texto(cabecalho.get("cliente"))),
        ("STATUS", _texto(status)),
        ("RESPONSÁVEL", _texto(cabecalho.get("responsavel"))),
        ("PRAZO CONTRATUAL", f"{prazo.get('contratual', '—')} dias"),
        ("PRAZO DECORRIDO", f"{prazo.get('decorrido', '—')} dias"),
        ("PRAZO A VENCER", f"{prazo.get('aVencer', '—')} dias"),
        ("ENDEREÇO", _texto(cabecalho.get("endereco"))),
    ]
    linhas = []
    for i in range(0, len(campos), 2):
        par = campos[i:i + 2]
        linha = []
        for rotulo, valor in par:
            cel = [Paragraph(rotulo, ESTILO_LABEL), Paragraph(valor, ESTILO_VALOR)]
            linha.append(cel)
        if len(linha) == 1:
            linha.append("")
        linhas.append(linha)

    largura_util = A4[0] - 2 * MARGEM
    caixa_img = _caixa_imagem_obra(imagem_obra, 40 * mm, 40 * mm) if imagem_obra else None
    largura_info = largura_util - 40 * mm - 6 * mm if caixa_img is not None else largura_util

    tabela_info = Table(linhas, colWidths=[largura_info / 2] * 2)
    tabela_info.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDA),
    ]))

    if caixa_img is not None:
        linha_externa = Table([[tabela_info, caixa_img]],
                              colWidths=[largura_info, 40 * mm])
        linha_externa.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        elementos.append(linha_externa)
    else:
        elementos.append(tabela_info)
    elementos.append(Spacer(1, 6 * mm))

    resumo_cel = []
    for rotulo, valor in (("TAREFAS", resumo.get("tarefas", 0)),
                          ("FOTOS", resumo.get("fotos", 0)),
                          ("GRUPOS", resumo.get("grupos", 0))):
        resumo_cel.append([Paragraph(str(valor), ParagraphStyle(
            "ResumoN", parent=_styles["Normal"], fontSize=15,
            textColor=BRAND_DARK, fontName="Helvetica-Bold", leading=18)),
            Paragraph(rotulo, ESTILO_LABEL)])
    tabela_resumo = Table([resumo_cel], colWidths=[largura_util / 3] * 3)
    tabela_resumo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), FUNDO_CLARO),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDA),
        ("LEFTPADDING", (0, 0), (-1, -1), 6 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
    ]))
    elementos.append(tabela_resumo)
    elementos.append(Spacer(1, 8 * mm))
    return elementos


def _bloco_atividades(atividades: list[dict]) -> list:
    elementos = [Paragraph("ATIVIDADES", ESTILO_SUPRA), Spacer(1, 2 * mm)]
    largura_util = A4[0] - 2 * MARGEM

    grupos_ordem: list[str] = []
    por_grupo: dict[str, list[dict]] = {}
    for ativ in atividades:
        chave = ativ["grupo"] or "—"
        if chave not in por_grupo:
            por_grupo[chave] = []
            grupos_ordem.append(chave)
        por_grupo[chave].append(ativ)

    for letra in grupos_ordem:
        itens = por_grupo[letra]
        desc_grupo = itens[0]["grupo_desc"] or ""
        titulo = f"{letra} — {desc_grupo}" if desc_grupo else letra
        linhas = [[Paragraph(titulo, ESTILO_GRUPO), "", ""]]
        estilos = [
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TOPPADDING", (0, 0), (-1, 0), 3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 3 * mm),
            ("LEFTPADDING", (0, 0), (-1, 0), 4 * mm),
        ]
        for i, ativ in enumerate(itens, start=1):
            qtd = ativ.get("quantidade")
            unid = ativ.get("unidade")
            qtd_txt = f"{qtd:g} {unid}".strip() if qtd is not None and unid else "—"
            rotulo, cor_txt, cor_fundo = _rotulo_status(ativ["porcentagem"])
            selo = Table(
                [[Paragraph(f"{rotulo} · {ativ['porcentagem']}%",
                            ParagraphStyle("Selo", parent=_styles["Normal"],
                                           fontSize=8, textColor=cor_txt,
                                           fontName="Helvetica-Bold",
                                           alignment=1))]],
                # Largura EXATA da coluna externa (SELO_LARGURA) — a coluna
                # externa tem padding zerado (ver estilos abaixo) pra não
                # somar duas larguras e estourar o selo pra fora da tabela.
                colWidths=[SELO_LARGURA])
            selo.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), cor_fundo),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]))
            linhas.append([
                Paragraph(f"{letra}{ativ['codigo']} — {ativ['descricao']}",
                          ESTILO_TAREFA_DESC),
                Paragraph(qtd_txt, ESTILO_QTD),
                selo,
            ])
            cor_linha = colors.white if i % 2 else FUNDO_CLARO
            estilos.append(("BACKGROUND", (0, i), (-1, i), cor_linha))
        tabela = Table(linhas, colWidths=[largura_util - SELO_LARGURA - 28 * mm,
                                          28 * mm, SELO_LARGURA])
        estilos += [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 1), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 1), (-1, -1), 2 * mm),
            ("TOPPADDING", (0, 1), (-1, -1), 2.5 * mm),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5 * mm),
            # a coluna do selo (2) não pode ter padding extra aqui: o selo
            # já é uma Table com a largura EXATA da coluna, então qualquer
            # padding somado empurra o conteúdo pra fora da tabela.
            ("LEFTPADDING", (2, 1), (2, -1), 0),
            ("RIGHTPADDING", (2, 1), (2, -1), 0),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDA),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, BORDA),
        ]
        tabela.setStyle(TableStyle(estilos))
        elementos.append(tabela)
        elementos.append(Spacer(1, 4 * mm))
    return elementos


def _cortar_para_preencher(dados_img: bytes, aspecto: float) -> bytes:
    """Corta a imagem pelo centro pra preencher exatamente a proporção
    largura/altura pedida, sem distorcer (equivalente a object-fit: cover
    em CSS) — o excesso é cortado, nunca esticado."""
    im = PILImage.open(io.BytesIO(dados_img)).convert("RGB")
    w, h = im.size
    atual = w / h
    if atual > aspecto:
        nova_w = max(1, round(h * aspecto))
        x0 = (w - nova_w) // 2
        im = im.crop((x0, 0, x0 + nova_w, h))
    else:
        nova_h = max(1, round(w / aspecto))
        y0 = (h - nova_h) // 2
        im = im.crop((0, y0, w, y0 + nova_h))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class _CardFoto(Flowable):
    """Card de foto: imagem cortada (cover) com cantos arredondados,
    etiqueta de identificação sobre a imagem ("D3 • Foto 01") e a
    descrição da tarefa abaixo. É um único Flowable — o ReportLab nunca
    quebra um Flowable no meio entre duas páginas, então o card nunca
    fica com a imagem numa página e o texto na seguinte (equivalente a
    break-inside: avoid em CSS)."""

    RAIO = 5
    PAD_TEXTO = 3.2 * mm
    COR_ETIQUETA = colors.Color(0.06, 0.09, 0.16, alpha=0.72)
    COR_SOMBRA = colors.Color(0.06, 0.09, 0.16, alpha=0.08)

    def __init__(self, dados_img: Optional[bytes], rotulo_id: str,
                titulo: str, largura: float, altura_imagem: float):
        super().__init__()
        self.dados_img = dados_img
        self.rotulo_id = rotulo_id
        self.largura = largura
        self.altura_imagem = altura_imagem
        largura_texto = largura - 2 * self.PAD_TEXTO
        self._paragrafo = Paragraph(titulo or "—", ESTILO_CARD_TITULO)
        _, altura_paragrafo = self._paragrafo.wrap(largura_texto, 999 * mm)
        self.altura_texto = altura_paragrafo + 2 * self.PAD_TEXTO
        self._largura_texto = largura_texto

    def wrap(self, avail_w, avail_h):
        return (self.largura, self.altura_imagem + self.altura_texto)

    def draw(self):
        c = self.canv
        w = self.largura
        h = self.altura_imagem + self.altura_texto

        c.saveState()
        c.setFillColor(self.COR_SOMBRA)
        c.roundRect(0.5, -0.5, w, h, self.RAIO, fill=1, stroke=0)
        c.restoreState()

        c.saveState()
        caminho = c.beginPath()
        caminho.roundRect(0, 0, w, h, self.RAIO)
        c.clipPath(caminho, stroke=0, fill=0)
        c.setFillColor(colors.white)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        self._desenhar_imagem(c, w, h)
        c.restoreState()

        c.saveState()
        c.setStrokeColor(BORDA)
        c.setLineWidth(0.75)
        c.roundRect(0.4, 0.4, w - 0.8, h - 0.8, self.RAIO, fill=0, stroke=1)
        c.restoreState()

        self._desenhar_etiqueta(c, h)
        self._paragrafo.drawOn(c, self.PAD_TEXTO, self.PAD_TEXTO)

    def _desenhar_imagem(self, c, w, h):
        y_img = h - self.altura_imagem
        if self.dados_img:
            try:
                cortada = _cortar_para_preencher(self.dados_img, w / self.altura_imagem)
                c.drawImage(ImageReader(io.BytesIO(cortada)), 0, y_img,
                           width=w, height=self.altura_imagem, mask="auto")
                return
            except Exception:  # noqa: BLE001 — imagem corrompida/formato inválido
                pass
        c.setFillColor(FUNDO_CLARO)
        c.rect(0, y_img, w, self.altura_imagem, fill=1, stroke=0)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawCentredString(w / 2, y_img + self.altura_imagem / 2, "(foto indisponível)")

    def _desenhar_etiqueta(self, c, h):
        c.saveState()
        fonte, tam = "Helvetica-Bold", 6.6
        pad_h, pad_v = 2.2 * mm, 1.3 * mm
        largura_etq = c.stringWidth(self.rotulo_id, fonte, tam) + 2 * pad_h
        altura_etq = tam + 2 * pad_v
        x, y = 2.5 * mm, h - 2.5 * mm - altura_etq
        c.setFillColor(self.COR_ETIQUETA)
        c.roundRect(x, y, largura_etq, altura_etq, 2, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(fonte, tam)
        c.drawString(x + pad_h, y + pad_v, self.rotulo_id)
        c.restoreState()


def _linha_de_cards(cards: list, largura_util: float) -> Table:
    if len(cards) == 1:
        cards = [cards[0], ""]
    linha = Table([cards], colWidths=[largura_util / 2] * 2)
    linha.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 3 * mm),
        ("LEFTPADDING", (1, 0), (1, 0), 3 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * mm),
    ]))
    return linha


def _bloco_fotos(grupos_fotos: list[dict]) -> list:
    """`grupos_fotos`: lista de {letra, desc, tarefas: [{codigo, descricao,
    fotos: [bytes|None, ...]}]} — já vem organizada nessa ordem."""
    elementos = [Paragraph("FOTOS", ESTILO_SUPRA), Spacer(1, 2 * mm)]
    largura_util = A4[0] - 2 * MARGEM
    largura_card = largura_util / 2 - 3 * mm
    altura_imagem = 50 * mm

    for grupo in grupos_fotos:
        titulo = f"{grupo['letra']} — {grupo['desc']}" if grupo["desc"] else grupo["letra"]
        cabecalho_grupo = Table([[Paragraph(titulo, ESTILO_GRUPO)]],
                                colWidths=[largura_util])
        cabecalho_grupo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BRAND),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
        ]))
        elementos.append(cabecalho_grupo)
        elementos.append(Spacer(1, 4 * mm))

        cards_linha: list = []
        for tarefa in grupo["tarefas"]:
            fotos = tarefa["fotos"] or [None]
            for n, dados_img in enumerate(fotos, start=1):
                rotulo_id = f"{tarefa['codigo']} • Foto {n:02d}"
                cards_linha.append(_CardFoto(dados_img, rotulo_id,
                                             tarefa["descricao"],
                                             largura_card, altura_imagem))
                if len(cards_linha) == 2:
                    elementos.append(_linha_de_cards(cards_linha, largura_util))
                    cards_linha = []
        if cards_linha:
            elementos.append(_linha_de_cards(cards_linha, largura_util))
        elementos.append(Spacer(1, 2 * mm))
    return elementos


def gerar_pdf(cabecalho: dict, atividades: list[dict], resumo: dict,
             grupos_fotos: list[dict], gerado_em: str,
             imagem_obra: Optional[bytes] = None) -> bytes:
    """Monta o PDF completo e retorna os bytes prontos pra download."""
    buffer = io.BytesIO()
    doc = _DocumentoRelatorio(buffer, pagesize=A4, gerado_em=gerado_em,
                              topMargin=MARGEM, bottomMargin=MARGEM,
                              leftMargin=MARGEM, rightMargin=MARGEM)
    story: list = []
    story += _bloco_cabecalho(cabecalho, resumo, imagem_obra)
    if atividades:
        story += _bloco_atividades(atividades)
    if grupos_fotos:
        story += _bloco_fotos(grupos_fotos)
    doc.build(story)
    return buffer.getvalue()
