#!/usr/bin/env python3
"""
Auditor de Relatórios - APP DIÁRIO DE OBRA
==========================================

Para um relatório do sistema Diário de Obra, esta ferramenta:

  1. Baixa todas as fotos do relatório para ~/Downloads.
  2. Nomeia cada foto pelo código da tarefa (ex: A1.jpg; A1.1.jpg, A1.2.jpg
     quando a tarefa tem várias fotos).
  3. Usa um modelo de IA com visão (Claude) atuando como engenheiro civil
     sênior para avaliar se cada foto é compatível com a descrição da
     atividade cadastrada, sinalizando as divergentes.
  4. Gera um CSV final e um resumo no terminal.

Uso:
    python auditar_relatorio.py <obra_id> <relatorio_id> [opções]

Exemplo:
    python auditar_relatorio.py 69e62ce907797d5a0d02bd17 69e6328daa6865a6a3078bd4

Variáveis de ambiente:
    DIARIODEOBRA_TOKEN   Token gerado em Cadastros > Empresa > Gerar token.
    ANTHROPIC_API_KEY    Chave da API Anthropic (console.anthropic.com).
    ANTHROPIC_MODEL      (opcional) Modelo de visão. Padrão: claude-opus-5.
                         Para reduzir custo em relatórios grandes, use
                         claude-sonnet-5 ou claude-haiku-4-5.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Garante saída UTF-8 mesmo em consoles Windows legados (cp1252), evitando
# UnicodeEncodeError com acentos e símbolos.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# ----------------------------------------------------------------------------
# Configuração geral
# ----------------------------------------------------------------------------

API_BASE = "https://apiexterna.diariodeobra.app/v1"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MODELS = {
    "anthropic": DEFAULT_MODEL,
    "groq": "meta-llama/llama-4-scout-17b-16e-instruct",
}
RATE_LIMIT_PER_MIN = 150          # limite da API do Diário de Obra
MIN_API_INTERVAL = 60.0 / RATE_LIMIT_PER_MIN  # espaçamento mínimo entre chamadas
MAX_IMAGE_EDGE = 1568            # px na maior aresta enviada à IA (recomendação Anthropic)
MAX_INLINE_BYTES = int(4.5 * 1024 * 1024)  # limite prático p/ base64 sem redimensionar

def _criar_contexto_ssl() -> ssl.SSLContext:
    """Contexto SSL robusto no Windows: usa a store do SO (truststore) ou
    o pacote certifi; cai no padrão do Python se nenhum estiver disponível."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


SSL_CONTEXT = _criar_contexto_ssl()

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SISTEMA_AUDITORIA = (
    "Você é um engenheiro civil sênior revisando o Relatório Diário de Obra (RDO). "
    "Recebe UMA foto e a descrição da atividade que ela deveria comprovar. "
    "Avalie, com rigor técnico e bom senso de campo, se a foto é compatível com a "
    "descrição.\n\n"
    "Critério de veredito:\n"
    "- COMPATIVEL: a foto mostra, de forma plausível, o serviço/local descrito.\n"
    "- DIVERGENTE: use SOMENTE quando a foto claramente não bate com a descrição "
    "(local errado, serviço diferente, foto genérica ou sem nexo com a atividade).\n"
    "- INCONCLUSIVO: a foto está ruim demais para avaliar (escura, borrada, "
    "enquadramento não permite identificar nada).\n\n"
    "Na dúvida razoável, prefira COMPATIVEL a DIVERGENTE — obras têm ângulos e "
    "estágios variados. Responda EXATAMENTE em duas linhas, sem texto extra:\n"
    "VEREDITO: <COMPATIVEL|DIVERGENTE|INCONCLUSIVO>\n"
    "MOTIVO: <uma frase curta justificando>"
)


# ----------------------------------------------------------------------------
# Estruturas de dados
# ----------------------------------------------------------------------------

class AppError(Exception):
    """Erro previsível (token inválido, 404, sem chave, etc.).
    O CLI o converte em mensagem + saída; o app web, em resposta JSON."""


@dataclass
class Foto:
    codigo: str            # código da tarefa (ex: D3)
    descricao: str         # descrição da atividade cadastrada
    url: str               # url da foto em alta resolução
    arquivo_origem: str    # nome original do arquivo (para extensão)
    grupo: str = ""        # letra do grupo/etapa (ex: D)
    grupo_desc: str = ""   # descrição do grupo (ex: CONTROLE DE ACESSO)
    subpasta: str = ""     # subpasta por letra (ex: "D - CONTROLE DE ACESSO")
    miniatura: str = ""    # url da miniatura (para exibição na galeria)
    nome_arquivo: str = "" # nome final em disco (ex: D3.2.jpg)
    caminho: Optional[Path] = None
    veredito: str = ""
    motivo: str = ""


# ----------------------------------------------------------------------------
# Cliente HTTP da API do Diário de Obra (stdlib, com throttling e retry em 429)
# ----------------------------------------------------------------------------

class DiarioObraClient:
    def __init__(self, token: str):
        self.token = token
        self._ultimo_request = 0.0

    def _throttle(self) -> None:
        """Mantém o ritmo abaixo do limite de 150 req/min."""
        agora = time.monotonic()
        espera = MIN_API_INTERVAL - (agora - self._ultimo_request)
        if espera > 0:
            time.sleep(espera)
        self._ultimo_request = time.monotonic()

    def get_json(self, caminho: str, tentativas: int = 5) -> dict:
        url = f"{API_BASE}{caminho}"
        for tentativa in range(1, tentativas + 1):
            self._throttle()
            req = urllib.request.Request(url, headers={"token": self.token})
            try:
                with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", "5") or "5")
                    print(f"    [429] Limite atingido. Aguardando {retry_after}s "
                          f"(tentativa {tentativa}/{tentativas})...")
                    time.sleep(retry_after)
                    continue
                if e.code == 401:
                    raise AppError("ERRO: token inválido ou expirado "
                                     "(DIARIODEOBRA_TOKEN). Gere um novo em "
                                     "Cadastros > Empresa > Gerar token.")
                if e.code == 404:
                    raise AppError(f"ERRO: recurso não encontrado (404): {url}\n"
                                     "Verifique obra_id e relatorio_id.")
                corpo = e.read().decode("utf-8", "replace")[:300]
                raise AppError(f"ERRO HTTP {e.code} em {url}\n{corpo}")
            except urllib.error.URLError as e:
                if tentativa == tentativas:
                    raise AppError(f"ERRO de conexão com {url}: {e.reason}")
                time.sleep(2 * tentativa)
        raise AppError(f"ERRO: falha após {tentativas} tentativas em {url}")


# ----------------------------------------------------------------------------
# Passo 1: mapa tarefaId -> código (item)
# ----------------------------------------------------------------------------

def montar_mapa_tarefas(client: DiarioObraClient, obra_id: str) -> dict[str, dict]:
    """Mapeia tarefaId -> {codigo, grupo, grupo_desc}.

    Neste sistema a LETRA da etapa fica no grupo (ex: item="D",
    descricao="CONTROLE DE ACESSO") e o NÚMERO fica na tarefa (item="3").
    O código final combina os dois (ex: "D3"). Se a tarefa já vier com a
    letra embutida (ex: item="A1"), usa como está."""
    print(">> Buscando lista de tarefas da obra...")
    dados = client.get_json(f"/obras/{obra_id}/lista-de-tarefas")
    mapa: dict[str, dict] = {}
    for grupo in dados.get("cronograma", []):
        letra = str(grupo.get("item") or "").strip()
        grupo_desc = (grupo.get("descricao") or "").strip()
        for tarefa in grupo.get("tarefas", []):
            tid = tarefa.get("_id")
            numero = str(tarefa.get("item") or "").strip()
            if not tid:
                continue
            # arquivo é só o NÚMERO; se a tarefa já vier "A1" com grupo "A",
            # remove a letra para sobrar só o número (fica dentro da pasta "a").
            codigo = numero
            if letra and codigo.upper().startswith(letra.upper()):
                codigo = codigo[len(letra):].lstrip() or numero
            mapa[tid] = {"codigo": codigo,
                         "grupo": letra, "grupo_desc": grupo_desc,
                         "descricao": (tarefa.get("descricao") or "").strip(),
                         "total_fotos": int(tarefa.get("totalFotos") or 0)}
    print(f"  {len(mapa)} tarefas mapeadas.")
    return mapa


# ----------------------------------------------------------------------------
# Passo 2: detalhe do relatório -> lista de fotos com código e descrição
# ----------------------------------------------------------------------------

def coletar_fotos(client: DiarioObraClient, obra_id: str, relatorio_id: str,
                  mapa_tarefas: dict[str, dict]) -> tuple[list[Foto], dict]:
    print(">> Buscando detalhe do relatório...")
    rel = client.get_json(f"/obras/{obra_id}/relatorios/{relatorio_id}")

    # Agrupa por (grupo, código) para numerar corretamente quando há várias
    # fotos — o mesmo número pode existir em grupos diferentes (a/3 e d/3).
    por_chave: dict[tuple[str, str], list[Foto]] = {}
    sem_codigo = 0
    for ativ in rel.get("atividades", []):
        tid = ativ.get("tarefaId")
        info = mapa_tarefas.get(tid)
        if info:
            codigo = info["codigo"]
            grupo = info["grupo"]
            grupo_desc = info["grupo_desc"]
        else:
            sem_codigo += 1
            codigo = str(sem_codigo)
            grupo, grupo_desc = "SEM_GRUPO", ""
        subpasta = _nome_subpasta(grupo, grupo_desc)
        descricao = (ativ.get("descricao") or "").strip()
        for foto in ativ.get("fotos", []):
            url = foto.get("url")
            if not url:
                continue
            por_chave.setdefault((subpasta, codigo), []).append(
                Foto(codigo=codigo, descricao=descricao, url=url,
                     arquivo_origem=foto.get("arquivo") or url,
                     grupo=grupo, grupo_desc=grupo_desc, subpasta=subpasta,
                     miniatura=foto.get("urlMiniatura") or url)
            )

    # Nomes finais: 1.jpg (1ª foto) e 1.1.jpg, 1.2.jpg (demais), dentro da
    # pasta da letra do grupo.
    fotos: list[Foto] = []
    for (_sub, codigo), lista in por_chave.items():
        for i, foto in enumerate(lista, start=1):
            ext = extensao_de(foto.arquivo_origem)
            foto.nome_arquivo = _nome_foto(codigo, i, ext)
            fotos.append(foto)

    grupos = sorted({f.subpasta for f in fotos})
    total = len(fotos)
    print(f"  {total} fotos em {len(por_chave)} tarefas / {len(grupos)} grupos"
          f" (relatório nº {rel.get('numero')}, data {rel.get('data')}).")
    return fotos, rel


def coletar_fotos_obra(client: DiarioObraClient, obra_id: str,
                       progresso=None) -> tuple[list[Foto], dict]:
    """Modo OBRA: baixa as fotos de TODAS as tarefas da obra (percorrendo
    'Visualizar tarefa' para cada tarefa que tem fotos), organizadas por
    letra do grupo. `progresso` é um callback opcional (str) para status."""
    mapa = montar_mapa_tarefas(client, obra_id)
    com_fotos = [(tid, info) for tid, info in mapa.items()
                 if info.get("total_fotos", 0) > 0]

    def log(msg):
        if progresso:
            progresso(msg)
        else:
            print(msg)

    log(f">> {len(com_fotos)} tarefas com fotos. Buscando fotos de cada uma...")

    por_chave: dict[tuple[str, str], list[Foto]] = {}
    for n, (tid, info) in enumerate(com_fotos, start=1):
        codigo = info["codigo"]
        grupo = info["grupo"]
        grupo_desc = info["grupo_desc"]
        subpasta = _nome_subpasta(grupo, grupo_desc)
        descricao = info.get("descricao", "")
        try:
            det = client.get_json(f"/obras/{obra_id}/lista-de-tarefas/{tid}")
        except AppError as e:
            log(f"   [!] tarefa {grupo}{codigo}: {e}")
            continue
        if det.get("descricao"):
            descricao = det["descricao"].strip()
        vistos = set()
        for r in det.get("relatorios", []):
            for foto in r.get("fotos", []):
                url = foto.get("url")
                if not url or url in vistos:
                    continue
                vistos.add(url)
                por_chave.setdefault((subpasta, codigo), []).append(
                    Foto(codigo=codigo, descricao=descricao, url=url,
                         arquivo_origem=foto.get("arquivo") or url,
                         grupo=grupo, grupo_desc=grupo_desc, subpasta=subpasta,
                         miniatura=foto.get("urlMiniatura") or url)
                )
        log(f"   ({n}/{len(com_fotos)}) {grupo}{codigo}: "
            f"{len(por_chave.get((subpasta, codigo), []))} fotos")

    fotos: list[Foto] = []
    for (_sub, codigo), lista in por_chave.items():
        for i, foto in enumerate(lista, start=1):
            ext = extensao_de(foto.arquivo_origem)
            foto.nome_arquivo = _nome_foto(codigo, i, ext)
            fotos.append(foto)

    # Nome da obra para a pasta de destino.
    nome_obra = f"obra_{obra_id}"
    try:
        info_obra = client.get_json(f"/obras/{obra_id}")
        if info_obra.get("nome"):
            nome_obra = info_obra["nome"]
    except AppError:
        pass

    grupos = sorted({f.subpasta for f in fotos})
    log(f">> Total: {len(fotos)} fotos em {len(grupos)} grupos (obra: {nome_obra}).")
    contexto = {"modo": "obra", "obra_id": obra_id, "nome": nome_obra,
                "total": len(fotos), "grupos": len(grupos)}
    return fotos, contexto


def _sanitizar_pasta(nome: str) -> str:
    """Saneia um nome de pasta preservando espaços/acentos, removendo apenas
    os caracteres inválidos no Windows."""
    nome = re.sub(r'[<>:"/\\|?*]+', " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip().rstrip(".")
    return nome or "SEM_GRUPO"


def _nome_subpasta(grupo: str, grupo_desc: str) -> str:
    """Subpasta = só a letra do grupo, em minúsculo (ex: 'd'). Igual ao
    padrão de organização usado nas obras (pastas a, b, c, d...)."""
    letra = _sanitizar_pasta(grupo).lower()
    return letra or "sem_grupo"


def extensao_de(nome_ou_url: str) -> str:
    nome = nome_ou_url.split("?")[0]
    ext = os.path.splitext(nome)[1].lower()
    return ext if ext in MEDIA_TYPES else ".jpg"


def _nome_foto(codigo: str, indice: int, ext: str) -> str:
    """Nome final da foto. A 1ª foto da tarefa é só o número (ex: '1'); as
    seguintes recebem sufixo sequencial ('1.1', '1.2', ...). indice é 1-based."""
    if indice <= 1:
        return f"{codigo}{ext}"
    return f"{codigo}.{indice - 1}{ext}"


# ----------------------------------------------------------------------------
# Passo 3: download (idempotente)
# ----------------------------------------------------------------------------

def baixar_fotos(fotos: list[Foto], destino: Path, forcar: bool) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    total = len(fotos)
    print(f"\n>> Baixando fotos para: {destino}")
    for i, foto in enumerate(fotos, start=1):
        pasta = destino / foto.subpasta if foto.subpasta else destino
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / foto.nome_arquivo
        foto.caminho = caminho
        rel = f"{foto.subpasta}/{foto.nome_arquivo}" if foto.subpasta else foto.nome_arquivo
        if caminho.exists() and not forcar:
            print(f"  [{i}/{total}] {rel} já existe (pulado).")
            continue
        try:
            _baixar_arquivo(foto.url, caminho)
            print(f"  [{i}/{total}] {rel} baixado.")
        except Exception as e:  # noqa: BLE001
            foto.caminho = None
            print(f"  [{i}/{total}] FALHA ao baixar {rel}: {e}")


def _baixar_arquivo(url: str, caminho: Path, tentativas: int = 4) -> None:
    for tentativa in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "auditor-rdo/1.0"})
            with urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT) as resp:
                dados = resp.read()
            caminho.write_bytes(dados)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and tentativa < tentativas:
                time.sleep(int(e.headers.get("Retry-After", "5") or "5"))
                continue
            raise
        except urllib.error.URLError:
            if tentativa == tentativas:
                raise
            time.sleep(2 * tentativa)


# ----------------------------------------------------------------------------
# Passo 4: auditoria por IA (visão)
# ----------------------------------------------------------------------------

def preparar_imagem(caminho: Path) -> tuple[str, str]:
    """Retorna (base64, media_type), redimensionando com Pillow se disponível."""
    try:
        from PIL import Image  # type: ignore
        import io

        with Image.open(caminho) as img:
            img = img.convert("RGB")
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return base64.standard_b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except ImportError:
        dados = caminho.read_bytes()
        if len(dados) > MAX_INLINE_BYTES:
            raise ValueError(
                "imagem grande demais para enviar sem redimensionar; "
                "instale Pillow (pip install pillow)"
            )
        media = MEDIA_TYPES.get(caminho.suffix.lower(), "image/jpeg")
        return base64.standard_b64encode(dados).decode("utf-8"), media


def _prompt_usuario(foto: Foto) -> str:
    return (f"Código da tarefa: {foto.codigo}\n"
            f"Descrição da atividade cadastrada:\n{foto.descricao or '(sem descrição)'}")


class AuditorAnthropic:
    """Auditor via API paga da Anthropic (Claude)."""

    pausa = 0.0  # o SDK já faz backoff em 429

    def __init__(self, modelo: str, chave: Optional[str] = None):
        try:
            import anthropic
        except ImportError:
            raise AppError("ERRO: pacote 'anthropic' não instalado. "
                             "Rode: pip install anthropic")
        chave = chave or os.environ.get("ANTHROPIC_API_KEY")
        if not chave:
            raise AppError("ERRO: informe a ANTHROPIC_API_KEY para usar o provedor "
                             "anthropic.")
        self._anthropic = anthropic
        self.modelo = modelo
        self.client = anthropic.Anthropic(api_key=chave)

    def avaliar(self, foto: Foto, b64: str, media: str) -> str:
        resp = self.client.messages.create(
            model=self.modelo,
            max_tokens=1500,
            output_config={"effort": "low"},
            system=[{"type": "text", "text": SISTEMA_AUDITORIA,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media, "data": b64}},
                {"type": "text", "text": _prompt_usuario(foto)},
            ]}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class AuditorGroq:
    """Auditor via API gratuita da Groq (modelos Llama com visão),
    usando o endpoint compatível com OpenAI e a biblioteca padrão."""

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
    pausa = 2.0  # espaçamento entre chamadas p/ respeitar o free tier

    def __init__(self, modelo: str, chave: Optional[str] = None):
        self.chave = chave or os.environ.get("GROQ_API_KEY")
        if not self.chave:
            raise AppError("ERRO: informe a GROQ_API_KEY para usar o provedor groq. "
                             "Gere gratuitamente em https://console.groq.com/keys")
        self.modelo = modelo

    def avaliar(self, foto: Foto, b64: str, media: str) -> str:
        corpo = json.dumps({
            "model": self.modelo,
            "temperature": 0,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": SISTEMA_AUDITORIA},
                {"role": "user", "content": [
                    {"type": "text", "text": _prompt_usuario(foto)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media};base64,{b64}"}},
                ]},
            ],
        }).encode("utf-8")

        for tentativa in range(1, 5):
            req = urllib.request.Request(
                self.ENDPOINT, data=corpo, method="POST",
                headers={"Authorization": f"Bearer {self.chave}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT) as resp:
                    dados = json.loads(resp.read().decode("utf-8"))
                return dados["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    espera = int(e.headers.get("retry-after", "10") or "10")
                    print(f"      [429 Groq] aguardando {espera}s "
                          f"(tentativa {tentativa}/4)...")
                    time.sleep(espera)
                    continue
                corpo_erro = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"HTTP {e.code}: {corpo_erro}")
            except urllib.error.URLError as e:
                if tentativa == 4:
                    raise RuntimeError(f"conexão: {e.reason}")
                time.sleep(3 * tentativa)
        raise RuntimeError("limite de tentativas excedido")


def criar_auditor(provedor: str, modelo: str, chave: Optional[str] = None):
    if provedor == "anthropic":
        return AuditorAnthropic(modelo, chave)
    if provedor == "groq":
        return AuditorGroq(modelo, chave)
    raise AppError(f"ERRO: provedor desconhecido: {provedor} "
                     "(use anthropic ou groq).")


def auditar_fotos(fotos: list[Foto], provedor: str, modelo: str) -> None:
    auditor = criar_auditor(provedor, modelo)
    baixadas = [f for f in fotos if f.caminho and f.caminho.exists()]
    total = len(baixadas)
    print(f"\n>> Auditando {total} fotos com IA "
          f"(provedor: {provedor}, modelo: {modelo})...")

    for i, foto in enumerate(baixadas, start=1):
        try:
            b64, media = preparar_imagem(foto.caminho)
        except Exception as e:  # noqa: BLE001
            foto.veredito, foto.motivo = "INCONCLUSIVO", f"não foi possível preparar a imagem: {e}"
            print(f"  [{i}/{total}] {foto.nome_arquivo}: INCONCLUSIVO ({e})")
            continue

        try:
            texto = auditor.avaliar(foto, b64, media)
            foto.veredito, foto.motivo = _parse_veredito(texto)
        except Exception as e:  # noqa: BLE001
            foto.veredito, foto.motivo = "INCONCLUSIVO", f"erro na IA: {e}"

        marca = {"COMPATIVEL": "OK", "DIVERGENTE": "!!", "INCONCLUSIVO": "??"}.get(
            foto.veredito, "??")
        print(f"  [{i}/{total}] {foto.nome_arquivo}: {marca} {foto.veredito} — {foto.motivo}")

        if auditor.pausa and i < total:
            time.sleep(auditor.pausa)


def _parse_veredito(texto: str) -> tuple[str, str]:
    veredito = "INCONCLUSIVO"
    motivo = texto.strip().replace("\n", " ")
    m = re.search(r"VEREDITO:\s*(COMPATIVEL|DIVERGENTE|INCONCLUSIVO)", texto, re.I)
    if m:
        veredito = m.group(1).upper()
    mm = re.search(r"MOTIVO:\s*(.+)", texto, re.I | re.S)
    if mm:
        motivo = mm.group(1).strip().replace("\n", " ")
    return veredito, motivo[:500]


# ----------------------------------------------------------------------------
# Passo 5: saída (CSV + resumo)
# ----------------------------------------------------------------------------

def gerar_csv(fotos: list[Foto], destino: Path) -> Path:
    caminho = destino / "auditoria.csv"
    # utf-8-sig + ';' para abrir corretamente no Excel em português.
    with caminho.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["grupo", "codigo", "arquivo", "pasta", "descricao",
                    "veredito", "motivo"])
        for foto in fotos:
            w.writerow([foto.grupo, foto.codigo, foto.nome_arquivo, foto.subpasta,
                        foto.descricao, foto.veredito or "NAO_AUDITADO", foto.motivo])
    return caminho


def resumo_terminal(fotos: list[Foto], csv_path: Path, com_ia: bool = True) -> None:
    baixadas = [f for f in fotos if f.caminho and f.caminho.exists()]

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"  Fotos no relatório: {len(fotos)}")
    print(f"  Baixadas:           {len(baixadas)}")

    if com_ia:
        divergentes = [f for f in fotos if f.veredito == "DIVERGENTE"]
        inconclusivos = [f for f in fotos if f.veredito == "INCONCLUSIVO"]
        compativeis = [f for f in fotos if f.veredito == "COMPATIVEL"]
        print(f"  COMPATIVEL:         {len(compativeis)}")
        print(f"  DIVERGENTE:         {len(divergentes)}")
        print(f"  INCONCLUSIVO:       {len(inconclusivos)}")

        if divergentes:
            print("\n  [!] FOTOS DIVERGENTES (revisar manualmente):")
            for f in divergentes:
                print(f"     - {f.nome_arquivo} [{f.codigo}]: {f.motivo}")
        if inconclusivos:
            print("\n  [?] FOTOS INCONCLUSIVAS:")
            for f in inconclusivos:
                print(f"     - {f.nome_arquivo} [{f.codigo}]: {f.motivo}")

    print(f"\n  CSV completo: {csv_path}")
    print("=" * 60)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def sanitizar(nome: str) -> str:
    return re.sub(r"[^\w.-]+", "-", nome).strip("-") or "sem-nome"


def extrair_ids(texto: str) -> tuple[str, str]:
    """Extrai (obra_id, relatorio_id) de uma URL do relatório, por exemplo:
    https://web.diariodeobra.app/#/app/obras/<obra>/relatorios/<rel>/editar"""
    m = re.search(r"obras/([^/?#]+)/relatorios/([^/?#]+)", texto)
    if not m:
        raise AppError(
            "ERRO: não consegui identificar obra_id e relatorio_id.\n"
            "Passe a URL completa do relatório, ou os dois IDs separados.\n"
            "Ex.: python auditar_relatorio.py <obra_id> <relatorio_id>")
    return m.group(1), m.group(2)


def extrair_obra_id(texto: str) -> str:
    """Extrai o obra_id de uma URL de lista-de-tarefas / obra, por exemplo:
    https://web.diariodeobra.app/#/app/obras/<obra>/lista-de-tarefas"""
    m = re.search(r"obras/([0-9a-fA-F]{8,})", texto)
    if m:
        return m.group(1)
    t = texto.strip()
    if re.fullmatch(r"[0-9a-fA-F]{8,}", t):
        return t
    raise AppError("ERRO: não consegui identificar o obra_id na URL informada.")


def resolver_alvo(texto: str) -> tuple[str, str, Optional[str]]:
    """Detecta o modo a partir do que o usuário passou.
    Retorna (modo, obra_id, relatorio_id):
      - modo 'relatorio' quando a URL tem /relatorios/<id>
      - modo 'obra' para URL de lista-de-tarefas ou só o obra_id"""
    texto = (texto or "").strip()
    if "/relatorios/" in texto:
        obra, rel = extrair_ids(texto)
        return "relatorio", obra, rel
    return "obra", extrair_obra_id(texto), None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Auditor de Relatórios do APP Diário de Obra.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("obra_id",
                        help="ID da obra OU a URL completa do relatório.")
    parser.add_argument("relatorio_id", nargs="?", default=None,
                        help="ID do relatório (omita se passou a URL completa).")
    parser.add_argument("--forcar", action="store_true",
                        help="Rebaixar fotos mesmo que já existam.")
    parser.add_argument("--ia", action="store_true",
                        help="Ativa a auditoria por IA (por padrão só baixa e nomeia).")
    parser.add_argument("--provedor", choices=["groq", "anthropic"], default="groq",
                        help="Provedor de IA quando usar --ia (padrão: groq, gratuito).")
    parser.add_argument("--modelo", default=None,
                        help="Modelo de visão para --ia (padrão depende do provedor).")
    parser.add_argument("--saida", default=None,
                        help="Pasta de destino (padrão: ~/Downloads/relatorio_...).")
    args = parser.parse_args(argv)

    token = os.environ.get("DIARIODEOBRA_TOKEN")
    if not token:
        raise AppError("ERRO: defina a variável de ambiente DIARIODEOBRA_TOKEN "
                         "(Cadastros > Empresa > Gerar token).")

    client = DiarioObraClient(token)

    if args.relatorio_id is not None:
        modo, obra_id, relatorio_id = "relatorio", args.obra_id, args.relatorio_id
    else:
        modo, obra_id, relatorio_id = resolver_alvo(args.obra_id)

    if modo == "obra":
        fotos, ctx = coletar_fotos_obra(client, obra_id)
        pasta_padrao = _sanitizar_pasta(f"obra {ctx.get('nome', obra_id)}")
    else:
        mapa = montar_mapa_tarefas(client, obra_id)
        fotos, rel = coletar_fotos(client, obra_id, relatorio_id, mapa)
        pasta_padrao = f"relatorio_{rel.get('numero', 'x')}_{sanitizar(str(rel.get('data', '')))}"

    if not fotos:
        print("Nenhuma foto encontrada.")
        return 0

    if args.saida:
        destino = Path(args.saida).expanduser()
    else:
        destino = Path.home() / "Downloads" / pasta_padrao

    baixar_fotos(fotos, destino, args.forcar)

    if args.ia:
        modelo = args.modelo or DEFAULT_MODELS[args.provedor]
        auditar_fotos(fotos, args.provedor, modelo)
    else:
        print("\n(sem IA) Apenas download e nomeação. Use --ia para auditar as fotos.")

    csv_path = gerar_csv(fotos, destino)
    resumo_terminal(fotos, csv_path, com_ia=args.ia)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AppError as e:
        print(f"\n{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário.")
        sys.exit(130)
